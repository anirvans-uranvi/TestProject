-- 0030_user_live_prices.sql
--
-- Per-user cache of live stock LTPs pulled from whichever broker the
-- account's "Data Provider" setting (migration 0028) points at -- written
-- by the "Market Data Refresh" button (src/utils/refresh_bar.py) only
-- when that setting is 'dhan'/'zerodha', read by Dashboard/Stock Detail
-- as an override on top of the shared daily_screener_snapshots value
-- (the same {**shared, **live} merge pattern src/utils/portfolio_page.py
-- already uses for the portfolio pages' own broker-live LTP).
--
-- A cache, not a live-on-every-render fetch: re-hitting a broker's quote
-- API on every Dashboard/Stock Detail page view (for up to ~50+ symbols)
-- isn't worth the added latency/rate-limit exposure for what's still a
-- fundamentally daily-refresh-oriented app -- "Market Data Refresh"
-- already exists as the one moment a user expects to wait for fresh
-- data.
create table if not exists user_live_prices (
    user_id      uuid not null references auth.users(id) on delete cascade,
    symbol       text not null,
    latest_price numeric(12, 2) not null,
    fetched_at   timestamptz not null default now(),
    primary key (user_id, symbol)
);

alter table user_live_prices enable row level security;

create policy "user manages own user_live_prices"
    on user_live_prices for all
    to authenticated
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);
