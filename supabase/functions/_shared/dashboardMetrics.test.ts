// Tests for dashboardMetrics.ts's CSP/CC port. Run with:
//   deno test supabase/functions/_shared/dashboardMetrics.test.ts
//
// Fixtures mirror tests/test_fo_service.py's TestCsp5PctForRows and
// TestCc5PctForRows exactly (same strikes/prices/trade_dates), so this
// checks the TypeScript port against the same behavior contract as the
// Python original.
import { assert, assertAlmostEquals, assertEquals } from "jsr:@std/assert@1";
import { ccFivePct, cspFivePct, cspOtm, dashboardMetricsRows, type OptionLegRow } from "./dashboardMetrics.ts";

function leg(overrides: Partial<OptionLegRow>): OptionLegRow {
  return {
    symbol: "RELIANCE",
    expiryDate: "2026-07-28",
    strikePrice: 0,
    optionType: "PE",
    tradeDate: null,
    lastPrice: null,
    close: null,
    settlementPrice: null,
    source: null,
    ...overrides,
  };
}

// --- cspFivePct ----------------------------------------------------------

Deno.test("cspFivePct: picks the strike nearest 5% below spot", () => {
  const rows = [
    leg({ strikePrice: 900.0, lastPrice: 5.0 }),
    leg({ strikePrice: 950.0, lastPrice: 25.0 }),
  ];
  const result = cspFivePct(rows, 1000.0, "2026-07-28");
  assertEquals(result?.strike, 950.0);
  assertEquals(result?.putPrice, 25.0);
  assertAlmostEquals(result!.cspPct!, (25.0 / 950.0) * 100);
  assertEquals(result?.spot, 1000.0);
  assertEquals(result?.expiryDate, "2026-07-28");
});

Deno.test("cspFivePct: echoes back the expiryDate argument, not a row field", () => {
  const rows = [leg({ strikePrice: 950.0, lastPrice: 25.0, expiryDate: "2026-08-25" })];
  const result = cspFivePct(rows, 1000.0, "2026-08-25");
  assertEquals(result?.expiryDate, "2026-08-25");
});

Deno.test("cspFivePct: no PE rows returns null", () => {
  const rows = [leg({ optionType: "CE", strikePrice: 950.0, lastPrice: 60.0 })];
  assertEquals(cspFivePct(rows, 1000.0, "2026-07-28"), null);
});

Deno.test("cspFivePct: empty rows returns null", () => {
  assertEquals(cspFivePct([], 1000.0, "2026-07-28"), null);
});

Deno.test("cspFivePct: prefers freshest trade_date over pure nearest-strike", () => {
  // spot 1000 -> target 950. Strike 950 is the literal nearest match but
  // hasn't traded since 2026-07-01 (illiquid); strike 900 is farther but
  // is the only strike from the freshest trade_date (2026-07-20).
  const rows = [
    leg({ strikePrice: 900.0, lastPrice: 5.0, tradeDate: "2026-07-20" }),
    leg({ strikePrice: 950.0, lastPrice: 25.0, tradeDate: "2026-07-01" }),
  ];
  const result = cspFivePct(rows, 1000.0, "2026-07-28");
  assertEquals(result?.strike, 900.0);
  assertEquals(result?.putTradeDate, "2026-07-20");
});

Deno.test("cspFivePct: no trade_date at all falls back to pure nearest-strike", () => {
  const rows = [
    leg({ strikePrice: 900.0, lastPrice: 5.0 }),
    leg({ strikePrice: 950.0, lastPrice: 25.0 }),
  ];
  const result = cspFivePct(rows, 1000.0, "2026-07-28");
  assertEquals(result?.strike, 950.0);
});

// --- cspOtm ----------------------------------------------------------------
// Mirrors tests/test_fo_service.py's TestCspOtmForRows.

