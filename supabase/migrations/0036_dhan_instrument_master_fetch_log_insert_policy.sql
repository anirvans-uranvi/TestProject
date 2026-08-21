-- 0036_dhan_instrument_master_fetch_log_insert_policy.sql
--
-- Same bug as 0034, missed there because dhan_instrument_master wasn't a
-- fetch_type yet: migration 0035's instrument-master loaders
-- (src/data_providers/dhan_provider.py's _load_instrument_master/
-- _load_fo_instrument_master) log a provider_fetch_log row after a fresh
-- download, from a user's own authenticated Streamlit session -- 0034's
-- INSERT policy only covers fetch_type='portfolio_sync', so this hit the
-- same RLS violation (confirmed live, APIError 42501) the very first time
-- an account's instrument-master cache was stale/missing.
--
-- Additive, not a replacement of 0034's policy -- Postgres OR's multiple
-- permissive policies together, so this only widens what's allowed, same
-- narrow by-value scoping approach (no user_id to scope by on this shared
-- table): fetch_type='dhan_instrument_master' and provider_name one of
-- the two loaders actually use ('equity'/'fo').

create policy "authenticated insert own dhan_instrument_master fetch log"
    on provider_fetch_log for insert
    to authenticated
    with check (
        fetch_type = 'dhan_instrument_master'
        and provider_name in ('equity', 'fo')
    );
