-- 0010_dashboard_fo_metrics_per_expiry.sql
-- Re-keys dashboard_fo_metrics from one row per symbol to one row per
-- (symbol, expiry_date), so the Dashboard can offer an "Options month"
-- dropdown (near/next/far -- currently Jul/Aug/Sep) that just selects
-- which already-cached row to display, with no live recomputation.
--
-- Drops and recreates rather than ALTERing: dashboard_fo_metrics (added
-- by migration 0009) is a pure derived cache with no history worth
-- preserving -- fo_service.py's recompute_dashboard_metrics (and its
-- TypeScript port) rebuilds it wholesale on every refresh, so there is
-- nothing to migrate row-by-row. This DOES mean the cache is empty until
-- the next refresh runs; the Dashboard already degrades to "N/A" for
-- these two columns when the table has no matching row, so this is a
-- harmless, temporary regression exactly like a fresh 0009 apply.
--
-- csp_spot/pmcc_spot and csp_expiry_date/pmcc_expiry_date (separate
-- columns in 0009) collapse into single spot/expiry_date columns here --
-- both metrics always shared the same spot (the cash-market price) and
-- now share the same expiry (the row's own key), so the duplication no
-- longer makes sense.

drop table if exists dashboard_fo_metrics;

create table dashboard_fo_metrics (
    symbol                   text not null references companies(symbol) on delete cascade,
    expiry_date              date not null,
    spot                     numeric(14,4),
    csp_strike               numeric(14,4),
    csp_put_price            numeric(14,4),
    csp_pct                  numeric(10,4),
    csp_put_trade_date       date,
    pmcc_itm_ce_strike       numeric(14,4),
    pmcc_otm_ce_strike       numeric(14,4),
    pmcc_buy_ce_price        numeric(14,4),
    pmcc_sell_pe_price       numeric(14,4),
    pmcc_sell_ce_price       numeric(14,4),
    pmcc_net_credit          numeric(14,4),
    pmcc_pct                 numeric(10,4),
    pmcc_buy_ce_trade_date   date,
    pmcc_sell_pe_trade_date  date,
    pmcc_sell_ce_trade_date  date,
    computed_at              timestamptz not null default now(),
    primary key (symbol, expiry_date)
);

alter table dashboard_fo_metrics enable row level security;

create policy "authenticated read dashboard_fo_metrics"
    on dashboard_fo_metrics for select to authenticated using (true);
