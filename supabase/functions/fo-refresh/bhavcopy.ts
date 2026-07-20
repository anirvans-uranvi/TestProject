// NSE F&O UDiFF bhavcopy: download, unzip, and parse -- the TypeScript
// port of src/data_providers/nse_fo_provider.py, for the on-demand
// "F&O Data Refresh" Edge Function. See that Python module's file header
// for why the bhavcopy (not yfinance, not NSE's live option-chain API) is
// the only reliable source.
//
// The bhavcopy is a single-entry ZIP. Rather than pull in an external zip
// library, this reads it directly via the ZIP Central Directory (accurate
// regardless of whether the local file header used a trailing data
// descriptor) plus the Web Streams API's native
// DecompressionStream("deflate-raw") -- both natively available in Deno's
// Edge Runtime, so no third-party dependency is needed for a format this
// constrained (one file, standard DEFLATE).

// Matches src/data_providers/nse_fo_provider.py's _BROWSER_HEADERS exactly
// -- the Python version (confirmed working live, run from a normal dev
// machine) sends Accept/Accept-Language alongside User-Agent; this
// TypeScript port originally sent User-Agent only, and NSE's bot-detection
// treated requests from Supabase's Edge Runtime infrastructure (a
// different network origin than a dev machine) as suspicious enough to
// serve an HTML challenge/block page instead of the real zip -- which
// then failed zip parsing with a confusing "not a valid zip" error rather
// than a clear "you got blocked" one. Matching Python's header set is a
// low-risk fix; the content-type/snippet check below is the real
// safety net that makes the next failure (if any) self-diagnosing.
const REQUEST_HEADERS: Record<string, string> = {
  "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
  "Accept": "*/*",
  "Accept-Language": "en-US,en;q=0.9",
};
const SOURCE_NAME = "nse_fo_bhavcopy_edge";

// The original fetch() call here had NO timeout at all -- a real incident
// this caused: findLatestAvailableBhavcopy() walks back up to
// MAX_LOOKBACK_DAYS days, and a single hung connection to NSE (its
// bot-detection layer is known to behave unusually -- see the
// looksLikeZipContentType note below) blocked the whole Edge Function
// invocation indefinitely, well past the Streamlit client's own request
// timeout, in a way that left the browser tab spinning and required a
// full app reboot to clear rather than surfacing a clean error. Every
// fetch to NSE is now bounded by this timeout.
const FETCH_TIMEOUT_MS = 15_000;

const EOCD_SIGNATURE = 0x06054b50;
const CENTRAL_DIR_SIGNATURE = 0x02014b50;
const LOCAL_FILE_SIGNATURE = 0x04034b50;

function findEndOfCentralDirectory(buf: Uint8Array): number {
  const minEocdSize = 22;
  const maxCommentSize = 65535;
  const searchStart = Math.max(0, buf.length - minEocdSize - maxCommentSize);
  for (let i = buf.length - minEocdSize; i >= searchStart; i--) {
    if (buf[i] === 0x50 && buf[i + 1] === 0x4b && buf[i + 2] === 0x05 && buf[i + 3] === 0x06) {
      return i;
    }
  }
  throw new Error("Not a valid zip file (End Of Central Directory record not found)");
}

