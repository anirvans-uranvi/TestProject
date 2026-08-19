-- 0028_user_settings_data_provider.sql
--
-- Per-user "Data Provider" choice (Settings page), replacing the
-- app-wide, env-var-only MARKET_DATA_PROVIDER setting for anything the
-- signed-in user personally views: Dashboard/Stock Detail's stock LTP,
-- and which broker (if any) the account's live sync pulls
-- holdings/positions from. Fundamentals (PEG/dividend) and the full F&O
-- chain are NOT provider-branched -- neither Dhan nor Zerodha's API
-- exposes that data, so those stay yfinance/NSE+BSE-bhavcopy-sourced
-- regardless of this setting; only stock LTP switches.
--
-- Defaults to 'yfinance_bhavcopy' -- today's behavior, unchanged for
-- every existing user until they actively opt into a broker.
alter table user_settings add column if not exists data_provider text
    not null default 'yfinance_bhavcopy'
    check (data_provider in ('dhan', 'zerodha', 'yfinance_bhavcopy'));