Deno.test("cspOtm: picks the highest strike still <= target, not merely nearest", () => {
  // spot 1000, pct=5 -> target 950 -- exact match, both selection rules
  // would agree here (covered separately below where they diverge).
  const rows = [
    leg({ strikePrice: 900.0, lastPrice: 5.0 }),
    leg({ strikePrice: 950.0, lastPrice: 25.0 }),
  ];
  const result = cspOtm(rows, 1000.0, 5.0, "2026-07-28");
  assertEquals(result?.strike, 950.0);
  assertEquals(result?.putPrice, 25.0);
  assertAlmostEquals(result!.cspPct!, (25.0 / 950.0) * 100);
});

Deno.test("cspOtm: nearest-below diverges from nearest-either-side when the closer strike is above target", () => {
  // spot 1000, pct=5 -> target 950. Strike 960 is nearer in absolute
  // distance (10 vs 30) but sits ABOVE target -- less than 5% OTM --
  // so it must lose to 920, the nearest strike that's still <= target.
  const rows = [
    leg({ strikePrice: 960.0, lastPrice: 28.0 }),
    leg({ strikePrice: 920.0, lastPrice: 12.0 }),
  ];
  const result = cspOtm(rows, 1000.0, 5.0, "2026-07-28");
  assertEquals(result?.strike, 920.0);
  assertEquals(result?.putPrice, 12.0);
});

Deno.test("cspOtm: falls back to lowest available strike when none qualify", () => {
  // target 950, but every listed strike is above it -- none clear "at
  // least 5% OTM", so the lowest available strike is the closest
  // approximation.
  const rows = [
    leg({ strikePrice: 980.0, lastPrice: 35.0 }),
    leg({ strikePrice: 1000.0, lastPrice: 50.0 }),
  ];
  const result = cspOtm(rows, 1000.0, 5.0, "2026-07-28");
  assertEquals(result?.strike, 980.0);
});

Deno.test("cspOtm: a wider pct produces a lower strike", () => {
  const rows = [
    leg({ strikePrice: 900.0, lastPrice: 5.0 }),
    leg({ strikePrice: 930.0, lastPrice: 12.0 }),
    leg({ strikePrice: 950.0, lastPrice: 25.0 }),
    leg({ strikePrice: 970.0, lastPrice: 40.0 }),
  ];
  assertEquals(cspOtm(rows, 1000.0, 5.0, "2026-07-28")?.strike, 950.0);
  assertEquals(cspOtm(rows, 1000.0, 7.0, "2026-07-28")?.strike, 930.0);
  assertEquals(cspOtm(rows, 1000.0, 10.0, "2026-07-28")?.strike, 900.0);
});

Deno.test("cspOtm: echoes back the expiryDate argument, not a row field", () => {
  const rows = [leg({ strikePrice: 950.0, lastPrice: 25.0, expiryDate: "2026-08-25" })];
  const result = cspOtm(rows, 1000.0, 5.0, "2026-08-25");
  assertEquals(result?.expiryDate, "2026-08-25");
});

Deno.test("cspOtm: no PE rows returns null", () => {
  const rows = [leg({ optionType: "CE", strikePrice: 950.0, lastPrice: 60.0 })];
  assertEquals(cspOtm(rows, 1000.0, 5.0, "2026-07-28"), null);
});

Deno.test("cspOtm: empty rows returns null", () => {
  assertEquals(cspOtm([], 1000.0, 5.0, "2026-07-28"), null);
});

Deno.test("cspOtm: prefers freshest trade_date over pure nearest-below-target", () => {
  // spot 1000, pct=5 -> target 950. Strike 950 is the literal
  // nearest-below match but hasn't traded since 2026-07-01 (illiquid);
  // strike 920 is farther but is the only strike from the freshest
  // trade_date (2026-07-20), so it must win instead.
  const rows = [
    leg({ strikePrice: 950.0, lastPrice: 999.0, tradeDate: "2026-07-01" }),
    leg({ strikePrice: 920.0, lastPrice: 12.0, tradeDate: "2026-07-20" }),
  ];
  const result = cspOtm(rows, 1000.0, 5.0, "2026-07-28");
  assertEquals(result?.strike, 920.0);
  assertEquals(result?.putTradeDate, "2026-07-20");
});

