-- 0035_dhan_instrument_master_cache.sql
--
-- DhanProvider.get_quotes/get_fo_quotes each resolve symbols/contracts to
-- Dhan security_ids via an instrument-master CSV
-- (images.dhan.co/api-data/api-scrip-master.csv, ~211,742 rows) --
-- confirmed live: the two loaders (_load_instrument_master,
-- _load_fo_instrument_master, src/data_providers/dhan_provider.py) each
-- download it independently, and the in-memory cache was per-process, so
-- every Streamlit Cloud cold start (redeploy, waking from idle sleep) paid
-- for two full downloads of the same file again. These two tables persist
-- the already-filtered result (NSE equities / NSE+BSE F&O) shared across
-- every user and process, refreshed at most once per IST calendar day --
-- see dhan_instrument_repo.py and dhan_provider.py's client-aware loaders.
--
-- "Current state" tables, not history -- replaced wholesale on refresh
-- (delete-then-insert), same convention as dashboard_fo_metrics
-- (fo_repo.clear_dashboard_fo_metrics/upsert_dashboard_fo_metrics).
-- Freshness is tracked the existing way, via a provider_fetch_log row
-- (fetch_type='dhan_instrument_master', provider_name='equity'/'fo').

create table if not exists dhan_equity_instruments (
    security_id    text primary key,
    trading_symbol text not null
);

create table if not exists dhan_fo_instruments (
    security_id       text primary key,
    underlying_symbol text not null,
    expiry_date       date not null,
    strike_price      numeric(12, 2) not null,
    option_type       text not null check (option_type in ('FUT', 'CE', 'PE'))
);

alter table dhan_equity_instruments enable row level security;
alter table dhan_fo_instruments enable row level security;

-- Unlike companies/nifty50_constituents (written only by the service-role
-- cron), this cache is also written from a user's own Streamlit session --
-- there's no service-role path from Streamlit, same reasoning as migration
-- 0034's provider_fetch_log fix. Full authenticated access is acceptable:
-- this is public, freely-downloadable Dhan reference data with no security
-- implications, and there's no natural per-row scoping possible for a
-- whole-table replace (unlike 0034's narrow by-value scoping).
create policy "authenticated read dhan_equity_instruments"
    on dhan_equity_instruments for select
    to authenticated using (true);
create policy "authenticated write dhan_equity_instruments"
    on dhan_equity_instruments for insert
    to authenticated with check (true);
create policy "authenticated delete dhan_equity_instruments"
    on dhan_equity_instruments for delete
    to authenticated using (true);

create policy "authenticated read dhan_fo_instruments"
    on dhan_fo_instruments for select
    to authenticated using (true);
create policy "authenticated write dhan_fo_instruments"
    on dhan_fo_instruments for insert
    to authenticated with check (true);
create policy "authenticated delete dhan_fo_instruments"
    on dhan_fo_instruments for delete
    to authenticated using (true);

-- Postgres has no ALTER CHECK CONSTRAINT -- drop and recreate it, same
-- approach as 0005/0008/0033.
do $$
declare
    constraint_name text;
begin
    select con.conname into constraint_name
    from pg_constraint con
    join pg_class rel on rel.oid = con.conrelid
    where rel.relname = 'provider_fetch_log'
      and con.contype = 'c'
      and pg_get_constraintdef(con.oid) like '%fetch_type%';

    if constraint_name is not null then
        execute format('alter table provider_fetch_log drop constraint %I', constraint_name);
    end if;

    alter table provider_fetch_log
        add constraint provider_fetch_log_fetch_type_check
        check (fetch_type in (
            'price', 'intraday_price', 'fundamentals', 'dividend', 'constituents', 'all', 'fo',
            'portfolio_sync', 'dhan_instrument_master'
        ));
end $$;
