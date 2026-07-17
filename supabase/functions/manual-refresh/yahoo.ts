// Unofficial Yahoo Finance endpoints, verified live (real curl requests)
// before writing this port -- see docs/CODEBASE_GUIDE.md and README
// "Limitations" for the caveats. Both endpoints are undocumented and
// Yahoo can change or block them at any time:
//   - /v8/finance/chart/{symbol}.NS -- prices + dividend events, NO auth needed.
//   - /v10/finance/quoteSummary/{symbol}.NS -- PE/PEG/EPS/market cap, needs
//     a session cookie + "crumb" token obtained via a separate handshake.
//     This is real added fragility beyond what Python's `yfinance` library
//     already manages for us on the cron-refresh side of this project.

const USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36";

function yahooSymbol(symbol: string): string {
  return `${symbol}.NS`;
}

function unixToIsoDate(unixSeconds: number): string {
  return new Date(unixSeconds * 1000).toISOString().slice(0, 10);
}

export interface PricePoint {
  tradeDate: string; // YYYY-MM-DD
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  adjustedClose: number | null;
  volume: number | null;
}

export interface DividendEventRaw {
  exDate: string; // YYYY-MM-DD
  amountPerShare: number;
}

export interface ChartData {
  points: PricePoint[];
  dividends: DividendEventRaw[];
}

/** Price history + dividend events in one call -- mirrors what
 * YFinancePriceProvider.get_historical_daily() +
 * YFinanceFundamentalsProvider.get_dividend_history() do in Python,
 * combined since Yahoo's chart endpoint already returns both. */
export async function fetchChartData(symbol: string, rangeParam = "3mo"): Promise<ChartData> {
  const url = `https://query1.finance.yahoo.com/v8/finance/chart/${yahooSymbol(symbol)}` +
    `?range=${rangeParam}&interval=1d&events=div`;
  const resp = await fetch(url, { headers: { "User-Agent": USER_AGENT } });
  if (!resp.ok) {
    throw new Error(`chart fetch failed for ${symbol}: HTTP ${resp.status}`);
  }
  const json = await resp.json();
  const result = json?.chart?.result?.[0];
  if (!result) {
    const err = json?.chart?.error;
    throw new Error(`chart fetch returned no result for ${symbol}: ${err ? JSON.stringify(err) : "unknown response shape"}`);
  }

  const timestamps: number[] = result.timestamp ?? [];
  const quote = result.indicators?.quote?.[0] ?? {};
  const adjClose: (number | null)[] = result.indicators?.adjclose?.[0]?.adjclose ?? [];

  const points: PricePoint[] = timestamps.map((ts, i) => ({
    tradeDate: unixToIsoDate(ts),
    open: quote.open?.[i] ?? null,
    high: quote.high?.[i] ?? null,
    low: quote.low?.[i] ?? null,
    close: quote.close?.[i] ?? null,
    // Dhan/yfinance-parity: prefer adjusted close, fall back to raw close.
    adjustedClose: adjClose[i] ?? quote.close?.[i] ?? null,
    volume: quote.volume?.[i] ?? null,
  }));

  const dividendsRaw: Record<string, { amount: number; date: number }> = result.events?.dividends ?? {};
  const dividends: DividendEventRaw[] = Object.values(dividendsRaw).map((d) => ({
    exDate: unixToIsoDate(d.date),
    amountPerShare: d.amount,
  }));

  return { points, dividends };
}

// ---------------------------------------------------------------------
// Fundamentals (crumb-authenticated)
// ---------------------------------------------------------------------

interface CrumbSession {
  cookie: string;
  crumb: string;
  fetchedAt: number;
}

let cachedSession: CrumbSession | null = null;
const SESSION_TTL_MS = 10 * 60 * 1000; // re-fetch periodically; crumbs can expire server-side

async function fetchFreshCrumbSession(): Promise<CrumbSession> {
  const cookieResp = await fetch("https://fc.yahoo.com", { headers: { "User-Agent": USER_AGENT } });
  const setCookie = cookieResp.headers.get("set-cookie") ?? "";
  const cookie = setCookie.split(";")[0];
  if (!cookie) {
    throw new Error("failed to obtain a Yahoo Finance session cookie");
  }

  const crumbResp = await fetch("https://query1.finance.yahoo.com/v1/test/getcrumb", {
    headers: { "User-Agent": USER_AGENT, Cookie: cookie },
  });
  const crumb = (await crumbResp.text()).trim();
  if (!crumb || crumb.includes("<html")) {
    throw new Error("failed to obtain a Yahoo Finance crumb token");
  }
  return { cookie, crumb, fetchedAt: Date.now() };
}

async function getCrumbSession(forceFresh = false): Promise<CrumbSession> {
  if (!forceFresh && cachedSession && Date.now() - cachedSession.fetchedAt < SESSION_TTL_MS) {
    return cachedSession;
  }
  cachedSession = await fetchFreshCrumbSession();
  return cachedSession;
}

export interface FundamentalsRaw {
  peRatio: number | null;
  pegRatio: number | null;
  eps: number | null;
  marketCap: number | null;
  week52High: number | null;
  week52Low: number | null;
}

async function requestFundamentals(symbol: string, session: CrumbSession): Promise<FundamentalsRaw | null> {
  const url = `https://query2.finance.yahoo.com/v10/finance/quoteSummary/${yahooSymbol(symbol)}` +
    `?modules=defaultKeyStatistics,summaryDetail&crumb=${encodeURIComponent(session.crumb)}`;
  const resp = await fetch(url, { headers: { "User-Agent": USER_AGENT, Cookie: session.cookie } });
  if (!resp.ok) return null;
  const json = await resp.json();
  const result = json?.quoteSummary?.result?.[0];
  if (!result) return null;

  const summaryDetail = result.summaryDetail ?? {};
  const keyStats = result.defaultKeyStatistics ?? {};
  return {
    peRatio: summaryDetail.trailingPE?.raw ?? null,
    pegRatio: keyStats.pegRatio?.raw ?? null,
    eps: keyStats.trailingEps?.raw ?? null,
    marketCap: summaryDetail.marketCap?.raw ?? null,
    week52High: summaryDetail.fiftyTwoWeekHigh?.raw ?? null,
    week52Low: summaryDetail.fiftyTwoWeekLow?.raw ?? null,
  };
}

/** PE/PEG/EPS/market cap -- mirrors YFinanceFundamentalsProvider.
 * get_fundamentals() in Python. Retries once with a forced-fresh
 * crumb/cookie session if the first attempt fails, since a cached crumb
 * can go stale server-side sooner than our local TTL expects. */
export async function fetchFundamentals(symbol: string): Promise<FundamentalsRaw> {
  const session = await getCrumbSession();
  const first = await requestFundamentals(symbol, session);
  if (first) return first;

  const freshSession = await getCrumbSession(true);
  const second = await requestFundamentals(symbol, freshSession);
  if (second) return second;

  throw new Error(`quoteSummary fetch failed for ${symbol} even after a fresh crumb session`);
}
