-- 0016_portfolio_positions.sql
-- Per-user F&O positions uploaded from broker CSV exports (Zerodha, Dhan,
-- ...) -- sibling table to portfolio_holdings, but for open option (and,
-- longer-term, futures) positions rather than equity holdings. `qty` keeps
-- its broker-reported sign (negative = short, positive = long).
--
-- `symbol`/`expiry_date`/`strike_price`/`option_type` describe the decoded
-- contract and are all nullable together: a row whose instrument string
-- didn't match either broker's known encoding is still saved (raw_name is
-- always present) but shown with no contract detail, same "save it anyway,
-- degrade the display" philosophy as portfolio_holdings' unresolved rows.
-- Deliberately no FK to option_contracts -- NSE index derivatives (NIFTY,
-- BANKNIFTY, SENSEX, ...) are out of scope for this app's own F&O
-- ingestion (see src/data_providers/nse_fo_provider.py), so a decoded
-- position's contract may never exist in that table at all.
create table if not exists portfolio_positions (
    user_id        uuid not null references auth.users(id) on delete cascade,
    portfolio_name text not null,
    broker         text not null,
    raw_name       text not null,
    symbol         text,
    expiry_date    date,
    strike_price   numeric(14,4),
    option_type    text check (option_type in ('CE', 'PE')),
    qty            numeric(14,4) not null,
    avg_price      numeric(14,4) not null,
    ltp            numeric(14,4),
    uploaded_at    timestamptz not null default now(),
    primary key (user_id, portfolio_name, broker, raw_name)
);

alter table portfolio_positions enable row level security;

create policy "user manages own portfolio_positions"
    on portfolio_positions for all
    to authenticated
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);
