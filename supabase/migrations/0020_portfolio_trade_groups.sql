-- 0020_portfolio_trade_groups.sql
-- Manual "Trade" grouping overrides for F&O positions (pages/6_Portfolio.py's
-- Positions section). Default grouping (no row here) is one Trade per
-- underlying symbol; a row here means the user manually combined this leg
-- into -- or split it out of -- a Trade, keyed by the leg's own natural
-- identity (portfolio_name, broker, raw_name -- the exact instrument string
-- a broker's export already gives one specific contract), NOT by any
-- portfolio_positions row id. That's deliberate: replace_broker_positions
-- does a full delete-then-reinsert on every upload/sync, so a row keyed to
-- an ephemeral position id would be wiped on the very next refresh. Keying
-- by raw_name instead means this table is untouched by that delete+reinsert
-- and the manual grouping survives every future refresh, exactly like
-- broker_connections already survives a holdings re-upload.
--
-- Caveat worth knowing: if a broker ever changes how it formats raw_name for
-- the same contract (or the same portfolio/broker pair is later synced from
-- a different source), a previously grouped leg's override row simply won't
-- match anything anymore and that leg silently falls back to the default
-- per-symbol grouping -- nothing breaks, but the manual grouping would need
-- to be redone for that leg.
create table if not exists portfolio_trade_groups (
    user_id        uuid not null references auth.users(id) on delete cascade,
    portfolio_name text not null,
    broker         text not null,
    raw_name       text not null,
    trade_id       text not null,
    updated_at     timestamptz not null default now(),
    primary key (user_id, portfolio_name, broker, raw_name)
);

alter table portfolio_trade_groups enable row level security;

create policy "user manages own portfolio_trade_groups"
    on portfolio_trade_groups for all
    to authenticated
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);
