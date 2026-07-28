// Direct TypeScript port of the "5% CSP" / "5% CC" calculations in
// src/services/fo_service.py (csp_5pct_map, csp_5pct_for_rows,
// cc_5pct_map, cc_5pct_for_rows, _freshest_rows), plus
// recomputeDashboardMetrics -- the entrypoint that reads the same two
// Postgres views the Python side reads (latest_screener_view for spot,
// latest_option_chain_view for option legs) and (re)writes
// dashboard_fo_metrics (migration 0011_dashboard_cc_5pct.sql), the
// Dashboard's precomputed cache, one row per (symbol, expiry_date).
//
// Lives in _shared/ (Supabase Edge Functions convention -- an
// underscore-prefixed folder is bundled into whichever function imports
// it, but never deployed as a function of its own) because BOTH
// manual-refresh (spot can change) and fo-refresh (option data can
// change) need to trigger this same recompute as their final step.
//
// IMPORTANT: this is a second copy of business logic that lives in
// Python (src/services/fo_service.py). If you change the CSP/CC
// calculation there, mirror it here too -- there is no automated check
// that these two stay in sync. Same accepted tradeoff as
// manual-refresh/calculations.ts and fo-refresh/bhavcopy.ts (see
// docs/CODEBASE_GUIDE.md): Streamlit can never hold the service-role key
// needed to write this cache itself, so the on-demand refresh path has
// to reimplement the write side in TypeScript.

// deno-lint-ignore no-explicit-any
type AnyClient = any;

export interface OptionLegRow {
  symbol: string;
  expiryDate: string; // "YYYY-MM-DD"
  strikePrice: number;
  optionType: "CE" | "PE";
  tradeDate: string | null;
  lastPrice: number | null;
  close: number | null;
  settlementPrice: number | null;
}

// Mirrors Python's `a or b or c` chain exactly, including falsy-zero
// fallthrough (a price of 0 is never real in this domain, so this
// matches fo_service.py's behavior rather than "fixing" it here).
function firstTruthy(...values: (number | null)[]): number | null {
  for (const v of values) {
    if (v) return v;
  }
  return null;
}

function legPrice(r: OptionLegRow): number | null {
  return firstTruthy(r.lastPrice, r.close, r.settlementPrice);
}

/** Mirrors fo_service.py::_freshest_rows -- restricts to the rows whose
 * tradeDate matches the most recent tradeDate present, or returns `rows`
 * unchanged if none of them carry one at all (e.g. hand-built test
 * fixtures). See that function's docstring for the real staleness bug
 * this guards against (an illiquid strike's "latest" row can be weeks
 * older than its liquid neighbors'). */
export function freshestRows<T extends { tradeDate: string | null }>(rows: T[]): T[] {
  const dates = rows.map((r) => r.tradeDate).filter((d): d is string => d !== null);
  if (dates.length === 0) return rows;
  const freshest = dates.reduce((a, b) => (b > a ? b : a));
  return rows.filter((r) => r.tradeDate === freshest);
}

function nearestByStrike<T extends { strikePrice: number }>(rows: T[], target: number): T {
  return rows.reduce((best, r) => (Math.abs(r.strikePrice - target) < Math.abs(best.strikePrice - target) ? r : best));
}

export interface CspResult {
  strike: number;
  putPrice: number | null;
  cspPct: number | null;
  spot: number;
  expiryDate: string;
  putTradeDate: string | null;
}

/** Mirrors fo_service.py::csp_5pct_for_rows -- `peRows` should already be
 * filtered to one symbol + one expiry (any CE legs mixed in are
 * ignored). "5% CSP": the premium for the strike nearest 5% below spot,
 * as a percentage of that strike, preferring the freshest-dated strikes
 * (see freshestRows). Returns null if there's no priceable PE strike. */
export function cspFivePct(peRows: OptionLegRow[], spot: number, expiryDate: string): CspResult | null {
  const nearRows = peRows.filter((r) => r.optionType === "PE");
  if (nearRows.length === 0) return null;

  const target = spot * 0.95;
  const bestRow = nearestByStrike(freshestRows(nearRows), target);
  const strike = bestRow.strikePrice;
  const putPrice = legPrice(bestRow);
  const cspPct = putPrice !== null && strike ? (putPrice / strike) * 100 : null;

  return { strike, putPrice, cspPct, spot, expiryDate, putTradeDate: bestRow.tradeDate };
}

export interface CcResult {
  strike: number;
  premium: number | null;
  grossInvestment: number;
  netInvestment: number | null;
  ccPct: number | null;
  assignmentProfitPct: number | null;
  spot: number;
  expiryDate: string;
  tradeDate: string | null;
}

function highestStrike<T extends { strikePrice: number }>(rows: T[]): T {
  return rows.reduce((best, r) => (r.strikePrice > best.strikePrice ? r : best));
}

