// Boundary-case tests mirroring tests/test_calculations_classification.py,
// tests/test_calculations_returns.py, tests/test_calculations_dividends.py,
// and tests/test_fundamentals_repo.py -- run with:
//   deno test supabase/functions/manual-refresh/calculations.test.ts
import { assertEquals } from "jsr:@std/assert@1";
import {
  buildClassification,
  carryForwardFields,
  classify,
  criterion52wHigh,
  criterion52wLow,
  criterionA,
  criterionB,
  criterionC,
  pctReturn,
  return1d,
  return20d,
  return5d,
  returnNTradingDaysAgo,
  ttmDividendSum,
  ttmDividendYield,
} from "./calculations.ts";

// --- pctReturn / returns ---------------------------------------------

Deno.test("pctReturn - positive return", () => {
  assertEquals(Math.round(pctReturn(110, 100)! * 100) / 100, 10.0);
});

Deno.test("pctReturn - negative return", () => {
  assertEquals(Math.round(pctReturn(90, 100)! * 100) / 100, -10.0);
});

Deno.test("pctReturn - zero return exactly", () => {
  assertEquals(pctReturn(100, 100), 0.0);
});

Deno.test("pctReturn - missing latest returns null", () => {
  assertEquals(pctReturn(null, 100), null);
});

Deno.test("pctReturn - missing base returns null", () => {
  assertEquals(pctReturn(100, null), null);
});

Deno.test("pctReturn - zero base returns null", () => {
  assertEquals(pctReturn(100, 0), null);
});

Deno.test("returnNTradingDaysAgo - exact window", () => {
  const closes = [90, 91, 92, 93, 94];
  assertEquals(returnNTradingDaysAgo(100, closes, 1), pctReturn(100, 94));
  assertEquals(returnNTradingDaysAgo(100, closes, 5), pctReturn(100, 90));
});

Deno.test("returnNTradingDaysAgo - insufficient history returns null", () => {
  assertEquals(returnNTradingDaysAgo(100, [92, 93, 94], 5), null);
});

Deno.test("returnNTradingDaysAgo - missing latest price returns null", () => {
  assertEquals(returnNTradingDaysAgo(null, [90, 91, 92, 93, 94], 1), null);
});

Deno.test("returnNTradingDaysAgo - n=0 returns null", () => {
  assertEquals(returnNTradingDaysAgo(100, [90, 91], 0), null);
});

Deno.test("return1d/5d/20d wrappers", () => {
  const closes = Array.from({ length: 20 }, (_, i) => 80 + i); // 80..99, oldest->newest
  assertEquals(return1d(100, closes), pctReturn(100, closes[closes.length - 1]));
  assertEquals(return5d(100, closes), pctReturn(100, closes[closes.length - 5]));
  assertEquals(return20d(100, closes), pctReturn(100, closes[closes.length - 20]));
  assertEquals(return20d(100, closes.slice(0, 19)), null);
});

// --- ttmDividendSum / ttmDividendYield --------------------------------

Deno.test("ttmDividendSum - sums events within window", () => {
  const events = [
    { exDate: "2025-08-01", amountPerShare: 10.0 },
    { exDate: "2026-02-01", amountPerShare: 8.0 },
  ];
  assertEquals(ttmDividendSum(events, "2026-07-11"), 18.0);
});

Deno.test("ttmDividendSum - excludes events outside window", () => {
  const events = [
    { exDate: "2025-06-01", amountPerShare: 10.0 }, // > 365 days before
    { exDate: "2026-02-01", amountPerShare: 8.0 },
  ];
  assertEquals(ttmDividendSum(events, "2026-07-11"), 8.0);
});

Deno.test("ttmDividendSum - empty events sums to zero", () => {
  assertEquals(ttmDividendSum([], "2026-07-11"), 0.0);
});

Deno.test("ttmDividendYield - no dividends is confirmed zero not null", () => {
  assertEquals(ttmDividendYield([], "2026-07-11", 1500.0), 0.0);
});

Deno.test("ttmDividendYield - missing price returns null", () => {
  const events = [{ exDate: "2026-02-01", amountPerShare: 30.0 }];
  assertEquals(ttmDividendYield(events, "2026-07-11", null), null);
});

Deno.test("ttmDividendYield - zero/negative price returns null", () => {
  assertEquals(ttmDividendYield([], "2026-07-11", 0.0), null);
  assertEquals(ttmDividendYield([], "2026-07-11", -5.0), null);
});

Deno.test("ttmDividendYield - computes percentage", () => {
  const events = [{ exDate: "2026-02-01", amountPerShare: 30.0 }];
  assertEquals(ttmDividendYield(events, "2026-07-11", 1500.0), 2.0);
});

// --- criterionA / B / C ------------------------------------------------

Deno.test("criterionA - above/at/below threshold, missing", () => {
  assertEquals(criterionA(3.01, 3.0), true);
  assertEquals(criterionA(3.0, 3.0), false); // exactly-at-threshold fails (strict >)
  assertEquals(criterionA(2.99, 3.0), false);
  assertEquals(criterionA(null, 3.0), null);
  assertEquals(criterionA(0.0, 3.0), false); // zero yield fails, not missing
});

Deno.test("criterionB - all positive, any non-positive, missing", () => {
  assertEquals(criterionB(0.1, 0.1, 0.1), true);
  assertEquals(criterionB(0.0, 1.0, 1.0), false); // exactly 0% fails
  assertEquals(criterionB(-0.01, 1.0, 1.0), false);
  assertEquals(criterionB(null, 1.0, 1.0), null);
  assertEquals(criterionB(1.0, null, 1.0), null);
  assertEquals(criterionB(1.0, 1.0, null), null);
});

