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
  source: string | null;
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
 * (see freshestRows). Returns null if there's no priceable PE strike.
 * Still used unchanged by the Streamlit Options page's own "5% CSP"
 * section (Python-side only, no TypeScript caller) -- kept here, and
 * still exported/tested, purely for parity with that Python function
 * staying alive; dashboardMetricsRows below calls cspOtm instead. */
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

function lowestStrike<T extends { strikePrice: number }>(rows: T[]): T {
  return rows.reduce((best, r) => (r.strikePrice < best.strikePrice ? r : best));
}

function highestOf<T extends { strikePrice: number }>(rows: T[]): T {
  return rows.reduce((best, r) => (r.strikePrice > best.strikePrice ? r : best));
}

/** Mirrors fo_service.py::csp_otm_for_rows -- a CSP strike/ROI pick for
 * an arbitrary OTM target `pct` (not fixed at 5% like cspFivePct above),
 * used by dashboardMetricsRows for the Screener for CSP page's
 * term-scaled near/next/far columns (5%/7%/10%). `peRows` should already
 * be filtered to one symbol + one expiry.
 *
 * Selection rule is deliberately not cspFivePct's "nearest either side"
 * -- confirmed with the user: picks the **highest strike that is still
 * <= target** (`target = spot * (1 - pct/100)`), i.e. the nearest strike
 * *below* the target. Mirrors ccFivePct's floor-filter exactly, flipped
 * to the put side (`<=` instead of `>=`, highest of the qualifying
 * candidates instead of lowest). Falls back to the single **lowest**
 * available strike if none qualify -- the symmetric mirror of
 * ccFivePct's fallback-to-highest. Prefers freshest-dated strikes (see
 * freshestRows). Returns null if there's no priceable PE strike.
 *
 * Real bug caught by its own test suite: `target = spot * (1 -
 * pct/100)` is a floating-point value that can land a hair below a
 * strike it's mathematically meant to exactly equal (confirmed:
 * `1000 * (1 - 7/100) === 929.9999999999999`, not `930`) -- a bare
 * `strikePrice <= target` then wrongly excludes that strike. CSP_OTM_EPS
 * absorbs that drift; real strike intervals are always far larger. */
const CSP_OTM_EPS = 1e-6;

