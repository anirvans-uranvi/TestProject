// Tests for bhavcopy.ts's zip reader + CSV parser. Run with:
//   deno test supabase/functions/fo-refresh/bhavcopy.test.ts
import { assert, assertEquals } from "jsr:@std/assert@1";
import { bhavcopyUrl, extractFirstZipEntry, parseFoBhavcopy } from "./bhavcopy.ts";

// --- zip reader --------------------------------------------------------

function writeUint16LE(view: DataView, offset: number, value: number) {
  view.setUint16(offset, value, true);
}
function writeUint32LE(view: DataView, offset: number, value: number) {
  view.setUint32(offset, value, true);
}

/** Hand-builds a minimal, valid single-entry ZIP (local file header +
 * central directory + EOCD) so extractFirstZipEntry can be tested without
 * a real NSE file. Mirrors exactly what a real zip tool would produce for
 * one DEFLATE-compressed entry. */
async function buildTestZip(filename: string, content: string): Promise<Uint8Array> {
  const nameBytes = new TextEncoder().encode(filename);
  const contentBytes = new TextEncoder().encode(content);

  const compressedStream = new Blob([contentBytes]).stream().pipeThrough(new CompressionStream("deflate-raw"));
  const compressed = new Uint8Array(await new Response(compressedStream).arrayBuffer());

  // Local file header (30 bytes fixed + filename)
  const localHeaderLen = 30 + nameBytes.length;
  const local = new Uint8Array(localHeaderLen);
  const localView = new DataView(local.buffer);
  writeUint32LE(localView, 0, 0x04034b50);
  writeUint16LE(localView, 4, 20); // version needed
  writeUint16LE(localView, 6, 0); // flags
  writeUint16LE(localView, 8, 8); // method: deflate
  writeUint16LE(localView, 10, 0); // time
  writeUint16LE(localView, 12, 0); // date
  writeUint32LE(localView, 14, 0); // crc32 (unchecked by our reader)
  writeUint32LE(localView, 18, compressed.length); // compressed size
  writeUint32LE(localView, 22, contentBytes.length); // uncompressed size
  writeUint16LE(localView, 26, nameBytes.length); // filename length
  writeUint16LE(localView, 28, 0); // extra length
  local.set(nameBytes, 30);

  const localHeaderOffset = 0;
  const dataStart = localHeaderOffset + localHeaderLen;

  // Central directory header (46 bytes fixed + filename)
  const centralLen = 46 + nameBytes.length;
  const central = new Uint8Array(centralLen);
  const centralView = new DataView(central.buffer);
  writeUint32LE(centralView, 0, 0x02014b50);
  writeUint16LE(centralView, 4, 20); // version made by
  writeUint16LE(centralView, 6, 20); // version needed
  writeUint16LE(centralView, 8, 0); // flags
  writeUint16LE(centralView, 10, 8); // method
  writeUint16LE(centralView, 12, 0); // time
  writeUint16LE(centralView, 14, 0); // date
  writeUint32LE(centralView, 16, 0); // crc32
  writeUint32LE(centralView, 20, compressed.length); // compressed size
  writeUint32LE(centralView, 24, contentBytes.length); // uncompressed size
  writeUint16LE(centralView, 28, nameBytes.length); // filename length
  writeUint16LE(centralView, 30, 0); // extra length
  writeUint16LE(centralView, 32, 0); // comment length
  writeUint16LE(centralView, 34, 0); // disk number start
  writeUint16LE(centralView, 36, 0); // internal attrs
  writeUint32LE(centralView, 38, 0); // external attrs
  writeUint32LE(centralView, 42, localHeaderOffset); // relative offset of local header
  central.set(nameBytes, 46);

  const centralDirOffset = dataStart + compressed.length;

  // End Of Central Directory (22 bytes)
  const eocd = new Uint8Array(22);
  const eocdView = new DataView(eocd.buffer);
  writeUint32LE(eocdView, 0, 0x06054b50);
  writeUint16LE(eocdView, 4, 0); // disk number
  writeUint16LE(eocdView, 6, 0); // disk with central dir
  writeUint16LE(eocdView, 8, 1); // records on this disk
  writeUint16LE(eocdView, 10, 1); // total records
  writeUint32LE(eocdView, 12, central.length); // size of central directory
  writeUint32LE(eocdView, 16, centralDirOffset); // offset of central directory
  writeUint16LE(eocdView, 20, 0); // comment length

  const out = new Uint8Array(local.length + compressed.length + central.length + eocd.length);
  let pos = 0;
  out.set(local, pos);
  pos += local.length;
  out.set(compressed, pos);
  pos += compressed.length;
  out.set(central, pos);
  pos += central.length;
  out.set(eocd, pos);
  return out;
}

