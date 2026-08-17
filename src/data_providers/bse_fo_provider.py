"""BSE F&O data provider: the daily UDiFF bhavcopy -- same SEBI-mandated
format as src/data_providers/nse_fo_provider.py's NSE source, confirmed
live: identical column schema, identical FinInstrmTp codes, downloadable
with just a browser User-Agent from BSE's own site:

    https://www.bseindia.com/download/Bhavcopy/Derivative/BhavCopy_BSE_FO_0_0_0_YYYYMMDD_F_0000.CSV

Unlike NSE's file, **this one is a plain CSV, not a zip** -- confirmed
live, no zipfile handling needed here. Built specifically so SENSEX and
BANKEX index options (both BSE-listed -- NSE's own bhavcopy never
contains them) have a real F&O data source: before this, a Dhan-synced
SENSEX/BANKEX position could only ever show LTP as N/A (see migration
0018's Index company_type rows and pages/6_Portfolio.py's Dhan LTP
fallback).

**BSE ingestion is index-options-only (`IDO`) -- no stock futures/options
(`STF`/`STO`) at all**, even though BSE's bhavcopy carries rows for many
of the same stock underlyings NSE does: BSE's own stock-level F&O
liquidity is negligible in practice (real trading activity for individual
stocks on BSE derivatives is effectively NSE's, not BSE's), so ingesting
it would just add noisy, unreliable prices for symbols this app already
gets a liquid NSE quote for. NSE stays the sole source for every stock
future/option; BSE is only ever consulted for index options that
genuinely trade there and nowhere else (SENSEX, BANKEX).

The CSV parsing itself is shared with nse_fo_provider.py via
src/data_providers/udiff_bhavcopy.py; this module only owns BSE-specific
URL/download mechanics.
"""
from __future__ import annotations

from datetime import date, timedelta

import requests

from src.data_providers.base import ProviderError
from src.data_providers.udiff_bhavcopy import FOBhavcopy, parse_udiff_bhavcopy

SOURCE_NAME = "bse_fo_bhavcopy"

BHAVCOPY_URL_TEMPLATE = (
    "https://www.bseindia.com/download/Bhavcopy/Derivative/"
    "BhavCopy_BSE_FO_0_0_0_{yyyymmdd}_F_0000.CSV"
)

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

# Deliberately NARROWER than nse_fo_provider.py's allow-list: BSE's own
# stock-level F&O liquidity is negligible, so STF (stock future) and STO
# (stock option) are excluded entirely here -- NSE is the sole stock F&O
# source. Only IDO (index option -- SENSEX, BANKEX) is kept; IDF (index
# future) stays out of scope on both exchanges, see nse_fo_provider.py's
# docstring for why.
_FUTURES_TYPES: set[str] = set()
_OPTION_TYPES = {"IDO"}


def bhavcopy_url(trade_date: date) -> str:
    return BHAVCOPY_URL_TEMPLATE.format(yyyymmdd=trade_date.strftime("%Y%m%d"))


def parse_fo_bhavcopy(
    csv_text: str,
    trade_date: date | None = None,
    universe: set[str] | None = None,
) -> FOBhavcopy:
    """Parse BSE bhavcopy CSV text into the four F&O table shapes -- same
    rules as nse_fo_provider.parse_fo_bhavcopy (identical UDiFF schema),
    just stamped with this module's own `source_name` for traceability."""
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
    """Download one day's F&O bhavcopy, returning its CSV text directly --
    no unzip step (unlike NSE), BSE serves this as a plain CSV.

    Returns None on a 404 (weekend / holiday / not-yet-published), so
    callers can walk backwards to the previous trading day.
    """
    sess = session or requests.Session()
    resp = sess.get(bhavcopy_url(trade_date), headers=_BROWSER_HEADERS, timeout=timeout)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    # A real bhavcopy has a header row plus many data rows; guard against
    # BSE occasionally serving just a header (or a small HTML/PDF error
    # body) with a 200, same reasoning as NSE's zip-size guard.
    if len(resp.content) < 500:
        return None
    text = resp.content.decode("utf-8", errors="replace")
    # A real bhavcopy always starts with this exact header row. **A real
    # bug this caught**: BSE's bot-detection can serve a >500-byte
    # response to a non-browser network origin (confirmed live from
    # Supabase's Edge Runtime -- see bhavcopy.ts's identical check) that
    # isn't the real CSV -- the bad body still parses cleanly, it just
    # matches nothing in `universe`, so a caller sees a silent "0 rows"
    # success instead of a diagnostic error. BSE has no zip/content-type
    # signal to check (always served as application/octet-stream, real or
    # not), so the header text itself is the only available signal.
    if not text.startswith("TradDt,"):
        snippet = " ".join(text[:200].split())
        raise ProviderError(
            f"BSE did not return a bhavcopy CSV for {trade_date} (likely blocked the request) -- "
            f"HTTP {resp.status_code}, {len(resp.content)} bytes. Body starts: {snippet!r}"
        )
    return text


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
    to the most recent published F&O bhavcopy, skipping weekends/holidays.

    `download_bhavcopy_csv` raises `ProviderError` (rather than returning
    None) when a day's response is implausible for a real bhavcopy -- e.g.
    BSE serving its own homepage (HTTP 200, text/html) instead of a 404 for
    a file that simply isn't published yet. **A real bug this caught**:
    `on_or_before` defaults to today, and BSE does exactly that for today's
    not-yet-published file while the market is still open -- this loop used
    to let that exception propagate immediately, aborting on day one and
    never reaching the genuinely available bhavcopy from a few days earlier.
    A single day's hard error is now swallowed and the walk continues; the
    error is only re-raised if every day in the lookback window fails, so a
    genuine persistent block is still surfaced instead of silently
    degrading to "no data found"."""
    sess = session or requests.Session()
    d = on_or_before or date.today()
    last_error: ProviderError | None = None
    for _ in range(max_lookback):
        try:
            parsed = fetch_fo_bhavcopy(d, universe=universe, session=sess)
        except ProviderError as exc:
            last_error = exc
            d -= timedelta(days=1)
            continue
        if parsed is not None and not parsed.is_empty:
            return parsed
        d -= timedelta(days=1)
    if last_error is not None:
        raise last_error
    return None
