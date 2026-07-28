// Run with: deno test supabase/functions/manual-refresh/yahoo.test.ts
import { assertEquals } from "jsr:@std/assert@1";
import { finiteOrNull } from "./yahoo.ts";

Deno.test("finiteOrNull - passes through a normal number", () => {
  assertEquals(finiteOrNull(12.5), 12.5);
  assertEquals(finiteOrNull(0), 0);
});

Deno.test("finiteOrNull - rejects Infinity sent as a number", () => {
  assertEquals(finiteOrNull(Infinity), null);
  assertEquals(finiteOrNull(-Infinity), null);
});

Deno.test("finiteOrNull - rejects Infinity sent as Yahoo's string form", () => {
  // Yahoo's quoteSummary JSON can't represent a real Infinity, so it sends
  // the *string* "Infinity" for a stock like VAML (trailingEps == 0 ->
  // trailingPE = price / 0) instead of a number.
  assertEquals(finiteOrNull("Infinity"), null);
  assertEquals(finiteOrNull("-Infinity"), null);
});

Deno.test("finiteOrNull - rejects NaN and null/undefined", () => {
  assertEquals(finiteOrNull(NaN), null);
  assertEquals(finiteOrNull("NaN"), null);
  assertEquals(finiteOrNull(null), null);
  assertEquals(finiteOrNull(undefined), null);
});
