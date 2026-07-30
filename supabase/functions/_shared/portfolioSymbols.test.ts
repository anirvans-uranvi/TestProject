// Tests for portfolioSymbols.ts's resolveTrackedSymbols/looksLikeEtfName.
// Run with:
//   deno test supabase/functions/_shared/portfolioSymbols.test.ts
//
// Mirrors tests/test_portfolio_service.py's TestResolveTrackedSymbols /
// TestLooksLikeEtfName.
import { assertEquals } from "jsr:@std/assert@1";
import { looksLikeEtfName, resolveTrackedSymbols } from "./portfolioSymbols.ts";

Deno.test("resolveTrackedSymbols: returns only symbols not already known", () => {
  const result = resolveTrackedSymbols(
    ["SBIN", "NIFTYBEES", "HINDZINC"],
    new Set(["SBIN"]),
    { NIFTYBEES: "NIFTYBEES", HINDZINC: "Hindustan Zinc" },
  );
  const symbols = result.map((c) => c.symbol).sort();
  assertEquals(symbols, ["HINDZINC", "NIFTYBEES"]);
  const bySymbol = Object.fromEntries(result.map((c) => [c.symbol, c.name]));
  assertEquals(bySymbol["HINDZINC"], "Hindustan Zinc");
});

Deno.test("resolveTrackedSymbols: falls back to the symbol itself when no raw name is known", () => {
  const result = resolveTrackedSymbols(["NIFTYBEES"], new Set(), {});
  assertEquals(result, [{ symbol: "NIFTYBEES", name: "NIFTYBEES", isEtf: false }]);
});

Deno.test("resolveTrackedSymbols: no new companies when everything is already known", () => {
  const result = resolveTrackedSymbols(["SBIN"], new Set(["SBIN"]), {});
  assertEquals(result, []);
});

Deno.test("resolveTrackedSymbols: deduplicates repeated symbols across users", () => {
  const result = resolveTrackedSymbols(
    ["NIFTYBEES", "NIFTYBEES"],
    new Set(),
    { NIFTYBEES: "Nifty BeES" },
  );
  assertEquals(result.length, 1);
});

Deno.test("looksLikeEtfName: matches real ETF/fund display names, case-insensitively", () => {
  assertEquals(looksLikeEtfName("Nippon India ETF Nifty 50 BeES"), true);
  assertEquals(looksLikeEtfName("Zerodha Nifty 1D Rate Liquid ETF"), true);
  assertEquals(looksLikeEtfName("zerodha mutual fund - zerodha nifty 8-13 yr g-sec etf"), true);
});

Deno.test("looksLikeEtfName: does not match real stock names", () => {
  assertEquals(looksLikeEtfName("Hindustan Zinc Limited"), false);
  assertEquals(looksLikeEtfName("IndusInd Bank Limited"), false);
  assertEquals(looksLikeEtfName("Vedanta Aluminium Metal Limited"), false);
});