/** Mirrors fo_service.py::cc_5pct_for_rows -- `ceRows` should already be
 * filtered to one symbol + one expiry (any PE legs mixed in are
 * ignored). "5% CC" is a covered-call yield: sell 1 lot of the OTM call
 * whose strike is the *lowest one at or above* 5% above spot (`target =
 * spot * 1.05`) -- i.e. the cheapest strike that genuinely satisfies "at
 * least 5% OTM", not merely the strike nearest to target in absolute
 * distance (which could round down to a strike below the 5% line).
 * Falls back to the single highest available strike if none reach the
 * 5% line at all. Prefers freshest-dated strikes among the candidates
 * (see freshestRows).
 *
 * - grossInvestment = spot -- the cost of buying 1 share at the current
 *   price.
 * - netInvestment = grossInvestment - premium -- the premium collected
 *   up front reduces the real capital outlay for the covered-call
 *   position.
 * - ccPct = premium / grossInvestment * 100 -- the premium alone as a
 *   yield on the stock's own price.
 * - assignmentProfitPct = (strike / netInvestment - 1) * 100 -- if
 *   assigned, the seller receives `strike` per share for a position that
 *   only cost `netInvestment` per share, so this is the total return of
 *   the whole covered-call trade if called away; null if netInvestment
 *   is zero or negative (premium >= spot), not a divide-by-zero.
 * Returns null if there's no priceable CE strike. */
export function ccFivePct(ceRows: OptionLegRow[], spot: number, expiryDate: string): CcResult | null {
  const nearRows = ceRows.filter((r) => r.optionType === "CE");
  if (nearRows.length === 0) return null;

  const target = spot * 1.05;
  const freshest = freshestRows(nearRows);
  const otmEnough = freshest.filter((r) => r.strikePrice >= target);
  const bestRow = otmEnough.length > 0 ? nearestByStrike(otmEnough, target) : highestStrike(freshest);
  const strike = bestRow.strikePrice;
  const premium = legPrice(bestRow);
  const grossInvestment = spot;
  const netInvestment = premium !== null ? grossInvestment - premium : null;
  const ccPct = premium !== null && grossInvestment ? (premium / grossInvestment) * 100 : null;
  const assignmentProfitPct = netInvestment !== null && netInvestment > 0 ? (strike / netInvestment - 1) * 100 : null;

  return { strike, premium, grossInvestment, netInvestment, ccPct, assignmentProfitPct, spot, expiryDate, tradeDate: bestRow.tradeDate };
}

export interface DashboardMetricsRow {
  symbol: string;
  expiryDate: string;
  spot: number;
  cspStrike: number | null;
  cspPutPrice: number | null;
  cspPct: number | null;
  cspPutTradeDate: string | null;
  ccStrike: number | null;
  ccPremium: number | null;
  ccPct: number | null;
  ccTradeDate: string | null;
}

/** Mirrors fo_service.py::dashboard_metrics_rows -- for each symbol with
 * a spot price and open option legs, computes "5% CSP" / "5% CC" for
 * each of that symbol's **up to 3 nearest distinct expiries**
 * (near/next/far) and emits one flat row per (symbol, expiryDate). A
 * symbol with no spot or no option legs gets zero rows (there's no
 * expiryDate to key a row on); a symbol with fewer than 3 expiries just
 * gets fewer rows. cspPct/ccPct are null independently of each other
 * when either calculation has no priceable result for that specific
 * expiry. ccFivePct's assignmentProfitPct is deliberately NOT cached
 * here -- the Dashboard only ever displays ccPct; the Options screen's
 * "Assignment Profit" figure is computed live instead. */
export function dashboardMetricsRows(
  optionRows: OptionLegRow[],
  spotBySymbol: Record<string, number | null>,
): DashboardMetricsRow[] {
  const legsBySymbol = new Map<string, OptionLegRow[]>();
  for (const r of optionRows) {
    if (!legsBySymbol.has(r.symbol)) legsBySymbol.set(r.symbol, []);
    legsBySymbol.get(r.symbol)!.push(r);
  }

  const rows: DashboardMetricsRow[] = [];
  for (const [symbol, spot] of Object.entries(spotBySymbol)) {
    if (spot === null) continue;
    const legs = legsBySymbol.get(symbol);
    if (!legs || legs.length === 0) continue;

    const expiries = [...new Set(legs.map((r) => r.expiryDate).filter((e) => !!e))]
      .sort()
      .slice(0, 3);

    for (const expiry of expiries) {
      const expiryRows = legs.filter((r) => r.expiryDate === expiry);
      const csp = cspFivePct(expiryRows, spot, expiry);
      const cc = ccFivePct(expiryRows, spot, expiry);

      rows.push({
        symbol,
        expiryDate: expiry,
        spot,
        cspStrike: csp?.strike ?? null,
        cspPutPrice: csp?.putPrice ?? null,
        cspPct: csp?.cspPct ?? null,
        cspPutTradeDate: csp?.putTradeDate ?? null,
        ccStrike: cc?.strike ?? null,
        ccPremium: cc?.premium ?? null,
        ccPct: cc?.ccPct ?? null,
        ccTradeDate: cc?.tradeDate ?? null,
      });
    }
  }
  return rows;
}

