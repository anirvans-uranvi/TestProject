// Direct TypeScript port of the "5% CSP" / "5% ITM PMCC" calculations in
// src/services/fo_service.py (csp_5pct_map, csp_5pct_for_rows,
// itm_pmcc_5pct_map, _freshest_rows), plus recomputeDashboardMetrics --
// the entrypoint that reads the same two Postgres views the Python side
// reads (latest_screener_view for spot, latest_option_chain_view for
// option legs) and (re)writes dashboard_fo_metrics (migration
// 0009_add_dashboard_fo_metrics.sql), the Dashboard's precomputed cache.
//
// Lives in _shared/ (Supabase Edge Functions convention -- an
// underscore-prefixed folder is bundled into whichever function imports
// it, but never deployed as a function of its own) because BOTH
// manual-refresh (spot can change) and fo-refresh (option data can
// change) need to trigger this same recompute as their final step.
//
// IMPORTANT: this is a second copy of business logic that lives in
// Python (src/services/fo_service.py). If you change the CSP/PMCC
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

export interface PmccResult {
  itmCeStrike: number;
  otmCeStrike: number;
  buyCePrice: number;
  sellPePrice: number;
  sellCePrice: number;
  netCredit: number;
  pmccPct: number;
  spot: number;
  expiryDate: string;
  buyCeTradeDate: string | null;
  sellPeTradeDate: string | null;
  sellCeTradeDate: string | null;
}

/** Mirrors fo_service.py::itm_pmcc_5pct_map's per-symbol body -- `legRows`
 * should already be filtered to one symbol + one expiry (both CE and PE
 * legs). Builds the "5% ITM PMCC": buy the ITM CE closest to spot
 * (preferring freshest-dated strikes, falling back to a stale one only
 * if no fresh strike is actually ITM), sell the PE at that same strike,
 * sell the CE nearest 5% below the bought strike. Returns null if any
 * leg is missing. */
export function itmPmccFivePct(legRows: OptionLegRow[], spot: number, expiryDate: string): PmccResult | null {
  const ceRows = legRows.filter((r) => r.optionType === "CE");
  const peRows = legRows.filter((r) => r.optionType === "PE");
  if (ceRows.length === 0 || peRows.length === 0) return null;

  const freshCeRows = freshestRows(ceRows);
  let itmCandidates = freshCeRows.filter((r) => r.strikePrice < spot);
  if (itmCandidates.length === 0) {
    itmCandidates = ceRows.filter((r) => r.strikePrice < spot);
  }
  if (itmCandidates.length === 0) return null;

  const buyCe = itmCandidates.reduce((best, r) => (r.strikePrice > best.strikePrice ? r : best));
  const itmStrike = buyCe.strikePrice;
  const buyCePrice = legPrice(buyCe);

  const peSameStrike = peRows.filter((r) => r.strikePrice === itmStrike);
  if (peSameStrike.length === 0) return null;
  const sellPePrice = legPrice(peSameStrike[0]);

  const target = itmStrike * 0.95;
  const sellCe = nearestByStrike(freshCeRows, target);
  const otmStrike = sellCe.strikePrice;
  const sellCePrice = legPrice(sellCe);

  if (buyCePrice === null || sellPePrice === null || sellCePrice === null || !itmStrike) return null;

  const netCredit = sellPePrice + sellCePrice - buyCePrice;
  const pmccPct = (netCredit / itmStrike) * 100;

  return {
    itmCeStrike: itmStrike,
    otmCeStrike: otmStrike,
    buyCePrice,
    sellPePrice,
    sellCePrice,
    netCredit,
    pmccPct,
    spot,
    expiryDate,
    buyCeTradeDate: buyCe.tradeDate,
    sellPeTradeDate: peSameStrike[0].tradeDate,
    sellCeTradeDate: sellCe.tradeDate,
  };
}

export interface DashboardMetricsRow {
  symbol: string;
  cspStrike: number | null;
  cspPutPrice: number | null;
  cspPct: number | null;
  cspSpot: number | null;
  cspExpiryDate: string | null;
  cspPutTradeDate: string | null;
  pmccItmCeStrike: number | null;
  pmccOtmCeStrike: number | null;
  pmccBuyCePrice: number | null;
  pmccSellPePrice: number | null;
  pmccSellCePrice: number | null;
  pmccNetCredit: number | null;
  pmccPct: number | null;
  pmccSpot: number | null;
  pmccExpiryDate: string | null;
  pmccBuyCeTradeDate: string | null;
  pmccSellPeTradeDate: string | null;
  pmccSellCeTradeDate: string | null;
}

function nearestExpiry(rows: OptionLegRow[]): string | null {
  const expiries = rows.map((r) => r.expiryDate).filter((e) => !!e);
  if (expiries.length === 0) return null;
  return expiries.reduce((a, b) => (b < a ? b : a));
}

