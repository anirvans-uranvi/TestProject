-- 0027_latest_option_chain_view_source.sql
--
-- Exposes `source` on `latest_option_chain_view` (defined in 0007). Needed
-- so `fo_service.dashboard_metrics_rows` can tell an NSE-sourced leg from a
-- BSE-sourced one: BSE's F&O feed was later restricted to index options
-- only (see bse_fo_provider.py / migration-era comments around "index
-- options only"), but that restriction only governs new ingestion --
-- pre-restriction BSE option_contracts rows for real stock symbols (e.g.
-- an old BSE monthly expiry a few days apart from that symbol's NSE
-- monthly expiry) are still `is_open = true` as long as their own expiry
-- date hasn't passed, and `latest_option_chain_view` has no `is_open`-style
-- concept of "still a valid source for this symbol". Confirmed live: the
-- Dashboard's "Options month" dropdown showed some months twice (once from
-- NSE, once from a stale BSE contract for the same stock) even after
-- restricting the dropdown to symbols actually shown in the screener.
--
-- Additive only (`select *` consumers, which is every current one, just
-- gain a column) -- see fo_repo.get_all_open_options and its callers.

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
    p.source,
    oc.lot_size,
    oc.contract_name,
    oc.is_open
from option_daily_prices p
join option_contracts oc
    on oc.symbol = p.symbol and oc.expiry_date = p.expiry_date
   and oc.strike_price = p.strike_price and oc.option_type = p.option_type
where oc.is_open
order by p.symbol, p.expiry_date, p.strike_price, p.option_type, p.trade_date desc;

grant select on latest_option_chain_view to authenticated;