/** Extracts the first entry of a (single-entry) zip archive as raw bytes. */
export async function extractFirstZipEntry(zipBytes: Uint8Array): Promise<Uint8Array> {
  const eocdOffset = findEndOfCentralDirectory(zipBytes);
  const view = new DataView(zipBytes.buffer, zipBytes.byteOffset, zipBytes.byteLength);
  if (view.getUint32(eocdOffset, true) !== EOCD_SIGNATURE) {
    throw new Error("EOCD signature mismatch");
  }
  const centralDirOffset = view.getUint32(eocdOffset + 16, true);

  if (view.getUint32(centralDirOffset, true) !== CENTRAL_DIR_SIGNATURE) {
    throw new Error("Central directory signature mismatch");
  }
  const compressionMethod = view.getUint16(centralDirOffset + 10, true);
  const compressedSize = view.getUint32(centralDirOffset + 20, true);
  const localHeaderOffset = view.getUint32(centralDirOffset + 42, true);

  if (view.getUint32(localHeaderOffset, true) !== LOCAL_FILE_SIGNATURE) {
    throw new Error("Local file header signature mismatch");
  }
  // The local header's OWN filename/extra-field lengths locate the start
  // of the compressed data reliably, even when the "data descriptor"
  // general-purpose bit made the local header's own size/CRC fields
  // untrustworthy (the central directory's sizes, read above, are always
  // authoritative).
  const localFileNameLen = view.getUint16(localHeaderOffset + 26, true);
  const localExtraLen = view.getUint16(localHeaderOffset + 28, true);
  const dataStart = localHeaderOffset + 30 + localFileNameLen + localExtraLen;
  // Copy into a plain ArrayBuffer-backed Uint8Array: a subarray's `.buffer`
  // is typed as ArrayBufferLike (could be a SharedArrayBuffer), which
  // Blob's constructor type doesn't accept.
  const compressedData = zipBytes.slice(dataStart, dataStart + compressedSize);

  if (compressionMethod === 0) {
    return compressedData; // stored, no compression
  }
  if (compressionMethod !== 8) {
    throw new Error(`Unsupported zip compression method: ${compressionMethod}`);
  }
  const stream = new Blob([compressedData]).stream().pipeThrough(new DecompressionStream("deflate-raw"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

export function bhavcopyUrl(isoDate: string): string {
  const yyyymmdd = isoDate.replaceAll("-", "");
  return `https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_${yyyymmdd}_F_0000.csv.zip`;
}

function isoDateMinusDays(isoDate: string, days: number): string {
  const d = new Date(`${isoDate}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() - days);
  return d.toISOString().slice(0, 10);
}

export function looksLikeZipContentType(contentType: string | null): boolean {
  if (!contentType) return true; // NSE doesn't always set one; don't reject on absence alone
  const ct = contentType.toLowerCase();
  return ct.includes("zip") || ct.includes("octet-stream") || ct.includes("binary");
}

/** Downloads + unzips one day's bhavcopy. Null on a 404 (weekend/holiday/
 * not yet published) or an implausibly small response (NSE occasionally
 * serves a small HTML/PDF error body with a 200), so callers can walk
 * back to the previous trading day.
 *
 * Throws a diagnostic error (status, content-type, byte length, and a
 * text snippet of the body) rather than a bare "not a valid zip" when the
 * response clearly isn't one -- NSE's bot-detection can serve an HTML
 * challenge/block page with a 200 status to requests it doesn't like
 * (confirmed: this happened for this exact function's Edge Runtime
 * origin even though the identical request worked fine from a normal dev
 * machine), and a bare zip-parse failure gives no way to tell that apart
 * from a genuinely corrupt download. */
export async function fetchBhavcopyText(isoDate: string): Promise<string | null> {
  let resp: Response;
  try {
    resp = await fetch(bhavcopyUrl(isoDate), { headers: REQUEST_HEADERS, signal: AbortSignal.timeout(FETCH_TIMEOUT_MS) });
  } catch (err) {
    // A hung/refused connection is treated the same as a 404 -- "this day
    // isn't reachable, try the previous one" -- so one bad day (a
    // transient NSE stall, most likely on today's not-yet-published file)
    // can't block discovery of an earlier, genuinely available day. This
    // bounds findLatestAvailableBhavcopy()'s total worst-case runtime to
    // roughly maxLookback * FETCH_TIMEOUT_MS instead of hanging.
    const reason = err instanceof Error ? err.message : String(err);
    console.warn(`bhavcopy fetch for ${isoDate} timed out or failed (${reason}); treating as unavailable`);
    return null;
  }
  if (resp.status === 404) return null;
  if (!resp.ok) throw new Error(`bhavcopy fetch failed for ${isoDate}: HTTP ${resp.status}`);

  const contentType = resp.headers.get("content-type");
  const buf = new Uint8Array(await resp.arrayBuffer());
  if (buf.length < 1000) return null;

  if (!looksLikeZipContentType(contentType)) {
    const snippet = new TextDecoder("utf-8", { fatal: false }).decode(buf.slice(0, 200)).replace(/\s+/g, " ").trim();
    throw new Error(
      `NSE did not return a zip file for ${isoDate} (likely blocked the request) -- ` +
        `HTTP ${resp.status}, content-type "${contentType}", ${buf.length} bytes. Body starts: "${snippet}"`,
    );
  }

  try {
    const csvBytes = await extractFirstZipEntry(buf);
    return new TextDecoder("utf-8").decode(csvBytes);
  } catch (err) {
    const snippet = new TextDecoder("utf-8", { fatal: false }).decode(buf.slice(0, 200)).replace(/\s+/g, " ").trim();
    const cause = err instanceof Error ? err.message : String(err);
    throw new Error(
      `Failed to unzip bhavcopy for ${isoDate}: ${cause} -- ` +
        `HTTP ${resp.status}, content-type "${contentType}", ${buf.length} bytes. Body starts: "${snippet}"`,
    );
  }
}

export interface FoundBhavcopy {
  isoDate: string;
  csvText: string;
}

/** Walks back from `onOrBefore` up to `maxLookback` days to the most
 * recent published bhavcopy, skipping weekends/holidays. */
export async function findLatestAvailableBhavcopy(
  onOrBefore: string,
  maxLookback = 7,
): Promise<FoundBhavcopy | null> {
  let d = onOrBefore;
  for (let i = 0; i < maxLookback; i++) {
    const csvText = await fetchBhavcopyText(d);
    if (csvText !== null) return { isoDate: d, csvText };
    d = isoDateMinusDays(d, 1);
  }
  return null;
}

// --- CSV parsing -----------------------------------------------------

const FUTURES_TYPES = new Set(["STF"]);
const OPTION_TYPES = new Set(["STO"]);

export interface FuturesContractRow {
  symbol: string;
  expiry_date: string;
  contract_name: string | null;
  nse_token: string | null;
  lot_size: number | null;
  is_open: boolean;
  first_seen_date: string;
  last_seen_date: string;
}

export interface FuturesDailyPriceRow {
  symbol: string;
  expiry_date: string;
  trade_date: string;
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  last_price: number | null;
  prev_close: number | null;
  settlement_price: number | null;
  underlying_price: number | null;
  open_interest: number | null;
  change_in_oi: number | null;
  volume: number | null;
  turnover: number | null;
  num_trades: number | null;
  source: string;
}

export interface OptionContractRow extends FuturesContractRow {
  strike_price: number;
  option_type: "CE" | "PE";
}

export interface OptionDailyPriceRow extends FuturesDailyPriceRow {
  strike_price: number;
  option_type: "CE" | "PE";
}

export interface ParsedBhavcopy {
  tradeDate: string;
  futuresContracts: FuturesContractRow[];
  futuresPrices: FuturesDailyPriceRow[];
  optionContracts: OptionContractRow[];
  optionPrices: OptionDailyPriceRow[];
}

function parseNum(v: string | undefined): number | null {
  if (v === undefined) return null;
  const s = v.trim();
  if (s === "") return null;
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
}

function parseIntField(v: string | undefined): number | null {
  const n = parseNum(v);
  return n === null ? null : Math.round(n);
}

/** Parses bhavcopy CSV text into the four F&O table shapes. Keeps only
 * stock futures (STF) and stock options (STO) for symbols in `universe`;
 * ignores index derivatives (IDF/IDO) and everything outside the universe.
 * No quoted/embedded-comma fields in this file format, so a plain split
 * is sufficient (mirrors csv.DictReader's simplicity in the Python port).
 */
export function parseFoBhavcopy(csvText: string, universe: Set<string>): ParsedBhavcopy {
  const lines = csvText.split(/\r?\n/).filter((l) => l.length > 0);
  if (lines.length === 0) {
    throw new Error("Empty bhavcopy CSV");
  }
  const header = lines[0].split(",").map((h) => h.trim());
  const idx: Record<string, number> = {};
  header.forEach((h, i) => {
    idx[h] = i;
  });

  const result: ParsedBhavcopy = {
    tradeDate: "",
    futuresContracts: [],
    futuresPrices: [],
    optionContracts: [],
    optionPrices: [],
  };

  for (let li = 1; li < lines.length; li++) {
    const cols = lines[li].split(",");
    const get = (name: string): string | undefined => cols[idx[name]];

    const instr = (get("FinInstrmTp") ?? "").trim();
    if (!FUTURES_TYPES.has(instr) && !OPTION_TYPES.has(instr)) continue;

    const symbol = (get("TckrSymb") ?? "").trim();
    if (!symbol || !universe.has(symbol)) continue;

    const tradeDate = (get("TradDt") ?? "").trim().slice(0, 10);
    const expiry = (get("XpryDt") ?? "").trim().slice(0, 10);
    if (!expiry) continue;
    if (!result.tradeDate && tradeDate) result.tradeDate = tradeDate;

    const lotSize = parseIntField(get("NewBrdLotQty"));
    const contractName = (get("FinInstrmNm") ?? "").trim() || null;
    const nseToken = (get("FinInstrmId") ?? "").trim() || null;

    const commonPrice = {
      symbol,
      expiry_date: expiry,
      trade_date: tradeDate,
      open: parseNum(get("OpnPric")),
      high: parseNum(get("HghPric")),
      low: parseNum(get("LwPric")),
      close: parseNum(get("ClsPric")),
      last_price: parseNum(get("LastPric")),
      prev_close: parseNum(get("PrvsClsgPric")),
      settlement_price: parseNum(get("SttlmPric")),
      underlying_price: parseNum(get("UndrlygPric")),
      open_interest: parseIntField(get("OpnIntrst")),
      change_in_oi: parseIntField(get("ChngInOpnIntrst")),
      volume: parseIntField(get("TtlTradgVol")),
      turnover: parseNum(get("TtlTrfVal")),
      num_trades: parseIntField(get("TtlNbOfTxsExctd")),
      source: SOURCE_NAME,
    };

    if (FUTURES_TYPES.has(instr)) {
      result.futuresContracts.push({
        symbol,
        expiry_date: expiry,
        contract_name: contractName,
        nse_token: nseToken,
        lot_size: lotSize,
        is_open: true,
        first_seen_date: tradeDate,
        last_seen_date: tradeDate,
      });
      result.futuresPrices.push(commonPrice);
    } else {
      const strike = parseNum(get("StrkPric"));
      const optnRaw = (get("OptnTp") ?? "").trim().toUpperCase();
      if (strike === null || (optnRaw !== "CE" && optnRaw !== "PE")) continue;
      const optn = optnRaw as "CE" | "PE";
      result.optionContracts.push({
        symbol,
        expiry_date: expiry,
        strike_price: strike,
        option_type: optn,
        contract_name: contractName,
        nse_token: nseToken,
        lot_size: lotSize,
        is_open: true,
        first_seen_date: tradeDate,
        last_seen_date: tradeDate,
      });
      result.optionPrices.push({ ...commonPrice, strike_price: strike, option_type: optn });
    }
  }

  return result;
}
