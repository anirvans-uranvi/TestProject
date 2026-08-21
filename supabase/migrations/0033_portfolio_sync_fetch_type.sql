-- 0033_portfolio_sync_fetch_type.sql
--
-- The new "Portfolio Refresh" button (My Trades/My Holdings/My Positions/
-- My CSP, broker-provider accounts only -- src/utils/refresh_bar.py's
-- render_portfolio_refresh_button, wrapping
-- src/utils/data_provider_settings.py's sync_broker_portfolio) logs one
-- provider_fetch_log row per sync, so "Last portfolio refresh" has
-- something to read (same pattern every other refresh button already
-- follows). Needs a fetch_type value that doesn't already exist.
--
-- Postgres has no ALTER CHECK CONSTRAINT -- drop and recreate it. Looked
-- up by column/table rather than assuming the auto-generated constraint
-- name, in case a differently-named constraint was ever substituted --
-- same approach as 0005_add_manual_refresh_fetch_type.sql and
-- 0008_add_fo_fetch_type.sql.

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
            'price', 'intraday_price', 'fundamentals', 'dividend', 'constituents', 'all', 'fo', 'portfolio_sync'
        ));
end $$;
