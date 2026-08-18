-- 0026_portfolio_positions_ltp_as_of.sql
--
-- Adds `ltp_as_of date` to `portfolio_positions` -- set only when a
-- position's `ltp` came from this app's own end-of-day F&O data
-- (`portfolio_service.apply_fallback_option_ltp`) rather than a live
-- broker quote. `null` means "live" (Zerodha's own `last_price`, a
-- successful Dhan Market Quote call, or a CSV upload's own LTP column)
-- -- no caveat needed.
--
-- Real bug this fixes: a Dhan sync whose Market Quote call 401s (most
-- commonly because the account lacks the separate "Data APIs"
-- subscription) silently falls back to this app's own NSE F&O bhavcopy
-- close for *every* position in that sync, with no way for the user to
-- tell the difference from a live LTP -- confirmed live: a JioFin CSP
-- showed LTP 3.30 on My CSP while Dhan's own app showed a live 4.40 for
-- the same contract, a full trading day apart. `ltp_as_of` lets the UI
-- show "(as of <date>)" next to a fallback LTP, same convention the
-- Dashboard already uses for a stale screener price.

alter table portfolio_positions
    add column if not exists ltp_as_of date;
