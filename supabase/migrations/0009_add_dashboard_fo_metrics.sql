-- 0009_add_dashboard_fo_metrics.sql
-- Precomputed cache of the Dashboard's "5% CSP" / "5% ITM PMCC" columns.
--
-- Previously the Dashboard computed these on every page load: pull every
-- open option leg for all 50 symbols (thousands of rows) and run the
-- nearest-strike/freshness search in Python, in the request path. Every
-- other Dashboard column is already precomputed at refresh time into
-- daily_screener_snapshots -- this table gives the F&O columns the same
-- treatment: one small row per symbol, written by whichever refresh path
-- last touched spot price or F&O data (src/services/fo_service.py's
-- recompute_dashboard_metrics, and its TypeScript port in
-- supabase/functions/_shared/dashboardMetrics.ts), read directly by the
-- Dashboard instead of recomputed there.
--
-- One row per symbol, upserted wholesale on every recompute (not a
-- natural-key fact table like futures_daily_prices -- there's no history
-- to keep, just the current cached value). Same shared-market-data RLS
-- pattern as 0007: authenticated read, service-role-only writes.

create table if not exists dashboard_fo_metrics (
    symbol                   text primary key references companies(symbol) on delete cascade,
    csp_strike               numeric(14,4),
    csp_put_price            numeric(14,4),
    csp_pct                  numeric(10,4),
    csp_spot                 numeric(14,4),
    csp_expiry_date          date,
    csp_put_trade_date       date,
    pmcc_itm_ce_strike       numeric(14,4),
    pmcc_otm_ce_strike       numeric(14,4),
    pmcc_buy_ce_price        numeric(14,4),
    pmcc_sell_pe_price       numeric(14,4),
    pmcc_sell_ce_price       numeric(14,4),
    pmcc_net_credit          numeric(14,4),
    pmcc_pct                 numeric(10,4),
    pmcc_spot                numeric(14,4),
    pmcc_expiry_date         date,
    pmcc_buy_ce_trade_date   date,
    pmcc_sell_pe_trade_date  date,
    pmcc_sell_ce_trade_date  date,
    computed_at              timestamptz not null default now()
);

alter table dashboard_fo_metrics enable row level security;

create policy "authenticated read dashboard_fo_metrics"
    on dashboard_fo_metrics for select to authenticated using (true);
