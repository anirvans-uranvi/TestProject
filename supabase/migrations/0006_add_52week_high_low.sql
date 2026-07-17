-- 0006_add_52week_high_low.sql
--
-- Adds 52-week high/low price tracking (display-only proximity checks, not
-- part of the Green/Amber/Red engine): fundamental_snapshots gets the raw
-- values fetched from the provider (mirrors pe_ratio/peg_ratio/eps/
-- market_cap), daily_screener_snapshots gets the computed row (raw values
-- + the two pass/fail flags), and latest_screener_view is redefined to
-- expose all of it to the Dashboard.

alter table fundamental_snapshots
    add column if not exists week_52_high numeric(14,4),
    add column if not exists week_52_low  numeric(14,4);

alter table daily_screener_snapshots
    add column if not exists week_52_high numeric(14,4),
    add column if not exists week_52_low  numeric(14,4),
    add column if not exists criterion_52w_high boolean,
    add column if not exists criterion_52w_low  boolean;

-- Postgres' CREATE OR REPLACE VIEW can only APPEND new output columns --
-- it errors if an existing column's name/position shifts (42P16), so the
-- new week_52_high/week_52_low/criterion_52w_high/criterion_52w_low
-- columns go at the very end, after data_quality, preserving the exact
-- existing column order from 0004_fix_constituents_fk_and_view_defaults.sql.
-- Column order doesn't matter to the app either way -- it's read by name.
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

notify pgrst, 'reload schema';
