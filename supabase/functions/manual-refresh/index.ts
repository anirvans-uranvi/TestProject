// Manual on-demand refresh, triggered from the Dashboard's "Manual
// refresh" button (src/services/edge_refresh.py). Does the real
// fetch-from-Yahoo-and-write-to-Supabase work that Streamlit page code
// can never safely do itself, since that requires the service-role key
// (bypasses RLS) -- this function holds it as an Edge Runtime env var,
// never exposed to the browser or to Streamlit.
//
// Full parity with `python scripts/run_refresh.py --mode=all`: prices,
// dividends, fundamentals, and a screener recompute, all in one
// invocation. See calculations.ts's file header for the real tradeoff
// this implies (business logic duplicated in a second language) and
// yahoo.ts's for the Yahoo-endpoint fragility this accepts.
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";
import {
  buildClassification,
  carryForwardFields,
  type FundamentalsRow,
  returnNTradingDaysAgo,
  ttmDividendYield,
} from "./calculations.ts";
import { fetchChartData, fetchFundamentals } from "./yahoo.ts";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SUPABASE_ANON_KEY = Deno.env.get("SUPABASE_ANON_KEY")!;
const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

const COOLDOWN_MINUTES = 5;
const BATCH_SIZE = 8;
const CHART_RANGE = "1y"; // covers 20d return lookback and the full 365-day TTM dividend window in one fetch
const DEFAULT_DIVIDEND_YIELD_THRESHOLD = 3.0;
const DEFAULT_PEG_THRESHOLD = 1.0;
const FUNDAMENTALS_LOOKBACK_ROWS = 200;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function todayIsoInIst(): string {
  // en-CA locale conveniently formats as YYYY-MM-DD.
  return new Date().toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" });
}

