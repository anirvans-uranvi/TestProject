-- 0002_rls_policies.sql
-- Shared/reference market data is readable by any authenticated user and
-- written only by server-side jobs using the service-role key (which
-- bypasses RLS entirely, so no write policies are defined for those
-- tables). Per-user tables are scoped strictly to auth.uid().

-- ---------------------------------------------------------------------
-- Shared reference & market-data tables: read-only to authenticated users
-- ---------------------------------------------------------------------

alter table nifty50_constituents enable row level security;
alter table companies enable row level security;
alter table price_history enable row level security;
alter table fundamental_snapshots enable row level security;
alter table dividend_events enable row level security;
alter table daily_screener_snapshots enable row level security;
alter table provider_fetch_log enable row level security;

create policy "authenticated read nifty50_constituents"
    on nifty50_constituents for select
    to authenticated
    using (true);

create policy "authenticated read companies"
    on companies for select
    to authenticated
    using (true);

create policy "authenticated read price_history"
    on price_history for select
    to authenticated
    using (true);

create policy "authenticated read fundamental_snapshots"
    on fundamental_snapshots for select
    to authenticated
    using (true);

create policy "authenticated read dividend_events"
    on dividend_events for select
    to authenticated
    using (true);

create policy "authenticated read daily_screener_snapshots"
    on daily_screener_snapshots for select
    to authenticated
    using (true);

create policy "authenticated read provider_fetch_log"
    on provider_fetch_log for select
    to authenticated
    using (true);

-- ---------------------------------------------------------------------
-- Per-user tables: full CRUD scoped to the owning user
-- ---------------------------------------------------------------------

alter table user_settings enable row level security;
alter table saved_filters enable row level security;
alter table user_positions enable row level security;
alter table alerts enable row level security;
alter table notification_log enable row level security;

create policy "user manages own settings"
    on user_settings for all
    to authenticated
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

create policy "user manages own saved_filters"
    on saved_filters for all
    to authenticated
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

create policy "user manages own positions"
    on user_positions for all
    to authenticated
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

create policy "user manages own alerts"
    on alerts for all
    to authenticated
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

create policy "user reads own notifications"
    on notification_log for select
    to authenticated
    using (auth.uid() = user_id);

create policy "user updates own notifications"
    on notification_log for update
    to authenticated
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

-- notification_log inserts happen server-side via the service-role key
-- (alert_service writes on behalf of users), so no authenticated insert
-- policy is defined here by design.
