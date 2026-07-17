// Direct TypeScript port of src/calculations/*.py and
// src/repositories/fundamentals_repo.py::carry_forward_fields, kept as
// pure functions (no I/O) for the same reason the Python originals are:
// deterministic, fast, no-mocking-required tests (calculations.test.ts).
//
// IMPORTANT: this is a second copy of business logic that lives in
// Python everywhere else in this project. If you change a rule in
// src/calculations/, mirror it here too -- there is no automated check
// that these two stay in sync. See docs/CODEBASE_GUIDE.md for why this
// duplication exists (Supabase Edge Functions run Deno/TypeScript, not
// Python) and what tradeoff was accepted in choosing it anyway.

export type ScreenerStatus = "green" | "amber" | "red" | "unavailable";

export interface DividendEvent {
  exDate: string; // "YYYY-MM-DD"
  amountPerShare: number;
}

export interface DataQuality {
  missingPrice: boolean;
  missingPe: boolean;
  missingPeg: boolean;
  missingDividendData: boolean;
  missingReturn1d: boolean;
  missingReturn5d: boolean;
  missingReturn20d: boolean;
  isStale: boolean;
}

export interface ClassificationResult {
  status: ScreenerStatus;
  criterionA: boolean | null;
  criterionB: boolean | null;
  criterionC: boolean | null;
  dataQuality: DataQuality;
}

// ---------------------------------------------------------------------
// returns.py
// ---------------------------------------------------------------------

/** ((latest / base) - 1) * 100, or null if either input is missing or
 * base is zero (undefined, not zero). Mirrors returns.py::pct_return. */
export function pctReturn(latest: number | null, base: number | null): number | null {
  if (latest === null || base === null || base === 0) return null;
  return ((latest / base) - 1) * 100;
}

/** historicalCloses must be ordered oldest -> newest, NOT including
 * today's live price. Mirrors returns.py::return_n_trading_days_ago. */
export function returnNTradingDaysAgo(
  latestPrice: number | null,
  historicalCloses: (number | null)[],
  n: number,
): number | null {
  if (n <= 0 || latestPrice === null) return null;
  if (historicalCloses.length < n) return null;
  const base = historicalCloses[historicalCloses.length - n];
  return pctReturn(latestPrice, base);
}

export function return1d(latestPrice: number | null, closes: (number | null)[]): number | null {
  return returnNTradingDaysAgo(latestPrice, closes, 1);
}
export function return5d(latestPrice: number | null, closes: (number | null)[]): number | null {
  return returnNTradingDaysAgo(latestPrice, closes, 5);
}
export function return20d(latestPrice: number | null, closes: (number | null)[]): number | null {
  return returnNTradingDaysAgo(latestPrice, closes, 20);
}

// ---------------------------------------------------------------------
// dividends.py
// ---------------------------------------------------------------------

const TTM_WINDOW_DAYS = 365;