// --- ccFivePct -------------------------------------------------------------

const EXPIRY = "2026-07-28";

function ccBaseRows(): OptionLegRow[] {
  // spot 1000 -> target 1050 (5% above) -> strike 1050 is an exact match
  return [
    leg({ optionType: "CE", strikePrice: 1000.0, lastPrice: 30.0 }),
    leg({ optionType: "CE", strikePrice: 1050.0, lastPrice: 15.0 }),
    leg({ optionType: "CE", strikePrice: 1100.0, lastPrice: 5.0 }),
  ];
}

Deno.test("ccFivePct: picks the strike nearest 5% above spot", () => {
  const result = ccFivePct(ccBaseRows(), 1000.0, EXPIRY);
  assertEquals(result?.strike, 1050.0);
  assertEquals(result?.premium, 15.0);
});

Deno.test("ccFivePct: computes grossInvestment, netInvestment, ccPct, and assignmentProfitPct", () => {
  const result = ccFivePct(ccBaseRows(), 1000.0, EXPIRY);
  assertEquals(result?.grossInvestment, 1000.0);
  assertAlmostEquals(result!.netInvestment!, 985.0); // 1000 - 15
  assertAlmostEquals(result!.ccPct!, (15.0 / 1000.0) * 100);
  assertAlmostEquals(result!.assignmentProfitPct!, (1050.0 / 985.0 - 1) * 100);
});

Deno.test("ccFivePct: picks lowest strike at or above 5% OTM, not merely nearest", () => {
  // spot 1000 -> target 1050. 1040 is nearer to target in absolute
  // distance (10 vs 30) but falls BELOW the 5% OTM line, so it must lose
  // to 1080 -- the lowest strike that actually clears "5% or more OTM".
  const rows = [
    leg({ optionType: "CE", strikePrice: 1040.0, lastPrice: 20.0 }),
    leg({ optionType: "CE", strikePrice: 1080.0, lastPrice: 8.0 }),
  ];
  const result = ccFivePct(rows, 1000.0, EXPIRY);
  assertEquals(result?.strike, 1080.0);
  assertEquals(result?.premium, 8.0);
});

Deno.test("ccFivePct: falls back to highest available strike when none reach 5% OTM", () => {
  const rows = [
    leg({ optionType: "CE", strikePrice: 950.0, lastPrice: 40.0 }),
    leg({ optionType: "CE", strikePrice: 1000.0, lastPrice: 30.0 }),
  ];
  const result = ccFivePct(rows, 1000.0, EXPIRY);
  assertEquals(result?.strike, 1000.0);
});

Deno.test("ccFivePct: assignmentProfitPct is null when premium at least covers spot", () => {
  const rows = [leg({ optionType: "CE", strikePrice: 1050.0, lastPrice: 1000.0 })];
  const result = ccFivePct(rows, 1000.0, EXPIRY);
  assertEquals(result?.netInvestment, 0);
  assertEquals(result?.assignmentProfitPct, null);
});

Deno.test("ccFivePct: echoes back the expiryDate argument, not a row field", () => {
  const rows = [leg({ optionType: "CE", strikePrice: 1050.0, lastPrice: 15.0, expiryDate: "2026-08-25" })];
  const result = ccFivePct(rows, 1000.0, "2026-08-25");
  assertEquals(result?.expiryDate, "2026-08-25");
});

Deno.test("ccFivePct: no CE rows returns null", () => {
  const rows = [leg({ optionType: "PE", strikePrice: 1050.0, lastPrice: 15.0 })];
  assertEquals(ccFivePct(rows, 1000.0, EXPIRY), null);
});

Deno.test("ccFivePct: empty rows returns null", () => {
  assertEquals(ccFivePct([], 1000.0, EXPIRY), null);
});

