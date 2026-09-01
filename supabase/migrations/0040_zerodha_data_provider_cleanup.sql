-- 0040_zerodha_data_provider_cleanup.sql
--
-- Zerodha was removed entirely as a broker (see
-- src/utils/data_provider_settings.py's module docstring) -- "zerodha"
-- dropped from src/models/user.py's DataProvider Literal type. Any
-- account that still had it saved as their Data Provider choice failed
-- with a hard `pydantic.ValidationError` on every single page
-- (`require_login()` -> `settings_repo.get_user_settings()`, before any
-- page body runs), confirmed live. `get_user_settings` now degrades this
-- specific case gracefully instead of crashing, but this migration
-- cleans up the underlying stored value too, so that code path doesn't
-- have to keep correcting it on every read.
update user_settings set data_provider = 'yfinance_bhavcopy' where data_provider = 'zerodha';

-- Postgres has no ALTER CHECK CONSTRAINT -- drop and recreate it, same
-- approach as 0033/0037/0039. Tightened to match the current Literal
-- type now that 'zerodha' is no longer a valid application-level value.
do $$
declare
    constraint_name text;
begin
    select con.conname into constraint_name
    from pg_constraint con
    join pg_class rel on rel.oid = con.conrelid
    where rel.relname = 'user_settings'
      and con.contype = 'c'
      and pg_get_constraintdef(con.oid) like '%data_provider%';

    if constraint_name is not null then
        execute format('alter table user_settings drop constraint %I', constraint_name);
    end if;

    alter table user_settings
        add constraint user_settings_data_provider_check
        check (data_provider in ('dhan', 'yfinance_bhavcopy'));
end $$;
