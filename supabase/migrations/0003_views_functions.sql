-- 0003_views_functions.sql
-- Aggregation helpers so the Dashboard/Stock Detail pages can load in a
-- single query instead of joining client-side.

-- Latest screener row per symbol: current constituents joined to their
-- most recent daily_screener_snapshots + companies metadata.
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
    s.status,
    s.data_quality
from companies c
join nifty50_constituents nc
    on nc.symbol = c.symbol and nc.is_current
left join lateral (
    select *
    from daily_screener_snapshots dss
    where dss.symbol = c.symbol
    order by dss.snapshot_date desc
    limit 1
) s on true;

grant select on latest_screener_view to authenticated;

-- Classification history for a single symbol, most recent first, for the
-- Stock Detail page's status-over-time chart.
create or replace function get_classification_history(p_symbol text, p_days int default 180)
returns table (
    snapshot_date date,
    status        text,
    latest_price  numeric,
    return_1d     numeric,
    return_5d     numeric,
    return_20d    numeric,
    ttm_dividend_yield numeric,
    pe_ratio      numeric,
    peg_ratio     numeric
)
language sql
stable
security invoker
as $$
    select
        snapshot_date, status, latest_price,
        return_1d, return_5d, return_20d,
        ttm_dividend_yield, pe_ratio, peg_ratio
    from daily_screener_snapshots
    where symbol = p_symbol
      and snapshot_date >= current_date - p_days
    order by snapshot_date asc;
$$;
