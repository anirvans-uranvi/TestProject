-- 0029_broker_connections_account_wide.sql
--
-- Collapses broker_connections from per-(user_id, portfolio_name, broker)
-- to per-(user_id, broker) -- on request, once "Data Provider" became an
-- account-wide choice (migration 0028) rather than a per-portfolio one.
-- A user now has at most one live Dhan connection and one live Zerodha
-- connection, period, same credential used both to sync holdings/
-- positions and to price Dashboard/Stock Detail when that provider is
-- selected -- not something scoped to an individual named portfolio
-- anymore (see pages/6_My_Broker.py's removal, folded into Settings'
-- "Data Provider" section).
--
-- Before dropping portfolio_name from the key, keep only the most
-- recently-saved row per (user_id, broker) -- a user could have had the
-- same broker connected under two different portfolio names before this
-- migration; there is no meaningful way to keep both once the key no
-- longer includes portfolio_name, so the newer one wins.
delete from broker_connections bc where exists (
    select 1 from broker_connections newer
    where newer.user_id = bc.user_id
      and newer.broker = bc.broker
      and newer.token_saved_at > bc.token_saved_at
);

alter table broker_connections drop constraint broker_connections_pkey;
alter table broker_connections drop column portfolio_name;
alter table broker_connections add primary key (user_id, broker);
