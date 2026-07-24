-- 0013_screener_fallback_and_portfolio_symbols.sql
--
-- Two Dashboard-screener fixes:
--
-- 1. The lateral join always took the single most recent
--    daily_screener_snapshots row per symbol, even when that day's price
--    fetch failed (latest_price is null, status='unavailable') -- so a
--    stock priced fine yesterday showed blank "--" today instead of its
--    last known value. Now it prefers the most recent row that actually
--    HAS a price, falling back across days -- the same idea
--    snapshot_repo.get_latest_prices() already applies for the Portfolio
--    page.
--
-- 2. latest_screener_view was scoped to only the 50 current Nifty50
--    constituents. It now also includes any symbol the *viewing* user
--    has in their own portfolio_holdings (e.g. HINDZINC, INDUSINDBK --
--    non-Nifty50 stocks/ETFs the refresh pipeline registers and tracks
--    once uploaded). security_invoker=true means auth.uid() here
--    resolves to the actual querying user, and portfolio_holdings' own
--    RLS policy additionally enforces the same scoping -- one user's
--    uploaded symbols never leak into another user's Dashboard.

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
where nc.is_current
   or exists (
        select 1 from portfolio_holdings ph
        where ph.symbol = c.symbol and ph.user_id = auth.uid()
   );

grant select on latest_screener_view to authenticated;

notify pgrst, 'reload schema';
