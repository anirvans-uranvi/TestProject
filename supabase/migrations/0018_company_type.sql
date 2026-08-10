-- 0018_company_type.sql
--
-- Replaces the boolean `is_etf` (migration 0015) with a proper category,
-- `company_type`, so `companies` can also hold non-equity, non-ETF rows --
-- specifically index rows (NIFTY, BANKNIFTY, SENSEX) so Dhan-synced index
-- option positions (pages/6_Portfolio.py's "Connect Dhan account") have a
-- `companies` row to satisfy option_contracts/futures_contracts' FK, and
-- so this app's F&O ingestion (src/data_providers/nse_fo_provider.py) can
-- widen its universe to include index options (IDO bhavcopy rows), not
-- just stock options (STO). 'Fund' is a fourth value with no rows yet --
-- room for a future non-ETF fund classification (see
-- src/services/portfolio_service.py's looks_like_etf_name(), which only
-- ever produces 'ETF' today).
--
-- NSE's F&O bhavcopy (this app's only F&O source) covers NIFTY and
-- BANKNIFTY, both NSE-listed indices. SENSEX is BSE-listed -- it is
-- seeded here too so a Dhan-synced SENSEX position at least resolves a
-- symbol instead of showing "undecoded", but this app has no BSE data
-- source, so SENSEX will never actually get F&O rows.

alter table companies
    add column company_type text not null default 'Equity'
    check (company_type in ('Equity', 'ETF', 'Index', 'Fund'));

update companies set company_type = 'ETF' where is_etf;

-- latest_screener_view must be redefined to stop referencing `is_etf`
-- BEFORE that column is dropped below -- `where not c.is_etf` only ever
-- excluded ETFs; `where c.company_type = 'Equity'` now also excludes
-- Index/Fund rows by construction, with no separate flag needed as new
-- non-equity categories are added.
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
where c.company_type = 'Equity'
  and (
    nc.is_current
    or exists (
        select 1 from portfolio_holdings ph
        where ph.symbol = c.symbol and ph.user_id = auth.uid()
    )
  );

grant select on latest_screener_view to authenticated;

alter table companies drop column is_etf;

insert into companies (symbol, name, company_type)
values
    ('NIFTY', 'Nifty 50 Index', 'Index'),
    ('BANKNIFTY', 'Nifty Bank Index', 'Index'),
    ('SENSEX', 'BSE Sensex Index', 'Index')
on conflict (symbol) do update set company_type = excluded.company_type;

notify pgrst, 'reload schema';