Deno.test("criterionC - PEG passes AT OR BELOW threshold (reversed direction)", () => {
  assertEquals(criterionC(1.01, 1.0), false); // above threshold FAILS
  assertEquals(criterionC(1.0, 1.0), true); // exactly-at-threshold PASSES
  assertEquals(criterionC(0.99, 1.0), true); // below threshold PASSES
  assertEquals(criterionC(null, 1.0), null);
  assertEquals(criterionC(-0.5, 1.0), true); // negative PEG passes literally (<=)
});

// --- criterion52wHigh / criterion52wLow -----------------------------------

Deno.test("criterion52wHigh - below/at/above 90pct of high, missing", () => {
  assertEquals(criterion52wHigh(890.0, 1000.0), true);
  assertEquals(criterion52wHigh(900.0, 1000.0), false); // exactly-at-threshold fails
  assertEquals(criterion52wHigh(950.0, 1000.0), false);
  assertEquals(criterion52wHigh(null, 1000.0), null);
  assertEquals(criterion52wHigh(900.0, null), null);
});

Deno.test("criterion52wLow - above/at/below 110pct of low, missing", () => {
  assertEquals(criterion52wLow(660.0, 500.0), true);
  assertEquals(criterion52wLow(550.0, 500.0), false); // exactly-at-threshold fails
  assertEquals(criterion52wLow(520.0, 500.0), false);
  assertEquals(criterion52wLow(null, 500.0), null);
  assertEquals(criterion52wLow(520.0, null), null);
});

// --- classify ------------------------------------------------------------

Deno.test("classify - all pass is green, none pass is red", () => {
  assertEquals(classify(true, true, true), "green");
  assertEquals(classify(false, false, false), "red");
});

Deno.test("classify - one or two pass is amber", () => {
  assertEquals(classify(true, false, false), "amber");
  assertEquals(classify(true, true, false), "amber");
});

Deno.test("classify - any missing is unavailable, never counted as fail", () => {
  assertEquals(classify(null, true, true), "unavailable");
  assertEquals(classify(false, false, null), "unavailable"); // NOT red
});

Deno.test("classify - stale forces unavailable even if all pass", () => {
  assertEquals(classify(true, true, true, true), "unavailable");
});

// --- buildClassification ---------------------------------------------

Deno.test("buildClassification - full green row", () => {
  const result = buildClassification({
    ttmDividendYield: 4.0,
    return1d: 0.5,
    return5d: 1.2,
    return20d: 3.0,
    pegRatio: 0.8, // <= 1.0 default threshold: passes
    latestPrice: 1000.0,
    peRatio: 20.0,
  });
  assertEquals(result.status, "green");
  assertEquals(result.criterionA, true);
  assertEquals(result.criterionB, true);
  assertEquals(result.criterionC, true);
});

Deno.test("buildClassification - missing PEG yields unavailable with data quality flag", () => {
  const result = buildClassification({
    ttmDividendYield: 4.0,
    return1d: 0.5,
    return5d: 1.2,
    return20d: 3.0,
    pegRatio: null,
    latestPrice: 1000.0,
  });
  assertEquals(result.status, "unavailable");
  assertEquals(result.dataQuality.missingPeg, true);
});

Deno.test("buildClassification - custom thresholds applied", () => {
  const result = buildClassification({
    ttmDividendYield: 5.0,
    return1d: 0.1,
    return5d: 0.1,
    return20d: 0.1,
    pegRatio: 2.0,
    latestPrice: 1000.0,
    dividendYieldThreshold: 6.0, // 5.0 now fails
    pegThreshold: 3.0, // 2.0 <= 3.0 now passes
  });
  assertEquals(result.criterionA, false);
  assertEquals(result.criterionC, true);
  assertEquals(result.status, "amber");
});

// --- carryForwardFields ------------------------------------------------

Deno.test("carryForwardFields - falls back to older row for missing field", () => {
  const rows = [
    { peRatio: 44.0, pegRatio: null, eps: 10.0, marketCap: 5e10 },
    { peRatio: 43.0, pegRatio: 0.83, eps: 9.5, marketCap: 4.9e10 },
  ];
  const result = carryForwardFields(rows);
  assertEquals(result.peRatio, 44.0); // present in latest row
  assertEquals(result.pegRatio, 0.83); // carried forward
});

Deno.test("carryForwardFields - each field carried forward independently", () => {
  const rows = [
    { peRatio: null, pegRatio: null, eps: null, marketCap: null },
    { peRatio: null, pegRatio: 0.9, eps: null, marketCap: null },
    { peRatio: 20.0, pegRatio: null, eps: null, marketCap: null },
    { peRatio: null, pegRatio: null, eps: 8.0, marketCap: 3e10 },
  ];
  assertEquals(carryForwardFields(rows), {
    peRatio: 20.0, pegRatio: 0.9, eps: 8.0, marketCap: 3e10, week52High: null, week52Low: null,
  });
});

Deno.test("carryForwardFields - 52w high/low carried forward independently", () => {
  const rows = [
    { week52High: null, week52Low: 850.0 },
    { week52High: 1600.0, week52Low: null },
  ];
  const result = carryForwardFields(rows);
  assertEquals(result.week52High, 1600.0);
  assertEquals(result.week52Low, 850.0);
});

Deno.test("carryForwardFields - field never available stays null", () => {
  const rows = [{ peRatio: 20.0, pegRatio: null, eps: null, marketCap: null }];
  const result = carryForwardFields(rows);
  assertEquals(result.pegRatio, null);
  assertEquals(result.eps, null);
});

Deno.test("carryForwardFields - empty rows returns all null", () => {
  const result = carryForwardFields([]);
  assertEquals(result, {
    peRatio: null, pegRatio: null, eps: null, marketCap: null, week52High: null, week52Low: null,
  });
});
