// F&O on-demand refresh, triggered from the Dashboard's "F&O Data
// Refresh" button (src/services/edge_refresh.py::trigger_fo_refresh).
// Checks whether NSE has published a newer F&O bhavcopy than what's
// already loaded in Supabase, and only downloads + parses + ingests when
// it has -- so repeated clicks on a day with no new data are cheap
// (one HTTP HEAD-equivalent walk, no writes).
//
// Runs server-side for the same reason supabase/functions/manual-refresh
// does: real writes need the service-role key (bypasses RLS), which must
// never live in Streamlit page code, since Streamlit Cloud runs that code
// in every logged-in user's own browser session.
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";
import { findLatestAvailableBhavcopy, parseFoBhavcopy, type ParsedBhavcopy } from "./bhavcopy.ts";
import { recomputeDashboardMetrics } from "../_shared/dashboardMetrics.ts";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SUPABASE_ANON_KEY = Deno.env.get("SUPABASE_ANON_KEY")!;
const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

const COOLDOWN_MINUTES = 5;
const CHUNK_SIZE = 500;
const MAX_LOOKBACK_DAYS = 7;
const PROVIDER_NAME = "fo_edge";
const FETCH_TYPE = "fo";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function todayIsoInIst(): string {
  return new Date().toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" });
}

function chunk<T>(items: T[], size: number): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < items.length; i += size) out.push(items.slice(i, i + size));
  return out;
}

// deno-lint-ignore no-explicit-any
type AnyClient = any;

async function upsertChunked(client: AnyClient, table: string, rows: unknown[], onConflict: string): Promise<void> {
  for (const batch of chunk(rows, CHUNK_SIZE)) {
    if (batch.length === 0) continue;
    const { error } = await client.from(table).upsert(batch, { onConflict });
    if (error) throw new Error(`${table} upsert: ${error.message}`);
  }
}

async function ingest(client: AnyClient, book: ParsedBhavcopy): Promise<{ futuresRows: number; optionRows: number }> {
  await upsertChunked(client, "futures_contracts", book.futuresContracts, "symbol,expiry_date");
  await upsertChunked(client, "futures_daily_prices", book.futuresPrices, "symbol,expiry_date,trade_date");
  await upsertChunked(client, "option_contracts", book.optionContracts, "symbol,expiry_date,strike_price,option_type");
  await upsertChunked(client, "option_daily_prices", book.optionPrices, "symbol,expiry_date,strike_price,option_type,trade_date");
  return { futuresRows: book.futuresPrices.length, optionRows: book.optionPrices.length };
}

// Contracts appear in the bhavcopy only while live, so is_open must be
// (re)derived against the real calendar, not the ingested file's own
// date -- same reasoning as fo_repo.refresh_open_flags in Python.
async function refreshOpenFlags(client: AnyClient, asOfIso: string): Promise<void> {
  for (const table of ["futures_contracts", "option_contracts"]) {
    await client.from(table).update({ is_open: true }).gte("expiry_date", asOfIso);
    await client.from(table).update({ is_open: false }).lt("expiry_date", asOfIso);
  }
}

async function logFetch(
  client: AnyClient,
  startedAt: Date,
  finishedAt: Date,
  status: "success" | "failure",
  errorMessage: string | null,
): Promise<void> {
  await client.from("provider_fetch_log").insert({
    provider_name: PROVIDER_NAME,
    fetch_type: FETCH_TYPE,
    symbol: null,
    status,
    error_message: errorMessage,
    retry_count: 0,
    started_at: startedAt.toISOString(),
    finished_at: finishedAt.toISOString(),
  });
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

  // Cooldown, same pattern/reasoning as manual-refresh: shared dataset
  // (not per-user), so gate on the shared last-success time to protect
  // NSE from repeated-click hammering across all users.
  const { data: recentFetches } = await serviceClient
    .from("provider_fetch_log")
    .select("started_at")
    .eq("provider_name", PROVIDER_NAME)
    .eq("fetch_type", FETCH_TYPE)
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
          message: `An F&O refresh already ran recently -- please wait ${waitMinutes} more minute(s) before trying again.`,
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
  // deno-lint-ignore no-explicit-any
  const universe = new Set<string>(constituents.map((c: any) => c.symbol as string));

  // "Already loaded" watermark: newest trade_date across any symbol's
  // futures (options are always ingested in the same run, so they share
  // this watermark).
  const { data: latestLoadedRows } = await serviceClient
    .from("futures_daily_prices")
    .select("trade_date")
    .order("trade_date", { ascending: false })
    .limit(1);
  const latestLoaded: string | null = latestLoadedRows?.[0]?.trade_date ?? null;

  let found;
  try {
    found = await findLatestAvailableBhavcopy(todayIsoInIst(), MAX_LOOKBACK_DAYS);
  } catch (err) {
    return jsonResponse({ error: `Could not reach NSE: ${err instanceof Error ? err.message : String(err)}` }, 502);
  }
  if (!found) {
    return jsonResponse(
      { error: "No NSE F&O bhavcopy found in the last week -- NSE may be down or blocking this request" },
      502,
    );
  }

  if (latestLoaded !== null && found.isoDate <= latestLoaded) {
    // Still a successful refresh -- we reached NSE and confirmed there's
    // nothing newer -- so it should count for the Dashboard's "Last F&O
    // refresh" timestamp, not just runs that ingested new rows.
    await logFetch(serviceClient, startedAt, new Date(), "success", null);
    return jsonResponse({
      updated: false,
      message: `Already up to date -- latest NSE bhavcopy (${found.isoDate}) is not newer than what's loaded (${latestLoaded}).`,
      latestAvailable: found.isoDate,
      latestLoaded,
    });
  }

  let book: ParsedBhavcopy;
  try {
    book = parseFoBhavcopy(found.csvText, universe);
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    await logFetch(serviceClient, startedAt, new Date(), "failure", `parse: ${message}`);
    return jsonResponse({ error: `Failed to parse bhavcopy: ${message}` }, 500);
  }

  let counts: { futuresRows: number; optionRows: number };
  try {
    counts = await ingest(serviceClient, book);
    await refreshOpenFlags(serviceClient, todayIsoInIst());
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    await logFetch(serviceClient, startedAt, new Date(), "failure", message);
    return jsonResponse({ error: `Ingest failed: ${message}` }, 500);
  }

  // Option data just changed, which feeds the Dashboard's precomputed 5%
  // CSP / 5% ITM PMCC cache -- recompute it here too, so the Dashboard
  // reflects this refresh the instant it finishes. Not fatal if it fails
  // (e.g. migration 0009 not applied yet) -- the real ingest above
  // already succeeded and shouldn't be reported as a failure over a
  // cache that degrades to "N/A" anyway.
  try {
    await recomputeDashboardMetrics(serviceClient);
  } catch (err) {
    console.error("dashboard_fo_metrics recompute failed:", err instanceof Error ? err.message : String(err));
  }

  const finishedAt = new Date();
  await logFetch(serviceClient, startedAt, finishedAt, "success", null);

  return jsonResponse({
    updated: true,
    tradeDate: found.isoDate,
    previousLatest: latestLoaded,
    futuresRows: counts.futuresRows,
    optionRows: counts.optionRows,
    startedAt: startedAt.toISOString(),
    finishedAt: finishedAt.toISOString(),
  });
});