function isoDateMinusDays(isoDate: string, days: number): string {
  const d = new Date(`${isoDate}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() - days);
  return d.toISOString().slice(0, 10);
}

function chunk<T>(items: T[], size: number): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < items.length; i += size) out.push(items.slice(i, i + size));
  return out;
}

interface SymbolResult {
  symbol: string;
  ok: boolean;
  error?: string;
}

// deno-lint-ignore no-explicit-any
type AnyClient = any;

// Not using generated Database types (no `supabase gen types` step in
// this project) means supabase-js's default generics would otherwise
// infer every `.from(table)` row as `never` -- explicitly loosened here
// rather than hand-maintaining a parallel schema type.
async function refreshOneSymbol(
  serviceClient: AnyClient,
  symbol: string,
  asOfDate: string,
): Promise<SymbolResult> {
  try {
    const chart = await fetchChartData(symbol, CHART_RANGE);
    if (chart.points.length === 0) {
      throw new Error("Yahoo returned no price points");
    }

    // --- price_history --------------------------------------------------
    const priceRows = chart.points.map((p) => ({
      symbol,
      trade_date: p.tradeDate,
      open: p.open,
      high: p.high,
      low: p.low,
      close: p.close,
      adjusted_close: p.adjustedClose,
      volume: p.volume,
      source: "manual_edge",
    }));
    const { error: priceErr } = await serviceClient
      .from("price_history")
      .upsert(priceRows, { onConflict: "symbol,trade_date" });
    if (priceErr) throw new Error(`price_history upsert: ${priceErr.message}`);

    // --- dividend_events -------------------------------------------------
    if (chart.dividends.length > 0) {
      const dividendRows = chart.dividends.map((d) => ({
        symbol,
        ex_date: d.exDate,
        amount_per_share: d.amountPerShare,
        dividend_type: "final",
        source: "manual_edge",
      }));
      const { error: divErr } = await serviceClient
        .from("dividend_events")
        .upsert(dividendRows, { onConflict: "symbol,ex_date,amount_per_share" });
      if (divErr) throw new Error(`dividend_events upsert: ${divErr.message}`);
    }

    // --- fundamental_snapshots --------------------------------------------
    const fundamentals = await fetchFundamentals(symbol);
    const fundamentalsPayload: Record<string, unknown> = {
      symbol,
      as_of_date: asOfDate,
      source: "manual_edge",
      is_stale: false,
    };
    // Mirror Python's exclude_none=True upsert: omit null fields entirely
    // so they don't clobber a same-day row's previously-fetched values.
    if (fundamentals.peRatio !== null) fundamentalsPayload.pe_ratio = fundamentals.peRatio;
    if (fundamentals.pegRatio !== null) fundamentalsPayload.peg_ratio = fundamentals.pegRatio;
    if (fundamentals.eps !== null) fundamentalsPayload.eps = fundamentals.eps;
    if (fundamentals.marketCap !== null) fundamentalsPayload.market_cap = fundamentals.marketCap;
    const { error: fundErr } = await serviceClient
      .from("fundamental_snapshots")
      .upsert(fundamentalsPayload, { onConflict: "symbol,as_of_date" });
    if (fundErr) throw new Error(`fundamental_snapshots upsert: ${fundErr.message}`);

    // Carry-forward: today's Yahoo fetch may have gaps (PEG especially),
    // so pull recent history and use the most recent non-null value per
    // field -- exactly what fundamentals_repo.get_latest_fundamentals()
    // does in Python. See calculations.ts::carryForwardFields.
    const { data: recentFundamentals, error: histErr } = await serviceClient
      .from("fundamental_snapshots")
      .select("pe_ratio,peg_ratio,eps,market_cap")
      .eq("symbol", symbol)
      .order("as_of_date", { ascending: false })
      .limit(FUNDAMENTALS_LOOKBACK_ROWS);
    if (histErr) throw new Error(`fundamental_snapshots read-back: ${histErr.message}`);

    const carried: FundamentalsRow = carryForwardFields(
      (recentFundamentals ?? []).map((r: any) => ({
        peRatio: r.pe_ratio,
        pegRatio: r.peg_ratio,
        eps: r.eps,
        marketCap: r.market_cap,
      })),
    );

    // --- classification ----------------------------------------------------
    // Bound historicalCloses to everything BEFORE the latest point --
    // the same fix screener_service.py applies, so latestPrice is never
    // compared against itself (which would force every return to 0).
    const sortedPoints = [...chart.points].sort((a, b) => (a.tradeDate < b.tradeDate ? -1 : 1));
    const latestPoint = sortedPoints[sortedPoints.length - 1];
    const latestPrice = latestPoint.adjustedClose ?? latestPoint.close;
    const historicalCloses = sortedPoints.slice(0, -1).map((p) => p.adjustedClose ?? p.close);

    const isStale = latestPoint.tradeDate < isoDateMinusDays(asOfDate, 5);

    const dividendEventsForYield = chart.dividends.map((d) => ({ exDate: d.exDate, amountPerShare: d.amountPerShare }));

    const return1dVal = returnNTradingDaysAgo(latestPrice, historicalCloses, 1);
    const return5dVal = returnNTradingDaysAgo(latestPrice, historicalCloses, 5);
    const return20dVal = returnNTradingDaysAgo(latestPrice, historicalCloses, 20);
    const ttmYield = ttmDividendYield(dividendEventsForYield, asOfDate, latestPrice);

    const classification = buildClassification({
      ttmDividendYield: ttmYield,
      return1d: return1dVal,
      return5d: return5dVal,
      return20d: return20dVal,
      pegRatio: carried.pegRatio,
      isStale,
      dividendYieldThreshold: DEFAULT_DIVIDEND_YIELD_THRESHOLD,
      pegThreshold: DEFAULT_PEG_THRESHOLD,
      latestPrice,
      peRatio: carried.peRatio,
    });

    const { error: snapshotErr } = await serviceClient
      .from("daily_screener_snapshots")
      .upsert(
        {
          symbol,
          snapshot_date: asOfDate,
          latest_price: latestPrice,
          return_1d: return1dVal,
          return_5d: return5dVal,
          return_20d: return20dVal,
          ttm_dividend_yield: ttmYield,
          pe_ratio: carried.peRatio,
          peg_ratio: carried.pegRatio,
          criterion_a: classification.criterionA,
          criterion_b: classification.criterionB,
          criterion_c: classification.criterionC,
          status: classification.status,
          data_quality: classification.dataQuality,
        },
        { onConflict: "symbol,snapshot_date" },
      );
    if (snapshotErr) throw new Error(`daily_screener_snapshots upsert: ${snapshotErr.message}`);

    return { symbol, ok: true };
  } catch (err) {
    return { symbol, ok: false, error: err instanceof Error ? err.message : String(err) };
  }
}

Deno.serve(async (req: Request) => {
  if (req.method !== "POST") {
    return jsonResponse({ error: "Use POST" }, 405);
  }

  const authHeader = req.headers.get("Authorization");
  if (!authHeader?.startsWith("Bearer ")) {
    return jsonResponse({ error: "Missing Authorization header" }, 401);
  }
  const accessToken = authHeader.slice("Bearer ".length);

  const anonClient: AnyClient = createClient(SUPABASE_URL, SUPABASE_ANON_KEY);
  const { data: userData, error: authError } = await anonClient.auth.getUser(accessToken);
  if (authError || !userData?.user) {
    return jsonResponse({ error: "Invalid or expired session -- please sign in again" }, 401);
  }

  const serviceClient: AnyClient = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);
  const startedAt = new Date();

  // Cooldown: any logged-in user can trigger this (shared dataset, not
  // per-user), so gate on the shared last-success time to protect Yahoo
  // from repeated-click rate-limiting across all users.
  const { data: recentFetches } = await serviceClient
    .from("provider_fetch_log")
    .select("started_at")
    .eq("provider_name", "manual_edge")
    .eq("fetch_type", "all")
    .eq("status", "success")
    .order("started_at", { ascending: false })
    .limit(1);

  if (recentFetches && recentFetches.length > 0) {
    const lastFetchAt = new Date(recentFetches[0].started_at as string);
    const minutesSince = (startedAt.getTime() - lastFetchAt.getTime()) / 60_000;
    if (minutesSince < COOLDOWN_MINUTES) {
      const waitMinutes = Math.ceil(COOLDOWN_MINUTES - minutesSince);
      return jsonResponse(
        {
          error: "cooldown",
          message: `A refresh already ran recently -- please wait ${waitMinutes} more minute(s) before trying again.`,
        },
        429,
      );
    }
  }

  const { data: constituents, error: constituentsErr } = await serviceClient
    .from("nifty50_constituents")
    .select("symbol")
    .eq("is_current", true);
  if (constituentsErr || !constituents || constituents.length === 0) {
    return jsonResponse({ error: "Could not load current Nifty 50 constituents" }, 500);
  }
  const symbols: string[] = constituents.map((c: any) => c.symbol as string);

  const asOfDate = todayIsoInIst();
  const results: SymbolResult[] = [];
  for (const batch of chunk(symbols, BATCH_SIZE)) {
    const batchResults = await Promise.all(batch.map((symbol) => refreshOneSymbol(serviceClient, symbol, asOfDate)));
    results.push(...batchResults);
  }

  const succeeded = results.filter((r) => r.ok).map((r) => r.symbol);
  const failed = results.filter((r) => !r.ok);
  const finishedAt = new Date();

  await serviceClient.from("provider_fetch_log").insert({
    provider_name: "manual_edge",
    fetch_type: "all",
    symbol: null,
    status: failed.length === 0 ? "success" : (succeeded.length > 0 ? "success" : "failure"),
    error_message: failed.length > 0 ? `${failed.length} symbol(s) failed: ${failed.map((f) => f.symbol).join(", ")}` : null,
    retry_count: 0,
    started_at: startedAt.toISOString(),
    finished_at: finishedAt.toISOString(),
  });

  return jsonResponse({
    succeeded: succeeded.length,
    failed: failed.length,
    total: symbols.length,
    symbolsFailed: failed.map((f) => ({ symbol: f.symbol, error: f.error })),
    startedAt: startedAt.toISOString(),
    finishedAt: finishedAt.toISOString(),
  });
});
