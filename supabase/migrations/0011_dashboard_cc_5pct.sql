-- 0011_dashboard_cc_5pct.sql
-- Replaces the Dashboard's "5% ITM PMCC" column with "5% CC" -- a much
-- simpler covered-call yield (sell 1 OTM call 5% above spot, no ITM
-- call/PE legs at all), per request. Drops and recreates
-- dashboard_fo_metrics (same reasoning as 0010: pure derived cache, no
-- history worth an ALTER, fo_service.py's recompute_dashboard_metrics
-- rebuilds it wholesale on every refresh) with the pmcc_* columns
-- replaced by cc_* ones:
--   pmcc_itm_ce_strike, pmcc_otm_ce_strike, pmcc_buy_ce_price,
--   pmcc_sell_pe_price, pmcc_sell_ce_price, pmcc_net_credit, pmcc_pct,
--   pmcc_buy_ce_trade_date, pmcc_sell_pe_trade_date, pmcc_sell_ce_trade_date
-- become just:
--   cc_strike, cc_premium, cc_pct, cc_trade_date
-- (one CE leg instead of three legs across two option types). See
-- fo_service.py::cc_5pct_for_rows for the calculation itself.

drop table if exists dashboard_fo_metrics;

create table dashboard_fo_metrics (
    symbol             text not null references companies(symbol) on delete cascade,
    expiry_date        date not null,
    spot               numeric(14,4),
    csp_strike         numeric(14,4),
    csp_put_price      numeric(14,4),
    csp_pct            numeric(10,4),
    csp_put_trade_date date,
    cc_strike          numeric(14,4),
    cc_premium         numeric(14,4),
    cc_pct             numeric(10,4),
    cc_trade_date      date,
    computed_at        timestamptz not null default now(),
    primary key (symbol, expiry_date)
);

alter table dashboard_fo_metrics enable row level security;

create policy "authenticated read dashboard_fo_metrics"
    on dashboard_fo_metrics for select to authenticated using (true);