function isoDateMinusDays(isoDate: string, days: number): string {
  const d = new Date(`${isoDate}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() - days);
  return d.toISOString().slice(0, 10);
}

/** Mirrors dividends.py::ttm_dividend_sum. An empty list sums to 0 --
 * that's a legitimately-zero payout, not missing data (see
 * classification note on criterion A below). */
export function ttmDividendSum(
  events: DividendEvent[],
  asOfDate: string,
  windowDays: number = TTM_WINDOW_DAYS,
): number {
  const windowStart = isoDateMinusDays(asOfDate, windowDays);
  return events
    .filter((e) => e.exDate >= windowStart && e.exDate <= asOfDate)
    .reduce((sum, e) => sum + e.amountPerShare, 0);
}

/** Mirrors dividends.py::ttm_dividend_yield. Returns null only when the
 * yield is mathematically undefined (no price), never for zero
 * dividends. */
export function ttmDividendYield(
  events: DividendEvent[],
  asOfDate: string,
  latestPrice: number | null,
  windowDays: number = TTM_WINDOW_DAYS,
): number | null {
  if (latestPrice === null || latestPrice <= 0) return null;
  const total = ttmDividendSum(events, asOfDate, windowDays);
  return (total / latestPrice) * 100;
}

// ---------------------------------------------------------------------
// classification.py
// ---------------------------------------------------------------------

/** A = TTM dividend yield > threshold (default 3%). Mirrors
 * classification.py::criterion_a. */
export function criterionA(ttmDividendYield: number | null, threshold = 3.0): boolean | null {
  if (ttmDividendYield === null) return null;
  return ttmDividendYield > threshold;
}

/** B = 1D, 5D, and 20D returns all strictly > 0% (exactly 0% fails).
 * Mirrors classification.py::criterion_b. */
export function criterionB(
  return1dVal: number | null,
  return5dVal: number | null,
  return20dVal: number | null,
): boolean | null {
  if (return1dVal === null || return5dVal === null || return20dVal === null) return null;
  return return1dVal > 0 && return5dVal > 0 && return20dVal > 0;
}

/** C = PEG <= threshold (default 1.0) -- passes AT OR BELOW, the
 * opposite direction from A and B, since a lower PEG is the
 * conventionally desirable side. Mirrors classification.py::criterion_c. */
export function criterionC(pegRatio: number | null, threshold = 1.0): boolean | null {
  if (pegRatio === null) return null;
  return pegRatio <= threshold;
}

/** Display-only proximity check (not part of the Green/Amber/Red engine
 * above): passes when price is comfortably below its 52-week high, i.e.
 * latestPrice < threshold * week52High. Mirrors
 * classification.py::criterion_52w_high. */
export function criterion52wHigh(latestPrice: number | null, week52High: number | null, threshold = 0.9): boolean | null {
  if (latestPrice === null || week52High === null) return null;
  return latestPrice < threshold * week52High;
}

/** Display-only proximity check (not part of the Green/Amber/Red engine
 * above): passes when price has moved comfortably above its 52-week
 * low, i.e. latestPrice > threshold * week52Low. Mirrors
 * classification.py::criterion_52w_low. */
export function criterion52wLow(latestPrice: number | null, week52Low: number | null, threshold = 1.1): boolean | null {
  if (latestPrice === null || week52Low === null) return null;
  return latestPrice > threshold * week52Low;
}

/** Missing (null) criteria always short-circuit to "unavailable" before
 * pass/fail counting -- never conflated with a failed criterion. Mirrors
 * classification.py::classify. */
export function classify(
  a: boolean | null,
  b: boolean | null,
  c: boolean | null,
  isStale = false,
): ScreenerStatus {
  if (isStale || a === null || b === null || c === null) return "unavailable";
  const passed = [a, b, c].filter((v) => v).length;
  if (passed === 3) return "green";
  if (passed === 0) return "red";
  return "amber";
}

/** Mirrors classification.py::build_classification. */
export function buildClassification(params: {
  ttmDividendYield: number | null;
  return1d: number | null;
  return5d: number | null;
  return20d: number | null;
  pegRatio: number | null;
  isStale?: boolean;
  dividendYieldThreshold?: number;
  pegThreshold?: number;
  latestPrice?: number | null;
  peRatio?: number | null;
}): ClassificationResult {
  const {
    ttmDividendYield: ttmYield,
    return1d: r1,
    return5d: r5,
    return20d: r20,
    pegRatio,
    isStale = false,
    dividendYieldThreshold = 3.0,
    pegThreshold = 1.0,
    latestPrice = null,
    peRatio = null,
  } = params;

  const a = criterionA(ttmYield, dividendYieldThreshold);
  const b = criterionB(r1, r5, r20);
  const c = criterionC(pegRatio, pegThreshold);
  const status = classify(a, b, c, isStale);

  return {
    status,
    criterionA: a,
    criterionB: b,
    criterionC: c,
    dataQuality: {
      missingPrice: latestPrice === null,
      missingPe: peRatio === null,
      missingPeg: pegRatio === null,
      missingDividendData: ttmYield === null,
      missingReturn1d: r1 === null,
      missingReturn5d: r5 === null,
      missingReturn20d: r20 === null,
      isStale,
    },
  };
}

// ---------------------------------------------------------------------
// fundamentals_repo.py::carry_forward_fields
// ---------------------------------------------------------------------

export interface FundamentalsRow {
  peRatio: number | null;
  pegRatio: number | null;
  eps: number | null;
  marketCap: number | null;
  week52High: number | null;
  week52Low: number | null;
}

const CARRY_FORWARD_FIELDS: (keyof FundamentalsRow)[] = [
  "peRatio", "pegRatio", "eps", "marketCap", "week52High", "week52Low",
];

/** rows must be ordered newest-first. Carries each field forward
 * independently from the most recent row where it was actually
 * non-null, instead of taking a single row's values wholesale -- a
 * same-day fetch commonly has gaps (e.g. Yahoo's PEG is intermittently
 * null) even when an older snapshot has a real value. Mirrors
 * fundamentals_repo.py::carry_forward_fields exactly. */
export function carryForwardFields(rows: Partial<FundamentalsRow>[]): FundamentalsRow {
  const carried: FundamentalsRow = {
    peRatio: null, pegRatio: null, eps: null, marketCap: null, week52High: null, week52Low: null,
  };
  for (const row of rows) {
    for (const field of CARRY_FORWARD_FIELDS) {
      const value = row[field];
      if (carried[field] === null && value !== null && value !== undefined) {
        carried[field] = value;
      }
    }
    if (CARRY_FORWARD_FIELDS.every((f) => carried[f] !== null)) break;
  }
  return carried;
}
