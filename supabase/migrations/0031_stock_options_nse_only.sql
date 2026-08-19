-- 0031_stock_options_nse_only.sql
--
-- Hard, database-level guarantee: STOCK option data is always NSE-sourced
-- only, never BSE -- irrespective of which market-data/Data Provider a
-- user has selected (F&O is never provider-branched at all -- it's
-- always yfinance/NSE+BSE-bhavcopy-sourced, see migration 0028's
-- docstring). BSE's own F&O feed is meant to be index-options-only
-- (SENSEX/BANKEX, etc. -- see bse_fo_provider.py's `_FUTURES_TYPES`/
-- `_OPTION_TYPES`), which already blocks any NEW stock-type (STF/STO)
-- row from ever being ingested. But PRE-RESTRICTION rows for real stock
-- symbols (an old BSE monthly expiry a few days off that symbol's real
-- NSE one) were never deleted, and fo_repo.refresh_open_flags()
-- reaffirms `is_open = true` for any row whose expiry hasn't passed yet
-- regardless of source/symbol type -- so a stale row just kept getting
-- revived forever. Confirmed live, twice: the Dashboard's "Options
-- month" dropdown showed a stock's month duplicated (once from NSE,
-- once from a stale BSE contract) -- migration 0027 first tried fixing
-- this by excluding BSE-sourced legs from the Dashboard's own cache
-- computation (fo_service.dashboard_metrics_rows), but that only
-- protects readers who apply the filter themselves -- the underlying
-- garbage rows were still live for every other consumer
-- (fo_repo.get_option_chain, fo_repo.list_option_expiries, My CSP's
-- fallback LTP lookup, Analyse Trade, the Options page).
--
-- This migration does the one-time cleanup (delete surviving BSE-sourced
-- option data for any symbol that isn't a genuine Index), and
-- permanently guards `latest_option_chain_view` itself so even a future
-- ingestion bug can never resurface a stock's BSE-derived row through
-- that shared view again.
--
-- option_contracts has no `source` column of its own (only
-- option_daily_prices does) -- deleted first, via its matching
-- option_daily_prices rows, before those are deleted out from under it.

delete from option_contracts oc
where not exists (
    select 1 from companies c where c.symbol = oc.symbol and c.company_type = 'Index'
)
and exists (
    select 1 from option_daily_prices p
    where p.symbol = oc.symbol and p.expiry_date = oc.expiry_date
      and p.strike_price = oc.strike_price and p.option_type = oc.option_type
      and p.source like 'bse_fo_bhavcopy%'
);

delete from option_daily_prices p
where p.source like 'bse_fo_bhavcopy%'
  and p.symbol not in (select symbol from companies where company_type = 'Index');

-- Permanent guard on the shared view: a BSE-sourced row only ever passes
-- through if its own symbol is a genuine Index -- same column order/
-- append-only constraint migration 0027 already documents (`create or
-- replace view` requires every pre-existing column to keep its original
-- name/position).
create or replace view latest_option_chain_view
with (security_invoker = true)
as
select distinct on (p.symbol, p.expiry_date, p.strike_price, p.option_type)
    p.symbol,
    p.expiry_date,
    p.strike_price,
    p.option_type,
    p.trade_date,
    p.open,
    p.high,
    p.low,
    p.close,
    p.last_price,
    p.prev_close,
    p.settlement_price,
    p.underlying_price,
    p.open_interest,
    p.change_in_oi,
    p.volume,
    p.turnover,
    p.num_trades,
    oc.lot_size,
    oc.contract_name,
    oc.is_open,
    p.source
from option_daily_prices p
join option_contracts oc
    on oc.symbol = p.symbol and oc.expiry_date = p.expiry_date
   and oc.strike_price = p.strike_price and oc.option_type = p.option_type
where oc.is_open
  and not (
    p.source like 'bse_fo_bhavcopy%'
    and not exists (select 1 from companies c where c.symbol = p.symbol and c.company_type = 'Index')
  )
order by p.symbol, p.expiry_date, p.strike_price, p.option_type, p.trade_date desc;

grant select on latest_option_chain_view to authenticated;
