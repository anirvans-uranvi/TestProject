-- 0034_portfolio_sync_fetch_log_insert_policy.sql
--
-- provider_fetch_log has no user_id column -- it's a shared operational
-- log, not a per-user table -- and 0002_rls_policies.sql only ever gave
-- authenticated users SELECT on it (every write path before now was a
-- service-role job: the cron script, or an Edge Function). The new
-- Portfolio Refresh button (src/utils/data_provider_settings.py's
-- _log_portfolio_sync, called from _sync_dhan/_sync_zerodha) is the
-- first write from an ordinary authenticated user's own session --
-- confirmed live: postgrest.exceptions.APIError 42501 ("new row
-- violates row-level security policy for table provider_fetch_log").
--
-- Since there's no user_id to scope by, this policy is narrowed by
-- *value* instead: an authenticated user may only insert a row shaped
-- exactly like _log_portfolio_sync's own writes (fetch_type =
-- 'portfolio_sync', provider_name one of the two brokers) -- it can
-- never be used to spoof a fake 'price'/'fundamentals'/'fo' success
-- entry for the shared cron/Edge-Function-only fetch types.

create policy "authenticated insert own portfolio_sync fetch log"
    on provider_fetch_log for insert
    to authenticated
    with check (
        fetch_type = 'portfolio_sync'
        and provider_name in ('dhan', 'zerodha')
    );