const PAGE_SIZE = 1000;

/** PostgREST caps a single response at a server-configured max (commonly
 * 1000 rows) -- fo_repo.py::_paginate's docstring documents a real bug
 * this exact omission caused (get_all_open_options silently truncated to
 * 1000 rows, dropping most of the universe). Paginated here for the same
 * reason. */
async function fetchAllOpenOptionLegs(serviceClient: AnyClient): Promise<OptionLegRow[]> {
  const rows: OptionLegRow[] = [];
  let offset = 0;
  // deno-lint-ignore no-explicit-any
  for (;;) {
    const { data, error } = await serviceClient
      .from("latest_option_chain_view")
      .select("symbol,expiry_date,strike_price,option_type,trade_date,last_price,close,settlement_price")
      .range(offset, offset + PAGE_SIZE - 1);
    if (error) throw new Error(`latest_option_chain_view read: ${error.message}`);
    const page = (data ?? []) as any[];
    for (const r of page) {
      rows.push({
        symbol: r.symbol,
        expiryDate: r.expiry_date,
        strikePrice: Number(r.strike_price),
        optionType: r.option_type,
        tradeDate: r.trade_date ?? null,
        lastPrice: r.last_price === null || r.last_price === undefined ? null : Number(r.last_price),
        close: r.close === null || r.close === undefined ? null : Number(r.close),
        settlementPrice: r.settlement_price === null || r.settlement_price === undefined ? null : Number(r.settlement_price),
      });
    }
    if (page.length < PAGE_SIZE) break;
    offset += PAGE_SIZE;
  }
  return rows;
}

function chunk<T>(items: T[], size: number): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < items.length; i += size) out.push(items.slice(i, i + size));
  return out;
}

async function upsertChunked(client: AnyClient, table: string, rows: unknown[], onConflict: string): Promise<void> {
  for (const batch of chunk(rows, 500)) {
    if (batch.length === 0) continue;
    const { error } = await client.from(table).upsert(batch, { onConflict });
    if (error) throw new Error(`${table} upsert: ${error.message}`);
  }
}

/** dashboardMetricsRows only ever emits a symbol's *current* up-to-3
 * nearest expiries -- a month that just expired stops being emitted, but
 * its old cached row from a previous run is never otherwise deleted, so
 * it would linger in the Dashboard's "Options month" dropdown forever.
 * Mirrors fo_repo.py::delete_expired_dashboard_fo_metrics. */
async function deleteExpiredDashboardMetrics(serviceClient: AnyClient, asOfIso: string): Promise<void> {
  const { error } = await serviceClient.from("dashboard_fo_metrics").delete().lt("expiry_date", asOfIso);
  if (error) throw new Error(`dashboard_fo_metrics prune: ${error.message}`);
}

/** The entrypoint both manual-refresh and fo-refresh call as their final
 * step: reads spot prices (latest_screener_view) + open option legs
 * (latest_option_chain_view), recomputes CSP/CC for every symbol, and
 * upserts the whole dashboard_fo_metrics cache. Returns the row count for
 * logging. `serviceClient` must be service-role (bypasses RLS to write),
 * same as every other write in these Edge Functions. */
export async function recomputeDashboardMetrics(serviceClient: AnyClient): Promise<number> {
  const { data: screenerRows, error: screenerErr } = await serviceClient
    .from("latest_screener_view")
    .select("symbol,latest_price");
  if (screenerErr) throw new Error(`latest_screener_view read: ${screenerErr.message}`);

  const spotBySymbol: Record<string, number | null> = {};
  for (const r of (screenerRows ?? []) as any[]) {
    spotBySymbol[r.symbol] = r.latest_price === null || r.latest_price === undefined ? null : Number(r.latest_price);
  }

  const optionRows = await fetchAllOpenOptionLegs(serviceClient);
  const rows = dashboardMetricsRows(optionRows, spotBySymbol);

  const payload = rows.map((r) => ({
    symbol: r.symbol,
    expiry_date: r.expiryDate,
    spot: r.spot,
    csp_strike: r.cspStrike,
    csp_put_price: r.cspPutPrice,
    csp_pct: r.cspPct,
    csp_put_trade_date: r.cspPutTradeDate,
    cc_strike: r.ccStrike,
    cc_premium: r.ccPremium,
    cc_pct: r.ccPct,
    cc_trade_date: r.ccTradeDate,
  }));

  await upsertChunked(serviceClient, "dashboard_fo_metrics", payload, "symbol,expiry_date");
  await deleteExpiredDashboardMetrics(serviceClient, new Date().toISOString().slice(0, 10));
  return payload.length;
}
