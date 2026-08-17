-- 0021_portfolio_trade_meta.sql
-- Trade-level overrides for the "My Trades" / "Analyse Trade" pages: a
-- corrected underlying label (e.g. the system resolved "Tata Motors" but
-- the real underlying is "Tata Motors Passenger Vehicle" post-demerger --
-- free text, not constrained to a known symbol) and a user-given trade
-- type (free text, defaults to "Trade"). Deliberately a SEPARATE table
-- from portfolio_trade_groups (migration 0020): that table keys a manual
-- grouping override per LEG (user_id, portfolio_name, broker, raw_name) --
-- this one stores metadata per TRADE (user_id, portfolio_name, trade_id),
-- a different grain entirely (many legs share one trade_id).
--
-- Same caveat as portfolio_trade_groups: if a trade is renamed via a merge
-- (its trade_id changes to the target trade's id), any existing meta row
-- here stays keyed to the OLD trade_id and simply stops applying -- no
-- auto-carry-forward. Not a bug: the target trade_id being merged into
-- already has its own (possibly already-customized) meta row, and blindly
-- overwriting it with the source trade's label/type would be more
-- surprising than a clean "re-enter it if you still want it".
create table if not exists portfolio_trade_meta (
    user_id           uuid not null references auth.users(id) on delete cascade,
    portfolio_name    text not null,
    trade_id          text not null,
    underlying_label  text,
    trade_type        text not null default 'Trade',
    updated_at        timestamptz not null default now(),
    primary key (user_id, portfolio_name, trade_id)
);

alter table portfolio_trade_meta enable row level security;

create policy "user manages own portfolio_trade_meta"
    on portfolio_trade_meta for all
    to authenticated
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);
