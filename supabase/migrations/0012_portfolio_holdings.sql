-- 0012_portfolio_holdings.sql
-- Per-user portfolio holdings uploaded from broker CSV exports (Zerodha,
-- Dhan, ...). `symbol` is deliberately NOT a foreign key to companies:
-- a resolved symbol may not exist in `companies` yet (an ETF/fund or a
-- non-Nifty50 stock the screener doesn't track today) -- the refresh
-- pipeline registers a minimal companies row for it once seen here, so
-- forcing the FK at insert time would create a chicken-and-egg failure.
-- `symbol` is NULL when the uploaded row's instrument name couldn't be
-- matched to any known company at all; the page shows it unresolved
-- (N/A valuation) until the user supplies a symbol manually.
create table if not exists portfolio_holdings (
    user_id      uuid not null references auth.users(id) on delete cascade,
    broker       text not null,
    raw_name     text not null,
    symbol       text,
    qty          numeric(14,4) not null,
    avg_price    numeric(14,4) not null,
    investment   numeric(14,4) not null,
    uploaded_at  timestamptz not null default now(),
    primary key (user_id, broker, raw_name)
);

alter table portfolio_holdings enable row level security;

create policy "user manages own portfolio_holdings"
    on portfolio_holdings for all
    to authenticated
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);
