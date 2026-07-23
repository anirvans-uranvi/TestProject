// Tests for portfolioSymbols.ts's resolveTrackedSymbols. Run with:
//   deno test supabase/functions/_shared/portfolioSymbols.test.ts
//
// Mirrors tests/test_portfolio_service.py's TestResolveTrackedSymbols.
import { assertEquals } from "jsr:@std/assert@1";
import { resolveTrackedSymbols } from "./portfolioSymbols.ts";

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
  assertEquals(result, [{ symbol: "NIFTYBEES", name: "NIFTYBEES" }]);
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