export function cspOtm(peRows: OptionLegRow[], spot: number, pct: number, expiryDate: string): CspResult | null {
  const nearRows = peRows.filter((r) => r.optionType === "PE");
  if (nearRows.length === 0) return null;

  const target = spot * (1 - pct / 100);
  const freshest = freshestRows(nearRows);
  const otmEnough = freshest.filter((r) => r.strikePrice <= target + CSP_OTM_EPS);
  const bestRow = otmEnough.length > 0 ? highestOf(otmEnough) : lowestStrike(freshest);
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

const CSP_OTM_PCT_BY_RANK = [5.0, 7.0, 10.0]; // near, next, far -- see dashboardMetricsRows

/** Mirrors fo_service.py::dashboard_metrics_rows -- for each symbol with
 * a spot price and open option legs, computes CSP (via cspOtm) / "5% CC"
 * (via ccFivePct, unchanged) for each of that symbol's **up to 3
 * nearest distinct expiries** (near/next/far) and emits one flat row per
 * (symbol, expiryDate). A symbol with no spot or no option legs gets
 * zero rows (there's no expiryDate to key a row on); a symbol with fewer
 * than 3 expiries just gets fewer rows. cspPct/ccPct are null
 * independently of each other when either calculation has no priceable
 * result for that specific expiry. ccFivePct's assignmentProfitPct is
 * deliberately NOT cached here -- the Dashboard only ever displays
 * ccPct; the Options screen's "Assignment Profit" figure is computed
 * live instead.
 *
 * **CSP's OTM target is rank-based, not flat 5% for every expiry** --
 * confirmed with the user: CSP_OTM_PCT_BY_RANK = [5, 7, 10], indexed by
 * each expiry's own rank (0=near, 1=next, 2=far) among that symbol's
 * sorted expiries, via cspOtm (nearest-strike-*below*-target, not
 * cspFivePct's nearest-either-side -- see cspOtm's own docstring). ccPct/
 * ccStrike are unaffected -- CC's computation stays the original flat
 * 5%/floor-filter ccFivePct, since CC is no longer displayed on the
 * Streamlit Dashboard at all (only tracked here for the Python side's
 * refresh_bar.py::_dhan_fo_universe live-quote-refresh bookkeeping).
 *
 * BSE-sourced legs (source starting with "bse_fo_bhavcopy") are excluded
 * entirely -- BSE's F&O feed is index-options only, and this cache backs
 * a Nifty50 *stock* screener, so a BSE leg is either an index (irrelevant
 * to any screener row) or a pre-restriction stale stock contract that
 * happens to still be open because its own expiry hasn't passed yet.
 * Confirmed live: a stock symbol's stale BSE monthly expiry, a few days
 * off its real NSE one, produced a second same-month entry in what used
 * to be the Dashboard's "Options month" dropdown (since removed -- all 3
 * expiries now show at once). Rows without a source (older fixtures, or
 * the mock provider's single "mock_fo" source) pass through unaffected. */
export function dashboardMetricsRows(
  optionRows: OptionLegRow[],
  spotBySymbol: Record<string, number | null>,
): DashboardMetricsRow[] {
  const legsBySymbol = new Map<string, OptionLegRow[]>();
  for (const r of optionRows) {
    if ((r.source ?? "").startsWith("bse_fo_bhavcopy")) continue;
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

    expiries.forEach((expiry, rank) => {
      const expiryRows = legs.filter((r) => r.expiryDate === expiry);
      const pct = CSP_OTM_PCT_BY_RANK[rank];
      const csp = cspOtm(expiryRows, spot, pct, expiry);
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
    });
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
      .select("symbol,expiry_date,strike_price,option_type,trade_date,last_price,close,settlement_price,source")
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
        source: r.source ?? null,
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

/** Deletes every row in the cache -- called at the *start* of
 * recomputeDashboardMetrics, immediately followed by upserting the
 * freshly computed full set back (a true replace, not an incremental
 * upsert). Mirrors fo_repo.py::clear_dashboard_fo_metrics; see its
 * docstring for why this replaced an expiry-only prune (a row can stop
 * belonging in the cache for reasons other than its own expiry passing
 * -- e.g. dashboardMetricsRows excluding a BSE-sourced leg -- and an
 * expiry-only prune left such rows lingering forever). */
async function clearDashboardMetrics(serviceClient: AnyClient): Promise<void> {
  const { error } = await serviceClient.from("dashboard_fo_metrics").delete().gte("expiry_date", "1900-01-01");
  if (error) throw new Error(`dashboard_fo_metrics clear: ${error.message}`);
}

/** Spot prices for every registered company, read directly from
 * daily_screener_snapshots -- deliberately **not** latest_screener_view.
 * Every caller of recomputeDashboardMetrics runs under the service-role
 * client (both Edge Functions), and that view's per-user
 * portfolio-symbol widening (migration 0013) keys off auth.uid(), which
 * is null under service-role -- so it silently falls back to only the 50
 * Nifty50 constituents no matter which user's portfolio a symbol came
 * from. Confirmed live: latest_screener_view returned only 50 rows under
 * the service client even though daily_screener_snapshots had real rows
 * for portfolio-only symbols like Hindustan Zinc, so those symbols could
 * never get a dashboard_fo_metrics row at all. Mirrors
 * fo_service.py::recompute_dashboard_metrics' identical fix (same
 * pattern snapshot_repo.get_latest_prices already uses for the
 * Portfolio page). */
async function fetchSpotBySymbol(serviceClient: AnyClient): Promise<Record<string, number | null>> {
  const { data: companies, error: companiesErr } = await serviceClient.from("companies").select("symbol");
  if (companiesErr) throw new Error(`companies read: ${companiesErr.message}`);
  const symbols = (companies ?? []).map((c: any) => c.symbol as string);
  if (symbols.length === 0) return {};

  const { data: snapshotRows, error: snapshotErr } = await serviceClient
    .from("daily_screener_snapshots")
    .select("symbol, latest_price")
    .in("symbol", symbols)
    .order("snapshot_date", { ascending: false });
  if (snapshotErr) throw new Error(`daily_screener_snapshots read: ${snapshotErr.message}`);

  const spotBySymbol: Record<string, number | null> = {};
  for (const r of (snapshotRows ?? []) as any[]) {
    if (r.symbol in spotBySymbol) continue; // already have this symbol's latest (newest-first order)
    spotBySymbol[r.symbol] = r.latest_price === null || r.latest_price === undefined ? null : Number(r.latest_price);
  }
  return spotBySymbol;
}

/** The entrypoint both manual-refresh and fo-refresh call as their final
 * step: reads spot prices + open option legs (latest_option_chain_view),
 * recomputes CSP/CC for every symbol, and upserts the whole
 * dashboard_fo_metrics cache. Returns the row count for logging.
 * `serviceClient` must be service-role (bypasses RLS to write), same as
 * every other write in these Edge Functions. */
export async function recomputeDashboardMetrics(serviceClient: AnyClient): Promise<number> {
  const spotBySymbol = await fetchSpotBySymbol(serviceClient);

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

  await clearDashboardMetrics(serviceClient);
  await upsertChunked(serviceClient, "dashboard_fo_metrics", payload, "symbol,expiry_date");
  return payload.length;
}
