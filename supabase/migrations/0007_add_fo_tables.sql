-- 0007_add_fo_tables.sql
-- Futures & Options (F&O) derivatives data for the 50 Nifty constituents.
--
-- Source of truth is the NSE F&O UDiFF bhavcopy (one zip per trading day):
--   https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_YYYYMMDD_F_0000.csv.zip
-- yfinance carries NO NSE derivatives, and NSE's live option-chain API
-- returns hollow JSON to non-interactive sessions -- so the bhavcopy is the
-- only reliable free source. It is END-OF-DAY: "latest price" here means the
-- most recent trading day's close/settlement, not an intraday live quote.
--
-- Design: futures and options are separate instruments (options carry a
-- strike + CE/PE and there are ~200/symbol vs ~3 futures/symbol), so each
-- gets its own pair of tables: a *contract dimension* (the open-contracts
-- registry, with expiry) and a flat *daily-price fact* table (OHLC history),
-- mirroring the split the user asked for. Greeks / implied volatility are
-- intentionally NOT stored -- they are not published in the bhavcopy (or any
-- free source) and were scoped out; the fact tables can gain those columns
-- later without disturbing this shape.
--
-- Fact tables follow the flat natural-key style of price_history (0001), and
-- all four tables use the shared-market-data RLS pattern from 0002:
-- authenticated users read, and only the service-role key (which bypasses
-- RLS) writes, so no write policies are defined here.

-- ---------------------------------------------------------------------
-- Futures
-- ---------------------------------------------------------------------

create table if not exists futures_contracts (
    id               bigint generated always as identity primary key,
    symbol           text not null references companies(symbol) on delete cascade,
    expiry_date      date not null,
    contract_name    text,
    nse_token        text,
    lot_size         int,
    is_open          boolean not null default true,
    first_seen_date  date,
    last_seen_date   date,
    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now(),
    unique (symbol, expiry_date)
);

create index if not exists idx_futures_contracts_symbol
    on futures_contracts (symbol, expiry_date);
create index if not exists idx_futures_contracts_open
    on futures_contracts (symbol, expiry_date) where is_open;

create table if not exists futures_daily_prices (
    id               bigint generated always as identity primary key,
    symbol           text not null references companies(symbol) on delete cascade,
    expiry_date      date not null,
    trade_date       date not null,
    open             numeric(14,4),
    high             numeric(14,4),
    low              numeric(14,4),
    close            numeric(14,4),
    last_price       numeric(14,4),
    prev_close       numeric(14,4),
    settlement_price numeric(14,4),
    underlying_price numeric(14,4),
    open_interest    bigint,
    change_in_oi     bigint,
    volume           bigint,
    turnover         numeric(20,2),
    num_trades       bigint,
    source           text not null default 'unknown',
    created_at       timestamptz not null default now(),
    unique (symbol, expiry_date, trade_date)
);

create index if not exists idx_futures_daily_symbol_expiry_date
    on futures_daily_prices (symbol, expiry_date, trade_date desc);

-- ---------------------------------------------------------------------
-- Options
-- ---------------------------------------------------------------------

create table if not exists option_contracts (
    id               bigint generated always as identity primary key,
    symbol           text not null references companies(symbol) on delete cascade,
    expiry_date      date not null,
    strike_price     numeric(14,4) not null,
    option_type      text not null check (option_type in ('CE', 'PE')),
    contract_name    text,
    nse_token        text,
    lot_size         int,
    is_open          boolean not null default true,
    first_seen_date  date,
    last_seen_date   date,
    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now(),
    unique (symbol, expiry_date, strike_price, option_type)
);

create index if not exists idx_option_contracts_symbol
    on option_contracts (symbol, expiry_date, strike_price);
create index if not exists idx_option_contracts_open
    on option_contracts (symbol, expiry_date) where is_open;