/** Mirrors fo_service.py::dashboard_metrics_rows -- merges cspFivePct and
 * itmPmccFivePct into one flat row per symbol (every symbol in
 * spotBySymbol gets a row; a symbol missing CSP/PMCC data just has those
 * fields left null, same "N/A" contract as the Python version). CSP's
 * nearest expiry is picked from that symbol's PE legs only and PMCC's
 * from all its CE+PE legs, independently -- exactly mirroring
 * csp_5pct_map / itm_pmcc_5pct_map's separate grouping (in practice CE
 * and PE always share the same expiry cycle per symbol, so this rarely
 * diverges, but the port stays faithful to the source rather than
 * assuming that). */
export function dashboardMetricsRows(
  optionRows: OptionLegRow[],
  spotBySymbol: Record<string, number | null>,
): DashboardMetricsRow[] {
  const allLegsBySymbol = new Map<string, OptionLegRow[]>();
  const peLegsBySymbol = new Map<string, OptionLegRow[]>();
  for (const r of optionRows) {
    if (!allLegsBySymbol.has(r.symbol)) allLegsBySymbol.set(r.symbol, []);
    allLegsBySymbol.get(r.symbol)!.push(r);
    if (r.optionType === "PE") {
      if (!peLegsBySymbol.has(r.symbol)) peLegsBySymbol.set(r.symbol, []);
      peLegsBySymbol.get(r.symbol)!.push(r);
    }
  }

  const rows: DashboardMetricsRow[] = [];
  for (const symbol of Object.keys(spotBySymbol)) {
    const spot = spotBySymbol[symbol];
    let csp: CspResult | null = null;
    let pmcc: PmccResult | null = null;

    if (spot !== null) {
      const peLegs = peLegsBySymbol.get(symbol) ?? [];
      const cspExpiry = nearestExpiry(peLegs);
      if (cspExpiry) {
        csp = cspFivePct(peLegs.filter((r) => r.expiryDate === cspExpiry), spot, cspExpiry);
      }

      const allLegs = allLegsBySymbol.get(symbol) ?? [];
      const pmccExpiry = nearestExpiry(allLegs);
      if (pmccExpiry) {
        pmcc = itmPmccFivePct(allLegs.filter((r) => r.expiryDate === pmccExpiry), spot, pmccExpiry);
      }
    }

    rows.push({
      symbol,
      cspStrike: csp?.strike ?? null,
      cspPutPrice: csp?.putPrice ?? null,
      cspPct: csp?.cspPct ?? null,
      cspSpot: csp?.spot ?? null,
      cspExpiryDate: csp?.expiryDate ?? null,
      cspPutTradeDate: csp?.putTradeDate ?? null,
      pmccItmCeStrike: pmcc?.itmCeStrike ?? null,
      pmccOtmCeStrike: pmcc?.otmCeStrike ?? null,
      pmccBuyCePrice: pmcc?.buyCePrice ?? null,
      pmccSellPePrice: pmcc?.sellPePrice ?? null,
      pmccSellCePrice: pmcc?.sellCePrice ?? null,
      pmccNetCredit: pmcc?.netCredit ?? null,
      pmccPct: pmcc?.pmccPct ?? null,
      pmccSpot: pmcc?.spot ?? null,
      pmccExpiryDate: pmcc?.expiryDate ?? null,
      pmccBuyCeTradeDate: pmcc?.buyCeTradeDate ?? null,
      pmccSellPeTradeDate: pmcc?.sellPeTradeDate ?? null,
      pmccSellCeTradeDate: pmcc?.sellCeTradeDate ?? null,
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

/** The entrypoint both manual-refresh and fo-refresh call as their final
 * step: reads spot prices (latest_screener_view) + open option legs
 * (latest_option_chain_view), recomputes CSP/PMCC for every symbol, and
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
    csp_strike: r.cspStrike,
    csp_put_price: r.cspPutPrice,
    csp_pct: r.cspPct,
    csp_spot: r.cspSpot,
    csp_expiry_date: r.cspExpiryDate,
    csp_put_trade_date: r.cspPutTradeDate,
    pmcc_itm_ce_strike: r.pmccItmCeStrike,
    pmcc_otm_ce_strike: r.pmccOtmCeStrike,
    pmcc_buy_ce_price: r.pmccBuyCePrice,
    pmcc_sell_pe_price: r.pmccSellPePrice,
    pmcc_sell_ce_price: r.pmccSellCePrice,
    pmcc_net_credit: r.pmccNetCredit,
    pmcc_pct: r.pmccPct,
    pmcc_spot: r.pmccSpot,
    pmcc_expiry_date: r.pmccExpiryDate,
    pmcc_buy_ce_trade_date: r.pmccBuyCeTradeDate,
    pmcc_sell_pe_trade_date: r.pmccSellPeTradeDate,
    pmcc_sell_ce_trade_date: r.pmccSellCeTradeDate,
  }));

  await upsertChunked(serviceClient, "dashboard_fo_metrics", payload, "symbol");
  return payload.length;
}
