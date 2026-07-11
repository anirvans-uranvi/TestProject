-- 0004_fix_constituents_fk_and_view_defaults.sql
--
-- Fixes two bugs found after first real deploy:
--
-- 1. nifty50_constituents.symbol had no FK to companies.symbol, so
--    PostgREST couldn't resolve the embedded-resource query
--    `nifty50_constituents.select("symbol, companies(...)")` used by
--    companies_repo.list_current_constituents() -- it needs a declared
--    relationship to know how to join the two tables.
--
-- 2. latest_screener_view LEFT JOINs to daily_screener_snapshots, which is
--    legitimately empty before the first screener refresh runs. That left
--    status/data_quality as NULL for every row, which ScreenerRow (status
--    is required, data_quality has a non-None default) rejects with a
--    pydantic ValidationError. Rows with no snapshot yet should just read
--    as Unavailable, matching the spec's classification rules.

do $$
begin
    if not exists (
        select 1 from pg_constraint where conname = 'nifty50_constituents_symbol_fkey'
    ) then
        alter table nifty50_constituents
            add constraint nifty50_constituents_symbol_fkey
            foreign key (symbol) references companies(symbol);
    end if;
end $$;

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
    coalesce(s.data_quality, '{}'::jsonb) as data_quality
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

-- Supabase's PostgREST schema cache usually auto-reloads on DDL via an
-- installed event trigger; NOTIFY here as a fallback so the FK is picked
-- up immediately instead of after PostgREST's next periodic refresh.
notify pgrst, 'reload schema';