Deno.test("ccFivePct: prefers freshest trade_date over pure nearest-strike", () => {
  // spot 1000 -> target 1050. Strike 1050 is the literal nearest match
  // but hasn't traded since 2026-07-01 (illiquid); strike 1100 is
  // farther but is the only strike from the freshest trade_date
  // (2026-07-20), so it must win instead.
  const rows = [
    leg({ optionType: "CE", strikePrice: 1050.0, lastPrice: 15.0, tradeDate: "2026-07-01" }),
    leg({ optionType: "CE", strikePrice: 1100.0, lastPrice: 5.0, tradeDate: "2026-07-20" }),
  ];
  const result = ccFivePct(rows, 1000.0, EXPIRY);
  assertEquals(result?.strike, 1100.0);
  assertEquals(result?.tradeDate, "2026-07-20");
});

Deno.test("ccFivePct: no trade_date at all falls back to pure nearest-strike", () => {
  const result = ccFivePct(ccBaseRows(), 1000.0, EXPIRY);
  assertEquals(result?.strike, 1050.0);
});

// --- dashboardMetricsRows --------------------------------------------------

function rowsForExpiry(expiryDate: string): OptionLegRow[] {
  return [
    leg({ optionType: "CE", strikePrice: 1000.0, lastPrice: 30.0, expiryDate }),
    leg({ optionType: "CE", strikePrice: 1050.0, lastPrice: 15.0, expiryDate }),
    leg({ optionType: "PE", strikePrice: 900.0, lastPrice: 5.0, expiryDate }),
    leg({ optionType: "PE", strikePrice: 950.0, lastPrice: 25.0, expiryDate }),
  ];
}

Deno.test("dashboardMetricsRows: merges CSP and CC for one expiry into one row", () => {
  const result = dashboardMetricsRows(rowsForExpiry("2026-07-28"), { RELIANCE: 1000.0 });
  assertEquals(result.length, 1);
  const row = result[0];
  assertEquals(row.symbol, "RELIANCE");
  assertEquals(row.expiryDate, "2026-07-28");
  assertEquals(row.spot, 1000.0);
  assertEquals(row.cspStrike, 950.0);
  assertEquals(row.cspPutPrice, 25.0);
  assertEquals(row.ccStrike, 1050.0);
  assertEquals(row.ccPremium, 15.0);
  assertAlmostEquals(row.ccPct!, (15.0 / 1000.0) * 100);
});

Deno.test("dashboardMetricsRows: up to 3 nearest expiries each get a row", () => {
  const rows = [
    ...rowsForExpiry("2026-07-28"),
    ...rowsForExpiry("2026-08-25"),
    ...rowsForExpiry("2026-09-29"),
  ];
  const result = dashboardMetricsRows(rows, { RELIANCE: 1000.0 });
  assertEquals(result.length, 3);
  assertEquals(new Set(result.map((r) => r.expiryDate)), new Set(["2026-07-28", "2026-08-25", "2026-09-29"]));
});

Deno.test("dashboardMetricsRows: a 4th, farther expiry does not get a row", () => {
  const rows = [
    ...rowsForExpiry("2026-07-28"),
    ...rowsForExpiry("2026-08-25"),
    ...rowsForExpiry("2026-09-29"),
    ...rowsForExpiry("2026-10-27"),
  ];
  const result = dashboardMetricsRows(rows, { RELIANCE: 1000.0 });
  assertEquals(result.length, 3);
  assert(!result.some((r) => r.expiryDate === "2026-10-27"));
});

Deno.test("dashboardMetricsRows: symbol with no option data gets zero rows", () => {
  const result = dashboardMetricsRows([], { RELIANCE: 1000.0 });
  assertEquals(result, []);
});

Deno.test("dashboardMetricsRows: symbol without spot gets zero rows even with option data", () => {
  const result = dashboardMetricsRows(rowsForExpiry("2026-07-28"), { RELIANCE: null });
  assertEquals(result, []);
});

Deno.test("dashboardMetricsRows: CSP and CC degrade independently within a row", () => {
  // no PE rows at all -> cspFivePct returns null, but ccFivePct only
  // needs CE legs, so it still succeeds.
  const rows = rowsForExpiry("2026-07-28").filter((r) => r.optionType !== "PE");
  const result = dashboardMetricsRows(rows, { RELIANCE: 1000.0 });
  assertEquals(result.length, 1);
  assertEquals(result[0].cspPct, null);
  assert(result[0].ccPct !== null);
});