create table if not exists option_daily_prices (
    id               bigint generated always as identity primary key,
    symbol           text not null references companies(symbol) on delete cascade,
    expiry_date      date not null,
    strike_price     numeric(14,4) not null,
    option_type      text not null check (option_type in ('CE', 'PE')),
    trade_date       date not null,
    open             numeric(14,4),
    high             numeric(14,4),
    low              numeric(14,4),
    close            numeric(14,4),
    last_price       numeric(14,4),
    prev_close       numeric(14,4),
    settlement_price numeric(14,4),
    underlying_price numeric(14,4),
    open_interest    bigint,
    change_in_oi     bigint,
    volume           bigint,
    turnover         numeric(20,2),
    num_trades       bigint,
    source           text not null default 'unknown',
    created_at       timestamptz not null default now(),
    unique (symbol, expiry_date, strike_price, option_type, trade_date)
);

create index if not exists idx_option_daily_symbol_expiry_date
    on option_daily_prices (symbol, expiry_date, trade_date desc);
create index if not exists idx_option_daily_symbol_expiry_strike
    on option_daily_prices (symbol, expiry_date, strike_price);

-- ---------------------------------------------------------------------
-- "Latest" views: newest daily row per open contract, so pages load the
-- current term structure / option chain in one query (mirrors
-- latest_screener_view in 0003). security_invoker => underlying-table RLS
-- still applies to the querying user.
-- ---------------------------------------------------------------------

create or replace view latest_futures_view
with (security_invoker = true)
as
select distinct on (p.symbol, p.expiry_date)
    p.symbol,
    p.expiry_date,
    p.trade_date,
    p.open,
    p.high,
    p.low,
    p.close,
    p.last_price,
    p.prev_close,
    p.settlement_price,
    p.underlying_price,
    p.open_interest,
    p.change_in_oi,
    p.volume,
    p.turnover,
    p.num_trades,
    fc.lot_size,
    fc.contract_name,
    fc.is_open
from futures_daily_prices p
join futures_contracts fc
    on fc.symbol = p.symbol and fc.expiry_date = p.expiry_date
where fc.is_open
order by p.symbol, p.expiry_date, p.trade_date desc;

grant select on latest_futures_view to authenticated;

create or replace view latest_option_chain_view
with (security_invoker = true)
as
select distinct on (p.symbol, p.expiry_date, p.strike_price, p.option_type)
    p.symbol,
    p.expiry_date,
    p.strike_price,
    p.option_type,
    p.trade_date,
    p.open,
    p.high,
    p.low,
    p.close,
    p.last_price,
    p.prev_close,
    p.settlement_price,
    p.underlying_price,
    p.open_interest,
    p.change_in_oi,
    p.volume,
    p.turnover,
    p.num_trades,
    oc.lot_size,
    oc.contract_name,
    oc.is_open
from option_daily_prices p
join option_contracts oc
    on oc.symbol = p.symbol and oc.expiry_date = p.expiry_date
   and oc.strike_price = p.strike_price and oc.option_type = p.option_type
where oc.is_open
order by p.symbol, p.expiry_date, p.strike_price, p.option_type, p.trade_date desc;

grant select on latest_option_chain_view to authenticated;

-- ---------------------------------------------------------------------
-- RLS: shared market data -- authenticated read; writes via service-role
-- key only (same pattern as price_history etc. in 0002_rls_policies.sql).
-- ---------------------------------------------------------------------

alter table futures_contracts enable row level security;
alter table futures_daily_prices enable row level security;
alter table option_contracts enable row level security;
alter table option_daily_prices enable row level security;

create policy "authenticated read futures_contracts"
    on futures_contracts for select to authenticated using (true);

create policy "authenticated read futures_daily_prices"
    on futures_daily_prices for select to authenticated using (true);

create policy "authenticated read option_contracts"
    on option_contracts for select to authenticated using (true);

create policy "authenticated read option_daily_prices"
    on option_daily_prices for select to authenticated using (true);
