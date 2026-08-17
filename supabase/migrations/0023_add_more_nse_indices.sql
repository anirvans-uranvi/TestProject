-- 0023_add_more_nse_indices.sql
--
-- Adds FINNIFTY, MIDCPNIFTY, and NIFTYNXT50 as `company_type = 'Index'`
-- rows, alongside the four migrations 0018/0019 already seed (NIFTY,
-- BANKNIFTY, SENSEX, BANKEX). Confirmed live: a Dhan-synced FINNIFTY
-- option position (pages/6_My_Broker.py's "Connect Dhan account") showed
-- up in My Positions' "Stock Options" table instead of "Index Options" --
-- portfolio_service.classify_position_bucket only checks company_type,
-- and FINNIFTY had no `companies` row at all, so the lookup silently
-- fell through to the "stock" default. These three are NSE's other
-- currently-traded index F&O underlyings besides NIFTY/BANKNIFTY, so
-- they're seeded pre-emptively rather than one bug report at a time.
-- Like SENSEX before it, none of these get real F&O rows from this app's
-- own NSE bhavcopy ingestion unless nse_fo_provider.py's bhavcopy actually
-- carries them -- this migration only fixes symbol resolution/
-- classification, not data coverage.

insert into companies (symbol, name, company_type)
values
    ('FINNIFTY', 'Nifty Financial Services Index', 'Index'),
    ('MIDCPNIFTY', 'Nifty Midcap Select Index', 'Index'),
    ('NIFTYNXT50', 'Nifty Next 50 Index', 'Index')
on conflict (symbol) do update set company_type = excluded.company_type;
