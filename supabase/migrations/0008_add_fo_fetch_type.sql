-- 0008_add_fo_fetch_type.sql
--
-- The on-demand "F&O Data Refresh" Edge Function
-- (supabase/functions/fo-refresh) logs one provider_fetch_log row per
-- invocation, so it needs a fetch_type value that doesn't already exist.
-- Named 'fo' to mirror 'all' from 0005_add_manual_refresh_fetch_type.sql.
--
-- Postgres has no ALTER CHECK CONSTRAINT -- drop and recreate it. Looked
-- up by column/table rather than assuming the auto-generated constraint
-- name, in case a differently-named constraint was ever substituted.

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
        check (fetch_type in ('price', 'intraday_price', 'fundamentals', 'dividend', 'constituents', 'all', 'fo'));
end $$;
