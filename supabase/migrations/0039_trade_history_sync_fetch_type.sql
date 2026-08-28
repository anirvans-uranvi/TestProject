-- 0039_trade_history_sync_fetch_type.sql
--
-- The new "Sync Trade History from Dhan" button (Settings ->
-- src/utils/data_provider_settings.py's _render_dhan_trade_history_sync)
-- logs one provider_fetch_log row per sync, so "Last trade synced" has
-- something to read (same pattern every other refresh button already
-- follows). Needs a fetch_type value that doesn't already exist.
--
-- Postgres has no ALTER CHECK CONSTRAINT -- drop and recreate it, same
-- approach as 0005_add_manual_refresh_fetch_type.sql,
-- 0008_add_fo_fetch_type.sql, and 0033_portfolio_sync_fetch_type.sql.

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
            'portfolio_sync', 'dhan_instrument_master', 'trade_history_sync'
        ));
end $$;

-- Same RLS gap as 0034/0036: provider_fetch_log has no user_id column, so
-- an ordinary authenticated user's own Settings-page write needs its own
-- narrow-by-value INSERT policy (Dhan-only in v1, matching
-- _render_dhan_trade_history_sync's own scope) rather than a user_id
-- check. Additive, not a replacement of 0034/0036's policies.
create policy "authenticated insert own trade_history_sync fetch log"
    on provider_fetch_log for insert
    to authenticated
    with check (
        fetch_type = 'trade_history_sync'
        and provider_name = 'dhan'
    );
