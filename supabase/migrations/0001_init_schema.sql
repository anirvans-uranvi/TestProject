-- 0001_init_schema.sql
-- Core schema for the Nifty 50 Momentum & Dividend Screener.
-- Raw/normalized market data lives separately from calculated daily
-- snapshots so the classification audit trail (daily_screener_snapshots)
-- never has to be recomputed from history to explain a past status.

create extension if not exists "pgcrypto";

-- ---------------------------------------------------------------------
-- Reference data
-- ---------------------------------------------------------------------

create table if not exists nifty50_constituents (
    id                 uuid primary key default gen_random_uuid(),
    symbol             text not null,
    company_name       text not null,
    sector             text,
    index_effective_from date not null,
    index_effective_to   date,
    is_current         boolean not null default true,
    created_at         timestamptz not null default now(),
    unique (symbol, index_effective_from)
);

create index if not exists idx_nifty50_constituents_current
    on nifty50_constituents (is_current) where is_current;

create table if not exists companies (
    symbol      text primary key,
    name        text not null,
    sector      text,
    industry    text,
    isin        text,
    updated_at  timestamptz not null default now()
);

-- ---------------------------------------------------------------------
-- Raw/normalized market data
-- ---------------------------------------------------------------------

create table if not exists price_history (
    id              bigint generated always as identity primary key,
    symbol          text not null references companies(symbol) on delete cascade,
    trade_date      date not null,
    open            numeric(14,4),
    high            numeric(14,4),
    low             numeric(14,4),
    close           numeric(14,4),
    adjusted_close  numeric(14,4),
    volume          bigint,
    source          text not null default 'unknown',
    created_at      timestamptz not null default now(),
    unique (symbol, trade_date)
);

create index if not exists idx_price_history_symbol_date
    on price_history (symbol, trade_date desc);

create table if not exists fundamental_snapshots (
    id           bigint generated always as identity primary key,
    symbol       text not null references companies(symbol) on delete cascade,
    as_of_date   date not null,
    pe_ratio     numeric(10,4),
    peg_ratio    numeric(10,4),
    eps          numeric(14,4),
    market_cap   numeric(20,2),
    source       text not null default 'unknown',
    is_stale     boolean not null default false,
    created_at   timestamptz not null default now(),
    unique (symbol, as_of_date)
);

create index if not exists idx_fundamental_snapshots_symbol_date
    on fundamental_snapshots (symbol, as_of_date desc);

create table if not exists dividend_events (
    id                bigint generated always as identity primary key,
    symbol            text not null references companies(symbol) on delete cascade,
    ex_date           date not null,
    amount_per_share  numeric(10,4) not null,
    dividend_type     text not null default 'final' check (dividend_type in ('interim', 'final', 'special')),
    source            text not null default 'unknown',
    created_at        timestamptz not null default now(),
    unique (symbol, ex_date, amount_per_share)
);

create index if not exists idx_dividend_events_symbol_exdate
    on dividend_events (symbol, ex_date desc);

-- ---------------------------------------------------------------------
-- Calculated daily snapshots (audit trail for classification history)
-- ---------------------------------------------------------------------

create table if not exists daily_screener_snapshots (
    id                  bigint generated always as identity primary key,
    symbol              text not null references companies(symbol) on delete cascade,
    snapshot_date       date not null,
    latest_price        numeric(14,4),
    return_1d           numeric(10,4),
    return_5d           numeric(10,4),
    return_20d          numeric(10,4),
    ttm_dividend_yield  numeric(10,4),
    pe_ratio            numeric(10,4),
    peg_ratio           numeric(10,4),
    criterion_a         boolean,
    criterion_b         boolean,
    criterion_c         boolean,
    status              text not null check (status in ('green', 'amber', 'red', 'unavailable')),
    data_quality        jsonb not null default '{}'::jsonb,
    created_at          timestamptz not null default now(),
    unique (symbol, snapshot_date)
);

