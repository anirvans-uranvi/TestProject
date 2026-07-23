// Tests for dashboardMetrics.ts's CSP/CC port. Run with:
//   deno test supabase/functions/_shared/dashboardMetrics.test.ts
//
// Fixtures mirror tests/test_fo_service.py's TestCsp5PctForRows and
// TestCc5PctForRows exactly (same strikes/prices/trade_dates), so this
// checks the TypeScript port against the same behavior contract as the
// Python original.
import { assert, assertAlmostEquals, assertEquals } from "jsr:@std/assert@1";
import { ccFivePct, cspFivePct, dashboardMetricsRows, type OptionLegRow } from "./dashboardMetrics.ts";

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

Deno.test("ccFivePct: computes ccPct and assignmentProfitPct", () => {
  const result = ccFivePct(ccBaseRows(), 1000.0, EXPIRY);
  assertAlmostEquals(result!.ccPct!, (15.0 / 1000.0) * 100);
  assertAlmostEquals(result!.assignmentProfitPct!, (15.0 / 50.0) * 100);
});

Deno.test("ccFivePct: assignmentProfitPct is null when strike equals spot", () => {
  const rows = [leg({ optionType: "CE", strikePrice: 1000.0, lastPrice: 30.0 })];
  const result = ccFivePct(rows, 1000.0, EXPIRY);
  assertEquals(result?.strike, 1000.0);
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