Deno.test("extractFirstZipEntry - round-trips a small DEFLATE zip", async () => {
  const original = "hello,world\n1,2,3\n";
  const zip = await buildTestZip("data.csv", original);
  const extracted = await extractFirstZipEntry(zip);
  assertEquals(new TextDecoder().decode(extracted), original);
});

Deno.test("extractFirstZipEntry - round-trips a larger, repetitive payload", async () => {
  const original = "TradDt,TckrSymb,ClsPric\n".repeat(500) + "2026-07-16,RELIANCE,1299.10\n";
  const zip = await buildTestZip("BhavCopy.csv", original);
  const extracted = await extractFirstZipEntry(zip);
  assertEquals(new TextDecoder().decode(extracted), original);
});

Deno.test("extractFirstZipEntry - rejects a non-zip buffer", async () => {
  const notAZip = new TextEncoder().encode("this is not a zip file at all, just plain text padding".repeat(10));
  await assertRejects(() => extractFirstZipEntry(notAZip));
});

async function assertRejects(fn: () => Promise<unknown>): Promise<void> {
  let threw = false;
  try {
    await fn();
  } catch {
    threw = true;
  }
  assert(threw, "expected the promise to reject");
}

// --- URL building --------------------------------------------------------

Deno.test("bhavcopyUrl - formats YYYYMMDD and uses the nsearchives host", () => {
  const url = bhavcopyUrl("2026-07-16");
  assertEquals(url, "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_20260716_F_0000.csv.zip");
});

// --- CSV parsing (mirrors tests/test_nse_fo_provider.py's fixture) -------

const HEADER =
  "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt," +
  "FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric," +
  "LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol," +
  "TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4";

const ROWS = [
  // RELIANCE July future (STF)
  "2026-07-16,2026-07-16,FO,NSE,STF,140001,,RELIANCE,,2026-07-28,2026-07-28,,," +
    "RELIANCE26JULFUT,1300.00,1313.50,1296.00,1299.10,1299.10,1305.00,1296.00,1299.10," +
    "105054000,-756000,20697,1000000.00,15000,F1,500,,,,,",
  // RELIANCE 1300 CE (STO)
  "2026-07-16,2026-07-16,FO,NSE,STO,140002,,RELIANCE,,2026-07-28,2026-07-28,1300.00,CE," +
    "RELIANCE26JUL1300CE,25.00,30.00,22.00,28.00,27.50,29.00,1296.00,28.00,975000,163000," +
    "662,4400000.00,585,F1,500,,,,,",
  // RELIANCE 1300 PE (STO)
  "2026-07-16,2026-07-16,FO,NSE,STO,140003,,RELIANCE,,2026-07-28,2026-07-28,1300.00,PE," +
    "RELIANCE26JUL1300PE,35.05,41.35,32.00,39.00,39.70,38.90,1296.00,39.00,502500,0,183," +
    "1090100.00,140,F1,500,,,,,",
  // NIFTY index future (IDF) -- must be ignored
  "2026-07-16,2026-07-16,FO,NSE,IDF,140004,,NIFTY,,2026-07-30,2026-07-30,,," +
    "NIFTY26JULFUT,25000.00,25100.00,24900.00,25050.00,25050.00,25010.00,25040.00,25050.00," +
    "12000000,50000,300000,9999.00,90000,F1,65,,,,,",
  // TCS future -- outside a {RELIANCE} universe filter
  "2026-07-16,2026-07-16,FO,NSE,STF,140005,,TCS,,2026-07-28,2026-07-28,,," +
    "TCS26JULFUT,3800.00,3820.00,3790.00,3805.00,3805.00,3810.00,3802.00,3805.00," +
    "8000000,10000,5000,1900000.00,3000,F1,175,,,,,",
];
const SAMPLE_CSV = HEADER + "\n" + ROWS.join("\n") + "\n";

