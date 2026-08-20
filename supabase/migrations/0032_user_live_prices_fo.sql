-- 0032_user_live_prices_fo.sql
--
-- Widens user_live_prices (migration 0030, previously equity-only,
-- primary key (user_id, symbol)) to also hold live F&O contract LTP --
-- futures and options -- and ETF LTP (ETFs already fit the existing
-- equity shape, no new column needed for them).
--
-- One row shape covers everything via a natural key mirroring
-- option_contracts/option_daily_prices' own (symbol, expiry_date,
-- strike_price, option_type) key, already used elsewhere in this
-- codebase (see migration 0007, 0031): `option_type = 'EQ'` (the
-- default) for a plain equity/ETF row -- identical to every row this
-- table has held since 0030 -- `option_type = 'FUT'` for a futures
-- contract (real expiry_date, sentinel strike_price), and `option_type`
-- in ('CE', 'PE') for an option contract (real expiry_date and
-- strike_price). Postgres primary keys cannot contain NULL in any
-- column, so expiry_date/strike_price get NOT NULL sentinel defaults
-- ('1900-01-01' / 0) rather than being nullable.
--
-- Written by src/utils/refresh_bar.py's "Market Data Refresh" -- Dhan
-- only for now (see src/data_providers/dhan_provider.py's new
-- get_fo_quotes); Zerodha-connected accounts keep writing/reading only
-- option_type='EQ' rows, unchanged.

alter table user_live_prices
  add column expiry_date date not null default '1900-01-01',
  add column strike_price numeric(12, 2) not null default 0,
  add column option_type text not null default 'EQ'
    check (option_type in ('EQ', 'FUT', 'CE', 'PE'));

alter table user_live_prices drop constraint user_live_prices_pkey;
alter table user_live_prices
  add primary key (user_id, symbol, expiry_date, strike_price, option_type);
