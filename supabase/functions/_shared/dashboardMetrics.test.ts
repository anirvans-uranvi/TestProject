// Tests for dashboardMetrics.ts's CSP/PMCC port. Run with:
//   deno test supabase/functions/_shared/dashboardMetrics.test.ts
//
// Fixtures mirror tests/test_fo_service.py's TestCsp5PctForRows and
// TestItmPmcc5PctMap exactly (same strikes/prices/trade_dates), so this
// checks the TypeScript port against the same behavior contract as the
// Python original.
import { assert, assertAlmostEquals, assertEquals } from "jsr:@std/assert@1";
import { cspFivePct, dashboardMetricsRows, itmPmccFivePct, type OptionLegRow } from "./dashboardMetrics.ts";

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

// --- itmPmccFivePct --------------------------------------------------------

const EXPIRY = "2026-07-28";

function pmccBaseRows(): OptionLegRow[] {
  // spot 1000 -> ITM CE closest to spot (strike < 1000) is 950; 5% below
  // 950 (902.5) is closest to strike 900.
  return [
    leg({ optionType: "CE", strikePrice: 900.0, lastPrice: 110.0 }),
    leg({ optionType: "CE", strikePrice: 950.0, lastPrice: 60.0 }),
    leg({ optionType: "CE", strikePrice: 1000.0, lastPrice: 20.0 }),
    leg({ optionType: "PE", strikePrice: 900.0, lastPrice: 5.0 }),
    leg({ optionType: "PE", strikePrice: 950.0, lastPrice: 25.0 }),
    leg({ optionType: "PE", strikePrice: 1000.0, lastPrice: 60.0 }),
  ];
}

Deno.test("itmPmccFivePct: picks the ITM CE closest to spot and the OTM CE 5% below it", () => {
  const result = itmPmccFivePct(pmccBaseRows(), 1000.0, EXPIRY);
  assertEquals(result?.itmCeStrike, 950.0);
  assertEquals(result?.otmCeStrike, 900.0);
});

Deno.test("itmPmccFivePct: net credit and percentage", () => {
  // net credit = PE(950) sell 25 + CE(900) sell 110 - CE(950) buy 60 = 75
  const result = itmPmccFivePct(pmccBaseRows(), 1000.0, EXPIRY);
  assertAlmostEquals(result!.netCredit, 75.0);
  assertAlmostEquals(result!.pmccPct, (75.0 / 950.0) * 100);
});

Deno.test("itmPmccFivePct: prefers freshest trade_date for ITM and OTM CE legs", () => {
  // spot 1000. Strike 990 is the largest CE strike below spot (the
  // literal "closest ITM" pick) but hasn't traded since 2026-07-01;
  // strikes 950/900 are farther from spot but are the only ones from the
  // freshest trade_date (2026-07-20), so 950 must be chosen as the ITM
  // leg instead of the stale 990, and 900 (not 990) as the OTM leg.
  const rows = [
    leg({ optionType: "CE", strikePrice: 990.0, lastPrice: 200.0, tradeDate: "2026-07-01" }),
    leg({ optionType: "CE", strikePrice: 950.0, lastPrice: 60.0, tradeDate: "2026-07-20" }),
    leg({ optionType: "CE", strikePrice: 900.0, lastPrice: 110.0, tradeDate: "2026-07-20" }),
    leg({ optionType: "PE", strikePrice: 950.0, lastPrice: 25.0, tradeDate: "2026-07-20" }),
  ];
  const result = itmPmccFivePct(rows, 1000.0, EXPIRY);
  assertEquals(result?.itmCeStrike, 950.0);
  assertEquals(result?.otmCeStrike, 900.0);
  assertEquals(result?.buyCePrice, 60.0);
  assertEquals(result?.sellCePrice, 110.0);
  assertAlmostEquals(result!.netCredit, 75.0);
});

Deno.test("itmPmccFivePct: falls back to a stale ITM CE if no fresh strike is ITM", () => {
  // spot 1000. The only strike from the freshest trade_date (2026-07-20)
  // is 1050, which isn't ITM at all -- must fall back to the full
  // (stale-inclusive) CE set to find the genuinely-ITM 950 strike.
  const rows = [
    leg({ optionType: "CE", strikePrice: 950.0, lastPrice: 60.0, tradeDate: "2026-06-01" }),
    leg({ optionType: "CE", strikePrice: 1050.0, lastPrice: 5.0, tradeDate: "2026-07-20" }),
    leg({ optionType: "PE", strikePrice: 950.0, lastPrice: 25.0, tradeDate: "2026-06-01" }),
  ];
  const result = itmPmccFivePct(rows, 1000.0, EXPIRY);
  assertEquals(result?.itmCeStrike, 950.0);
});

Deno.test("itmPmccFivePct: no trade_date at all falls back to pure nearest-strike", () => {
  const result = itmPmccFivePct(pmccBaseRows(), 1000.0, EXPIRY);
  assertEquals(result?.itmCeStrike, 950.0);
  assertEquals(result?.otmCeStrike, 900.0);
});

Deno.test("itmPmccFivePct: no CE rows returns null", () => {
  const rows = [leg({ optionType: "PE", strikePrice: 950.0, lastPrice: 25.0 })];
  assertEquals(itmPmccFivePct(rows, 1000.0, EXPIRY), null);
});

Deno.test("itmPmccFivePct: no ITM candidate at all (even stale) returns null", () => {
  const rows = [leg({ optionType: "CE", strikePrice: 1050.0, lastPrice: 5.0 })];
  assertEquals(itmPmccFivePct(rows, 1000.0, EXPIRY), null);
});

// --- dashboardMetricsRows --------------------------------------------------

Deno.test("dashboardMetricsRows: restricts to nearest expiry only, ignores farther expiries", () => {
  const rows = [
    ...pmccBaseRows(),
    // a farther expiry that must NOT be used even for the same strikes
    leg({ optionType: "CE", strikePrice: 950.0, lastPrice: 999.0, expiryDate: "2026-08-25" }),
    leg({ optionType: "PE", strikePrice: 950.0, lastPrice: 999.0, expiryDate: "2026-08-25" }),
  ];
  const result = dashboardMetricsRows(rows, { RELIANCE: 1000.0 });
  const reliance = result.find((r) => r.symbol === "RELIANCE")!;
  assertEquals(reliance.cspPutPrice, 25.0);
  assertAlmostEquals(reliance.pmccNetCredit!, 75.0);
});

Deno.test("dashboardMetricsRows: symbol with no option data still gets a row, fields null", () => {
  const result = dashboardMetricsRows([], { RELIANCE: 1000.0 });
  assertEquals(result.length, 1);
  assertEquals(result[0].symbol, "RELIANCE");
  assertEquals(result[0].cspPct, null);
  assertEquals(result[0].pmccPct, null);
});

Deno.test("dashboardMetricsRows: symbol without spot is still included with null fields", () => {
  const result = dashboardMetricsRows(pmccBaseRows(), { RELIANCE: null });
  assert(result.some((r) => r.symbol === "RELIANCE" && r.cspPct === null && r.pmccPct === null));
});