Deno.test("parseFoBhavcopy - splits futures and options, ignores index derivatives", () => {
  const book = parseFoBhavcopy(SAMPLE_CSV, new Set(["RELIANCE", "TCS"]));
  assertEquals(book.futuresPrices.length, 2);
  assertEquals(book.optionPrices.length, 2);
  assertEquals(new Set(book.futuresPrices.map((p) => p.symbol)), new Set(["RELIANCE", "TCS"]));
  assertEquals(new Set(book.optionPrices.map((p) => p.symbol)), new Set(["RELIANCE"]));
});

Deno.test("parseFoBhavcopy - universe filter excludes symbols outside it", () => {
  const book = parseFoBhavcopy(SAMPLE_CSV, new Set(["RELIANCE"]));
  assertEquals(book.futuresPrices.length, 1);
  assertEquals(book.futuresPrices[0].symbol, "RELIANCE");
});

Deno.test("parseFoBhavcopy - futures field mapping", () => {
  const book = parseFoBhavcopy(SAMPLE_CSV, new Set(["RELIANCE"]));
  const fut = book.futuresPrices[0];
  assertEquals(fut.expiry_date, "2026-07-28");
  assertEquals(fut.trade_date, "2026-07-16");
  assertEquals(fut.open, 1300.0);
  assertEquals(fut.close, 1299.1);
  assertEquals(fut.settlement_price, 1299.1);
  assertEquals(fut.underlying_price, 1296.0);
  assertEquals(fut.open_interest, 105054000);
  assertEquals(fut.change_in_oi, -756000);
  assertEquals(fut.volume, 20697);
  const contract = book.futuresContracts[0];
  assertEquals(contract.lot_size, 500);
  assertEquals(contract.contract_name, "RELIANCE26JULFUT");
});

Deno.test("parseFoBhavcopy - option field mapping (CE and PE)", () => {
  const book = parseFoBhavcopy(SAMPLE_CSV, new Set(["RELIANCE"]));
  const ce = book.optionPrices.find((p) => p.option_type === "CE")!;
  const pe = book.optionPrices.find((p) => p.option_type === "PE")!;
  assertEquals(ce.strike_price, 1300.0);
  assertEquals(ce.close, 28.0);
  assertEquals(ce.last_price, 27.5);
  assertEquals(ce.open_interest, 975000);
  assertEquals(pe.strike_price, 1300.0);
  assertEquals(pe.close, 39.0);
  assertEquals(pe.open_interest, 502500);
});

Deno.test("parseFoBhavcopy - empty/missing numeric cells parse to null, not a crash", () => {
  const csv = HEADER + "\n" +
    "2026-07-16,2026-07-16,FO,NSE,STO,140006,,RELIANCE,,2026-07-28,2026-07-28,1400.00,CE," +
    "RELIANCE26JUL1400CE,,,,,,,,,,,,,,F1,,,,,,\n";
  const book = parseFoBhavcopy(csv, new Set(["RELIANCE"]));
  assertEquals(book.optionPrices.length, 1);
  const opt = book.optionPrices[0];
  assertEquals(opt.last_price, null);
  assertEquals(opt.open_interest, null);
  assertEquals(opt.strike_price, 1400.0);
});

Deno.test("parseFoBhavcopy - tradeDate is the first row's trade date", () => {
  const book = parseFoBhavcopy(SAMPLE_CSV, new Set(["RELIANCE", "TCS"]));
  assertEquals(book.tradeDate, "2026-07-16");
});