Deno.test("dashboardMetricsRows: multiple symbols each get their own rows", () => {
  const rows = [
    ...rowsForExpiry("2026-07-28"),
    ...rowsForExpiry("2026-07-28").map((r) => ({ ...r, symbol: "TCS" })),
  ];
  const result = dashboardMetricsRows(rows, { RELIANCE: 1000.0, TCS: 1000.0 });
  const symbols = new Set(result.map((r) => r.symbol));
  assertEquals(symbols, new Set(["RELIANCE", "TCS"]));
});

Deno.test("dashboardMetricsRows: BSE-sourced legs are excluded", () => {
  // A stale, pre-restriction BSE contract for a real stock symbol, a few
  // days off its real NSE monthly expiry -- BSE's F&O feed is
  // index-options only, so this leg should never produce a row.
  const bseRows = rowsForExpiry("2026-07-31").map((r) => ({ ...r, source: "bse_fo_bhavcopy" }));
  const nseRows = rowsForExpiry("2026-07-28").map((r) => ({ ...r, source: "nse_fo_bhavcopy" }));
  const result = dashboardMetricsRows([...bseRows, ...nseRows], { RELIANCE: 1000.0 });
  assertEquals(new Set(result.map((r) => r.expiryDate)), new Set(["2026-07-28"]));
});

Deno.test("dashboardMetricsRows: BSE-sourced legs excluded even with _edge suffix", () => {
  const bseRows = rowsForExpiry("2026-07-28").map((r) => ({ ...r, source: "bse_fo_bhavcopy_edge" }));
  const result = dashboardMetricsRows(bseRows, { RELIANCE: 1000.0 });
  assertEquals(result, []);
});

Deno.test("dashboardMetricsRows: rows without a source pass through unaffected", () => {
  const result = dashboardMetricsRows(rowsForExpiry("2026-07-28"), { RELIANCE: 1000.0 });
  assertEquals(result.length, 1);
});

Deno.test("dashboardMetricsRows: CSP's OTM target is rank-based (5%/7%/10%), not flat 5% for every expiry", () => {
  // Same 4 PE strikes (900/930/950/970) at each of 3 expiries -- spot
  // 1000 -> near (5%) should land on 950, next (7%) on 930, far (10%)
  // on 900, confirming each expiry's own rank picks a different target,
  // not the same flat 5% cspFivePct/csp_5pct_for_rows would use.
  function rowsWithFourStrikes(expiryDate: string): OptionLegRow[] {
    return [
      leg({ optionType: "CE", strikePrice: 1050.0, lastPrice: 15.0, expiryDate }),
      leg({ optionType: "PE", strikePrice: 900.0, lastPrice: 5.0, expiryDate }),
      leg({ optionType: "PE", strikePrice: 930.0, lastPrice: 12.0, expiryDate }),
      leg({ optionType: "PE", strikePrice: 950.0, lastPrice: 25.0, expiryDate }),
      leg({ optionType: "PE", strikePrice: 970.0, lastPrice: 40.0, expiryDate }),
    ];
  }
  const rows = [
    ...rowsWithFourStrikes("2026-07-28"),
    ...rowsWithFourStrikes("2026-08-25"),
    ...rowsWithFourStrikes("2026-09-29"),
  ];
  const result = dashboardMetricsRows(rows, { RELIANCE: 1000.0 });
  assertEquals(result.length, 3);
  const byExpiry = Object.fromEntries(result.map((r) => [r.expiryDate, r]));
  assertEquals(byExpiry["2026-07-28"].cspStrike, 950.0); // near, 5%
  assertEquals(byExpiry["2026-08-25"].cspStrike, 930.0); // next, 7%
  assertEquals(byExpiry["2026-09-29"].cspStrike, 900.0); // far, 10%
});
