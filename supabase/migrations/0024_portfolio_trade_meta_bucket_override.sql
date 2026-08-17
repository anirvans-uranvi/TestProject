-- 0024_portfolio_trade_meta_bucket_override.sql
--
-- Adds a manual override for which My Trades table (Stock/Index/Other) a
-- trade sorts into. Real bug this fixes: `classify_underlying_bucket`
-- previously treated ETF/Fund company_type the same as Index -- so gilt/
-- liquid/gold ETFs (BANKBEES, GILT5YBEES, GOLDBEES, LIQUIDCASE, ...) all
-- showed up in "Index Trades" alongside genuine index positions (NIFTY,
-- FINNIFTY), confirmed live via a real portfolio. Fixed in application
-- code (portfolio_service.classify_underlying_bucket now only treats
-- company_type = 'Index' as "index", ETF/Fund fall to the "stock"
-- default) -- but a real Index ETF someone deliberately wants tracked
-- alongside Index Trades (or any other bucket correction) needs a manual
-- escape hatch, same as underlying_label/trade_type already have. `null`
-- (the default) means "use the auto-computed bucket"; a non-null value
-- always wins over the computed default in group_into_trades.
alter table portfolio_trade_meta
    add column if not exists bucket_override text
    check (bucket_override in ('stock', 'index', 'other'));

notify pgrst, 'reload schema';
