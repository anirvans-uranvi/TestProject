-- 0038_portfolio_trade_fills.sql
--
-- Everything this app has synced from a broker so far (portfolio_holdings/
-- portfolio_positions) is a current-state snapshot only -- open quantity
-- and average price, no record of individual fills. This table backs the
-- new "Sync Trade History from Dhan" button (Settings ->
-- src/utils/data_provider_settings.py's _render_dhan_trade_history_sync)
-- and the new Trade History page's realized-P&L/trade-journal views
-- (src/services/portfolio_service.py's compute_realized_pnl,
-- pages/14_Trade_History.py).
--
-- Deliberately append-only, unlike portfolio_holdings/portfolio_positions'
-- delete-then-insert "replace" semantics: a historical fill must never
-- disappear just because a later sync's date range didn't happen to
-- include it again. Syncs upsert on (user_id, portfolio_name, broker,
-- exchange_trade_id) -- exchange_trade_id is the exchange's own stable,
-- unique-per-fill identifier, so re-syncing an overlapping date range is
-- always safe (same row, same values, no duplicate).
--
-- broker-agnostic shape (a `broker` column, like every other portfolio_*
-- table) even though only Dhan writes to it today -- costs nothing now
-- and avoids a schema change if Zerodha's own trade-history API is added
-- later. symbol/expiry_date/strike_price/option_type are nullable, same
-- convention as portfolio_positions, for a fill that couldn't be decoded
-- to a known contract.
create table if not exists portfolio_trade_fills (
    user_id             uuid not null references auth.users(id) on delete cascade,
    portfolio_name      text not null,
    broker              text not null,
    exchange_trade_id   text not null,
    order_id            text,
    raw_name            text not null,
    symbol              text,
    expiry_date         date,
    strike_price        numeric(12, 2),
    option_type         text,
    transaction_type    text not null check (transaction_type in ('BUY', 'SELL')),
    qty                 numeric(14, 2) not null,
    price               numeric(12, 2) not null,
    product_type        text,
    traded_at           timestamptz not null,
    brokerage           numeric(12, 2) not null default 0,
    taxes_and_charges   numeric(12, 2) not null default 0,
    synced_at           timestamptz not null default now(),
    primary key (user_id, portfolio_name, broker, exchange_trade_id)
);

alter table portfolio_trade_fills enable row level security;

create policy "user manages own portfolio_trade_fills"
    on portfolio_trade_fills for all
    to authenticated
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);
