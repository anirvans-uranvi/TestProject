-- 0037_dhan_fo_instruments_exchange.sql
--
-- dhan_fo_instruments (migration 0035) never recorded which exchange
-- (NSE/BSE) each row's security_id actually lists on -- _download_fo_master
-- used it only to filter, then discarded it. That's fine for stock legs
-- (NSE-only, migration 0031), but 0035's own docstring already notes
-- index legs are cross-exchange (NIFTY/BANKNIFTY/FINNIFTY on NSE,
-- SENSEX/BANKEX on BSE). DhanProvider.get_fo_quotes queried every resolved
-- security_id under a single hardcoded "NSE_FNO" segment, which silently
-- returned no price for a BSE-listed one -- confirmed live: SENSEX/BANKEX
-- contracts resolved a real security_id (resolve_fo_security_id worked
-- fine) but never got a live quote, indistinguishable in the "Stock &
-- Option Data Refresh" summary from "Dhan has no matching contract".
--
-- Existing rows default to 'NSE' only as a bridge until the next refresh
-- (at most once per IST day) replaces the whole table wholesale -- same
-- delete-then-upsert convention as the rest of this cache -- with the
-- real per-row value from a fresh download.
alter table dhan_fo_instruments
    add column if not exists exchange text not null default 'NSE' check (exchange in ('NSE', 'BSE'));