create index if not exists idx_daily_screener_snapshots_date
    on daily_screener_snapshots (snapshot_date desc);
create index if not exists idx_daily_screener_snapshots_symbol_date
    on daily_screener_snapshots (symbol, snapshot_date desc);

-- ---------------------------------------------------------------------
-- Per-user data
-- ---------------------------------------------------------------------

create table if not exists user_settings (
    user_id                        uuid primary key references auth.users(id) on delete cascade,
    dividend_yield_threshold       numeric(6,2) not null default 3.0,
    peg_threshold                  numeric(6,2) not null default 1.0,
    stale_data_threshold_minutes   int not null default 30,
    theme                          text not null default 'system' check (theme in ('light', 'dark', 'system')),
    updated_at                     timestamptz not null default now()
);

create table if not exists saved_filters (
    id           uuid primary key default gen_random_uuid(),
    user_id      uuid not null references auth.users(id) on delete cascade,
    name         text not null,
    filter_json  jsonb not null default '{}'::jsonb,
    created_at   timestamptz not null default now(),
    unique (user_id, name)
);

-- User-saved entry/target/stop-loss/notes per symbol, used for risk/reward
-- display and to seed Buy Watch / Sell Watch alert configs.
create table if not exists user_positions (
    id                  uuid primary key default gen_random_uuid(),
    user_id             uuid not null references auth.users(id) on delete cascade,
    symbol              text not null references companies(symbol) on delete cascade,
    entry_price         numeric(14,4),
    target_price        numeric(14,4),
    stop_loss           numeric(14,4),
    notes               text,
    holding_period_days int,
    updated_at          timestamptz not null default now(),
    unique (user_id, symbol)
);

create table if not exists alerts (
    id                  uuid primary key default gen_random_uuid(),
    user_id             uuid not null references auth.users(id) on delete cascade,
    symbol              text references companies(symbol) on delete cascade,
    alert_type          text not null check (alert_type in (
                            'status_change', 'enters_green', 'leaves_green',
                            'price_cross', 'momentum_cross', 'dividend_yield_cross',
                            'peg_cross', 'buy_watch', 'sell_watch', 'refresh_failure'
                        )),
    config              jsonb not null default '{}'::jsonb,
    is_active           boolean not null default true,
    cooldown_minutes    int not null default 60,
    last_triggered_at   timestamptz,
    created_at          timestamptz not null default now()
);

create index if not exists idx_alerts_user_active
    on alerts (user_id, is_active);
create index if not exists idx_alerts_symbol
    on alerts (symbol);

create table if not exists notification_log (
    id            uuid primary key default gen_random_uuid(),
    alert_id      uuid references alerts(id) on delete set null,
    user_id       uuid not null references auth.users(id) on delete cascade,
    symbol        text,
    message       text not null,
    payload       jsonb not null default '{}'::jsonb,
    channel       text not null default 'in_app',
    triggered_at  timestamptz not null default now(),
    dedupe_key    text not null unique,
    read_at       timestamptz
);

create index if not exists idx_notification_log_user_triggered
    on notification_log (user_id, triggered_at desc);

-- ---------------------------------------------------------------------
-- Operational logging
-- ---------------------------------------------------------------------

create table if not exists provider_fetch_log (
    id              bigint generated always as identity primary key,
    provider_name   text not null,
    fetch_type      text not null check (fetch_type in (
                        'price', 'intraday_price', 'fundamentals', 'dividend', 'constituents'
                    )),
    symbol          text,
    status          text not null check (status in ('success', 'failure')),
    error_message   text,
    retry_count     int not null default 0,
    started_at      timestamptz not null,
    finished_at     timestamptz,
    created_at      timestamptz not null default now()
);

create index if not exists idx_provider_fetch_log_started
    on provider_fetch_log (started_at desc);
create index if not exists idx_provider_fetch_log_status
    on provider_fetch_log (status, started_at desc);
