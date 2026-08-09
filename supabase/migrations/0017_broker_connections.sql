-- 0017_broker_connections.sql
-- Per-user, per-portfolio broker API credentials -- lets a portfolio pull
-- holdings/positions directly from a broker's API ("Connect Dhan account"
-- in pages/6_Portfolio.py) instead of a CSV upload. Dhan only, for now:
-- Zerodha's API needs a separate paid Kite Connect subscription and isn't
-- wired up.
--
-- access_token is stored in plaintext, protected only by the RLS policy
-- below (auth.uid() = user_id) -- the same protection model as every other
-- per-user table in this app (portfolio_holdings, user_settings, ...), no
-- extra application-level encryption. This token can also place trades on
-- Dhan (there is no read-only scope for an individual account) even though
-- this app only ever calls the read-only Holdings/Positions/Market Quote
-- endpoints with it -- that's an app-level promise, not something Dhan
-- enforces at the token level. It also expires after 24 hours (a Dhan
-- platform limit), so token_saved_at exists purely to let the UI warn the
-- user it's likely stale -- there is no background refresh of it.
create table if not exists broker_connections (
    user_id        uuid not null references auth.users(id) on delete cascade,
    portfolio_name text not null,
    broker         text not null,
    client_id      text not null,
    access_token   text not null,
    token_saved_at timestamptz not null default now(),
    primary key (user_id, portfolio_name, broker)
);

alter table broker_connections enable row level security;

create policy "user manages own broker_connections"
    on broker_connections for all
    to authenticated
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);
