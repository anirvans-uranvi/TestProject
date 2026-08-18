-- 0025_portfolio_position_meta.sql
--
-- Per-LEG metadata for My CSP (pages/11_My_CSP.py): `trade_date` (when
-- the position was actually entered -- no broker export or API this app
-- talks to reliably carries this for an already-open position, so it's
-- entered manually) and `stop_loss` (a ratcheting value the app itself
-- computes and saves back on every My CSP render -- see
-- portfolio_service.csp_stop_loss).
--
-- Deliberately a SEPARATE table from portfolio_trade_meta (migration
-- 0021), not an extra column on it: that table is keyed per *trade*
-- (portfolio_name, trade_id) -- but a Trade's default grouping is one
-- Trade per underlying symbol (see assign_trade_ids), so two CSPs on the
-- same underlying at different strikes/expiries/entry dates would
-- otherwise collide on one trade_id and be forced to share one trade
-- date and one stop loss. Keying this per-LEG instead
-- (user_id, portfolio_name, broker, raw_name -- the same natural leg
-- identity portfolio_trade_groups already uses, stable across
-- replace_broker_holdings/replace_broker_positions's delete-then-insert
-- sync) matches My CSP's own row granularity exactly: one row, one
-- leg, one entry date, one stop loss.
create table if not exists portfolio_position_meta (
    user_id        uuid not null references auth.users(id) on delete cascade,
    portfolio_name text not null,
    broker         text not null,
    raw_name       text not null,
    trade_date     date,
    stop_loss      numeric,
    updated_at     timestamptz not null default now(),
    primary key (user_id, portfolio_name, broker, raw_name)
);

alter table portfolio_position_meta enable row level security;

create policy "user manages own portfolio_position_meta"
    on portfolio_position_meta for all
    to authenticated
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);
