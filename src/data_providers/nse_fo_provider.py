"""NSE F&O data provider: the daily UDiFF bhavcopy.

Yahoo/yfinance carries no NSE derivatives, and NSE's live option-chain API
serves hollow JSON to scripts, so the reliable free source is the
end-of-day F&O bhavcopy -- one zip per trading day, downloadable with just
a browser User-Agent (no cookie handshake) from the `nsearchives` host:

    https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_YYYYMMDD_F_0000.csv.zip

Each row is one contract's full trading-day summary: OHLC, LTP, previous
close, settlement, underlying (spot) price, open interest + change, volume,
turnover, number of trades, expiry, strike, option type, and lot size.
Instrument types: STF = stock future, STO = stock option, IDO = index
option (NIFTY/BANKNIFTY -- see migration 0018's Index company_type rows).
IDF (index future) stays out of scope -- no Index position on this app
needs a futures LTP today (see pages/6_My_Broker.py's Dhan sync, which
only deals in option positions), so it isn't worth widening for yet.

The CSV row shape itself (SEBI's UDiFF format) is shared with BSE's own
F&O bhavcopy -- confirmed live, identical columns and instrument-type
codes -- so the actual parsing lives in
src/data_providers/udiff_bhavcopy.py; this module only owns the
NSE-specific URL/download (zip) mechanics. See bse_fo_provider.py for the
BSE counterpart.

The HTTP download and the CSV parse are deliberately separated so the parse
is unit-testable against an inline fixture with no network.
"""
from __future__ import annotations

import io
import zipfile
from datetime import date, timedelta

import requests

from src.data_providers.udiff_bhavcopy import FOBhavcopy, parse_udiff_bhavcopy

SOURCE_NAME = "nse_fo_bhavcopy"

BHAVCOPY_URL_TEMPLATE = (
    "https://nsearchives.nseindia.com/content/fo/"
    "BhavCopy_NSE_FO_0_0_0_{yyyymmdd}_F_0000.csv.zip"
)

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

# Bhavcopy FinInstrmTp codes we care about: stock futures/options, plus
# index options (IDO) -- see the file header for why index futures (IDF)
# stay out of scope.
_FUTURES_TYPES = {"STF"}
_OPTION_TYPES = {"STO", "IDO"}


def bhavcopy_url(trade_date: date) -> str:
    return BHAVCOPY_URL_TEMPLATE.format(yyyymmdd=trade_date.strftime("%Y%m%d"))


def parse_fo_bhavcopy(
    csv_text: str,
    trade_date: date | None = None,
    universe: set[str] | None = None,
) -> FOBhavcopy:
    """Parse bhavcopy CSV text into the four F&O table shapes.

    Keeps stock futures (STF), stock options (STO), and index options
    (IDO); ignores index futures (IDF -- see the module docstring for why).
    If `universe` is given, keeps only those underlying symbols. `trade_date`
    defaults to each row's own TradDt.
    """
    return parse_udiff_bhavcopy(
        csv_text,
        source_name=SOURCE_NAME,
        futures_types=_FUTURES_TYPES,
        option_types=_OPTION_TYPES,
        trade_date=trade_date,
        universe=universe,
    )


def download_bhavcopy_csv(
    trade_date: date, session: requests.Session | None = None, timeout: int = 30
) -> str | None:
    """Download and unzip one day's F&O bhavcopy, returning its CSV text.

    Returns None on a 404 (weekend / holiday / not-yet-published), so callers
    can walk backwards to the previous trading day.
    """
    sess = session or requests.Session()
    resp = sess.get(bhavcopy_url(trade_date), headers=_BROWSER_HEADERS, timeout=timeout)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    # A real bhavcopy is a zip; NSE occasionally serves a small HTML/PDF error
    # body with a 200 -- guard against that.
    if len(resp.content) < 1000:
        return None
    try:
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
    except zipfile.BadZipFile:
        return None
    return zf.read(zf.namelist()[0]).decode("utf-8", errors="replace")


def fetch_fo_bhavcopy(
    trade_date: date,
    universe: set[str] | None = None,
    session: requests.Session | None = None,
) -> FOBhavcopy | None:
    """Download + parse one day's bhavcopy. None if that day has no file."""
    csv_text = download_bhavcopy_csv(trade_date, session=session)
    if csv_text is None:
        return None
    return parse_fo_bhavcopy(csv_text, trade_date=trade_date, universe=universe)


def latest_available_bhavcopy(
    universe: set[str] | None = None,
    on_or_before: date | None = None,
    max_lookback: int = 7,
    session: requests.Session | None = None,
) -> FOBhavcopy | None:
    """Walk back from `on_or_before` (default today) up to `max_lookback` days
    to the most recent published F&O bhavcopy, skipping weekends/holidays."""
    sess = session or requests.Session()
    d = on_or_before or date.today()
    for _ in range(max_lookback):
        parsed = fetch_fo_bhavcopy(d, universe=universe, session=sess)
        if parsed is not None and not parsed.is_empty:
            return parsed
        d -= timedelta(days=1)
    return None
