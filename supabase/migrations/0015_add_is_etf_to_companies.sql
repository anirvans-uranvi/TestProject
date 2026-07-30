-- 0015_add_is_etf_to_companies.sql
--
-- The Portfolio-symbol widening added in 0013 puts any tracked
-- portfolio-only symbol onto the Dashboard screener, including ETFs and
-- gilt/liquid funds (e.g. NIFTYBEES, GILT5YBEES, LIQUIDCASE, LTGILTCASE)
-- -- a stock-momentum/dividend/PEG screener doesn't make sense for these
-- (PE/PEG/dividend criteria are all meaningless for a fund), and the user
-- asked for them to be excluded from this list specifically (Stock
-- Detail/Options/Portfolio still show them fine -- this is a
-- Dashboard-screener-only exclusion).
--
-- `is_etf` is set at company-registration time by the refresh pipeline
-- (src.services.portfolio_service.looks_like_etf_name(), see its
-- docstring for why this is name-based rather than yfinance's own
-- `quoteType` field, which is unreliable for Indian-listed ETFs).

alter table companies add column is_etf boolean not null default false;

-- Backfill: every non-Nifty50 symbol already tracked via an uploaded
-- portfolio at the time of this migration, confirmed live via yfinance's
-- longName (all four literally contain "ETF" in their real display name,
-- e.g. "Nippon India ETF Nifty 50 BeES") -- HINDZINC/INDUSINDBK/INDHOTEL/
-- VAML are real stocks and correctly stay `is_etf = false`.
update companies
set is_etf = true
where symbol in ('GILT5YBEES', 'LIQUIDCASE', 'LTGILTCASE', 'NIFTYBEES');

create or replace view latest_screener_view
with (security_invoker = true)
as
select
    c.symbol,
    c.name,
    c.sector,
    c.industry,
    s.snapshot_date,
    s.latest_price,
    s.return_1d,
    s.return_5d,
    s.return_20d,
    s.ttm_dividend_yield,
    s.pe_ratio,
    s.peg_ratio,
    s.criterion_a,
    s.criterion_b,
    s.criterion_c,
    coalesce(s.status, 'unavailable') as status,
    coalesce(s.data_quality, '{}'::jsonb) as data_quality,
    s.week_52_high,
    s.week_52_low,
    s.criterion_52w_high,
    s.criterion_52w_low
from companies c
left join nifty50_constituents nc
    on nc.symbol = c.symbol and nc.is_current
left join lateral (
    select *
    from daily_screener_snapshots dss
    where dss.symbol = c.symbol
    order by (dss.latest_price is not null) desc, dss.snapshot_date desc
    limit 1
) s on true
where not c.is_etf
  and (
    nc.is_current
    or exists (
        select 1 from portfolio_holdings ph
        where ph.symbol = c.symbol and ph.user_id = auth.uid()
    )
  );

grant select on latest_screener_view to authenticated;

notify pgrst, 'reload schema';
