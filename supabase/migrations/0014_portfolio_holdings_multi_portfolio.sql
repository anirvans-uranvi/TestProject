-- 0014_portfolio_holdings_multi_portfolio.sql
--
-- Lets a user maintain multiple, independently-named portfolios that
-- coexist side by side (each shown in its own tab on the Portfolio page)
-- instead of a single implicit portfolio per user. Adds `portfolio_name`
-- and widens the primary key from (user_id, broker, raw_name) to
-- (user_id, portfolio_name, broker, raw_name) -- the same broker's same
-- raw instrument name can now be saved once per distinct portfolio
-- without colliding.
--
-- Existing rows (all belonging to whatever was previously "the" portfolio)
-- get portfolio_name = 'Portfolio 1' via the column default, so they keep
-- showing up under a real tab after this migration rather than vanishing.

alter table portfolio_holdings
    add column if not exists portfolio_name text not null default 'Portfolio 1';

alter table portfolio_holdings drop constraint if exists portfolio_holdings_pkey;
alter table portfolio_holdings add primary key (user_id, portfolio_name, broker, raw_name);
