"""Live price provider backed by DhanHQ API v2 (https://dhanhq.co/docs/v2/).

Coverage note (see README "Limitations"): Dhan is a broker/market-data API.
It provides OHLCV price data only -- it does NOT expose PE, PEG, or
dividend/corporate-action data, so this module implements PriceDataProvider
only. Fundamentals come from a separate FundamentalsDataProvider.

Endpoint shapes below follow the DhanHQ v2 docs as researched; verify
against a live account/sandbox before relying on this in production, as
Dhan has changed response shapes across releases.
"""
from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta

import httpx
import pandas as pd
import pytz
from supabase import Client
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.data_providers.base import PriceDataProvider, ProviderError
from src.models.enums import FetchStatus, FetchType
from src.models.fetch_log import ProviderFetchLog
from src.models.market_data import PricePoint, Quote
from src.repositories import dhan_instrument_repo, fetch_log_repo
from src.utils.timezones import now_ist

IST = pytz.timezone("Asia/Kolkata")

BASE_URL = "https://api.dhan.co/v2"
INSTRUMENT_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
HISTORICAL_ENDPOINT = f"{BASE_URL}/charts/historical"
LTP_ENDPOINT = f"{BASE_URL}/marketfeed/ltp"

# Dhan documents per-second rate limits on data endpoints; stay comfortably
# under them with a simple client-side throttle.
_MIN_REQUEST_INTERVAL_SECONDS = 0.25
_last_request_lock = threading.Lock()
_last_request_at = 0.0


def _throttle() -> None:
    global _last_request_at
    with _last_request_lock:
        wait = _MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


# In-memory, shared-across-calls-within-this-process cache, keyed by
# today's IST calendar date so it naturally rolls over without a process
# restart -- deliberately NOT a Client-keyed @lru_cache (a Client object
# isn't a stable/hashable identity across sessions, and keying on it would
# defeat sharing this cache across users within one process). Below this,
# dhan_equity_instruments/dhan_fo_instruments (migration 0035) persist the
# derived (filtered) results across *processes* too -- see
# _load_instrument_master's docstring. _raw_master_cache is a third,
# smaller-scoped cache: the *unfiltered* CSV, shared between
# _download_equity_master and _download_fo_master so a cache-cold refresh
# (both loaders missing today's data at once, the common case -- Stock &
# Option Data Refresh runs them concurrently) downloads Dhan's ~211,742-row
# file ONCE, not twice. Never persisted to Supabase -- unlike the two
# derived slices, the raw file is mostly rows neither loader wants
# (currency, commodity, other segments), so there's nothing worth storing
# beyond what dhan_equity_instruments/dhan_fo_instruments already keep.
_equity_master_cache: dict[str, pd.DataFrame] = {}
_fo_master_cache: dict[str, pd.DataFrame] = {}
_raw_master_cache: dict[str, pd.DataFrame] = {}
_master_cache_lock = threading.Lock()


def _get_raw_instrument_master() -> pd.DataFrame:
    """Downloads Dhan's full, unfiltered instrument master CSV
    (~211,742 rows across every exchange/segment/instrument type) at most
    once per IST calendar day, in-process. Held under _master_cache_lock
    for the *entire* download -- not just the cache check -- so that when
    the equity and F&O legs of a refresh run concurrently
    (src/utils/refresh_bar.py's ThreadPoolExecutor) and both find today's
    cache cold, the second one to arrive blocks and then reuses the first
    one's result instead of starting its own duplicate download of the
    same large file. Confirmed live: before this, a cache-cold refresh
    downloaded the same CSV twice, concurrently, competing for the same
    outbound bandwidth -- slower in aggregate than one download, not
    faster, despite running "in parallel".

    Always returns a fresh `.copy()`, never the cached object itself --
    confirmed live as a second, more serious bug the first version of
    this cache introduced: pandas is not thread-safe for concurrent
    *reads* of one shared DataFrame instance (lazy internal caching --
    e.g. block consolidation on first column access -- mutates C-level
    state even on what looks like a read-only `.rename()`/boolean-filter/
    `.str` operation), so handing the *same* object to both legs' filter
    functions while they ran concurrently silently corrupted the result
    for one or both (a live account saw "15 of 61" equities and "0 of
    348" F&O resolve, down from 61/61 and 333+/351 before this cache
    existed at all). The `.copy()` is cheap relative to the network
    download it avoids repeating -- it only duplicates already-in-memory
    data, no I/O -- and gives each leg's subsequent `.rename()`/filtering
    a fully independent object with no cross-thread interference."""
    today = now_ist().date().isoformat()
    with _master_cache_lock:
        cached = _raw_master_cache.get(today)
        if cached is not None:
            return cached.copy()
        try:
            df = pd.read_csv(INSTRUMENT_MASTER_URL, low_memory=False)
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"failed to download Dhan instrument master: {exc}") from exc
        _raw_master_cache.clear()  # only "today" is ever relevant
        _raw_master_cache[today] = df
        return df.copy()


def _download_equity_master(raw_df: pd.DataFrame) -> pd.DataFrame:
    """Filters the already-downloaded raw instrument master
    (_get_raw_instrument_master) down to the NSE-equity slice. Column
    names in Dhan's compact CSV have varied across releases, so we
    resolve them by fuzzy match instead of hardcoding exact headers.

    The equity filter used to test whether the SEGMENT column *contained*
    "EQ" -- confirmed live to be wrong: a real download's segment column
    (`SEM_SEGMENT`) holds a single letter per asset class ('E' for
    equity, 'D' for derivatives, 'C' for currency, ...), never the
    two-letter substring "EQ" the old filter looked for, so it matched
    *zero* rows and silently broke every equity resolution -- get_quotes
    then returned {} for a whole batch with no exception at all (nothing
    to catch: an *empty* instrument master isn't a download/parse
    failure, just a completely unmatched filter). "EQ" does appear
    elsewhere in a real row (`SEM_SERIES`), which is what makes this kind
    of near-miss so easy to not notice without a live download. Filters
    on the instrument-type column instead (`SEM_INSTRUMENT_NAME ==
    'EQUITY'`), the same exact-match-on-instrument-type approach
    `_download_fo_master` already uses for FUTSTK/OPTSTK.
    """
    df = raw_df

    def find_col(*keywords: str) -> str | None:
        for col in df.columns:
            upper = col.upper()
            if all(k in upper for k in keywords):
                return col
        return None

    sec_id_col = find_col("SECURITY", "ID")
    symbol_col = find_col("TRADING", "SYMBOL") or find_col("SYMBOL")
    exch_col = find_col("EXCH")
    instrument_col = find_col("INSTRUMENT", "NAME")

    if not all([sec_id_col, symbol_col]):
        raise ProviderError("Dhan instrument master schema unrecognized; update column resolution")

    df = df.rename(columns={sec_id_col: "security_id", symbol_col: "trading_symbol"})
    if exch_col:
        df = df[df[exch_col].astype(str).str.upper().str.contains("NSE", na=False)]
    if instrument_col:
        df = df[df[instrument_col].astype(str).str.upper() == "EQUITY"]
    if df.empty:
        raise ProviderError(
            "Dhan instrument master matched zero NSE equity rows -- exchange/instrument-type column "
            "resolution is likely wrong for this download; update column resolution"
        )
    df["security_id"] = df["security_id"].astype(str)
    return df[["security_id", "trading_symbol"]].drop_duplicates("trading_symbol")


def _load_instrument_master(client: Client | None = None) -> pd.DataFrame:
    """Returns the NSE-equity slice of Dhan's instrument master, checking
    (in order) an in-memory same-process cache, then -- if `client` is
    given -- the shared `dhan_equity_instruments` table (migration 0035).

    Deliberately does NOT fall back to a real Dhan download when `client`
    is given and the DB cache is empty/stale -- that used to happen here
    on request (this app's own "Stock & Option Data Refresh" button would
    silently pay for a full ~211,742-row CSV download whenever it judged
    the cache "not fresh today"), which made that button unpredictably
    slow depending on which process/worker happened to handle a given
    click. `refresh_dhan_instrument_master` (Settings' "Refresh Instrument
    Master - Dhan" button) is now the *only* thing that downloads and
    persists this data; a `client`-backed call here just reads whatever's
    already there, however old, and raises a clear `ProviderError`
    pointing at that button if the table has never been populated at all
    (get_quotes/get_fo_quotes let this propagate as a real, actionable
    error rather than silently resolving nothing). `client=None` (e.g. a
    script/cron context with no Supabase wiring at all) keeps the old
    real-download fallback, since there's no cache to read there in the
    first place."""
    today = now_ist().date().isoformat()
    with _master_cache_lock:
        cached = _equity_master_cache.get(today)
    if cached is not None:
        return cached

    if client is not None:
        rows = dhan_instrument_repo.get_equity_instruments(client)
        if not rows:
            raise ProviderError(
                'Dhan instrument master has never been fetched -- click "Refresh Instrument Master - Dhan" '
                "in Settings' Data Provider section first."
            )
        df = pd.DataFrame(rows)
    else:
        df = _download_equity_master(_get_raw_instrument_master())

    with _master_cache_lock:
        _equity_master_cache.clear()  # only "today" is ever relevant
        _equity_master_cache[today] = df
    return df


class DhanAuthError(ProviderError):
    """Raised on a 401 from the Dhan API -- the access token is invalid or
    (most commonly) has passed its 24-hour expiry. Callers show a distinct
    "regenerate your token" message rather than a generic failure."""


def resolve_security_id(symbol: str, client: Client | None = None) -> str:
    master = _load_instrument_master(client)
    match = master[master["trading_symbol"].astype(str).str.upper() == symbol.upper()]
    if match.empty:
        raise ProviderError(f"no Dhan security_id found for symbol {symbol!r}")
    return str(match.iloc[0]["security_id"])


def _underlying_from_trading_symbol(trading_symbol: str, option_type: str) -> str:
    """Dhan's compact CSV has no usable underlying-symbol column for a
    stock F&O row -- confirmed live: `SM_SYMBOL_NAME` (the obvious
    candidate) is blank for every single NSE FUTSTK/OPTSTK row in a real
    download, not just occasionally missing. The underlying has to be
    parsed out of `SEM_TRADING_SYMBOL` instead, e.g.
    "RELIANCE-Sep2026-700-CE" or "RELIANCE-Aug2026-FUT". Split from the
    RIGHT, not the left: a future always has exactly 2 trailing
    hyphen-separated tokens (expiry, "FUT"), an option always has exactly
    3 (expiry, strike, "CE"/"PE") -- confirmed live -- but the underlying
    ITSELF can also contain a hyphen (real NSE symbols: "NAM-INDIA",
    "BAJAJ-AUTO"), so splitting on the *first* hyphen would wrongly cut
    "NAM-INDIA-Aug2026-720-CE" down to just "NAM"."""
    parts = str(trading_symbol).split("-")
    trailing = 2 if option_type == "FUT" else 3
    return "-".join(parts[: len(parts) - trailing]) if len(parts) > trailing else parts[0]


def _download_fo_master(raw_df: pd.DataFrame) -> pd.DataFrame:
    """Filters the same already-downloaded raw instrument master
    `_download_equity_master` uses (`_get_raw_instrument_master`) down to
    derivative rows instead. Confirmed against a live download of
    https://images.dhan.co/api-data/api-scrip-master.csv
    -- real header: SEM_EXM_EXCH_ID, SEM_SEGMENT, SEM_SMST_SECURITY_ID,
    SEM_INSTRUMENT_NAME, SEM_EXPIRY_CODE, SEM_TRADING_SYMBOL,
    SEM_LOT_UNITS, SEM_CUSTOM_SYMBOL, SEM_EXPIRY_DATE, SEM_STRIKE_PRICE,
    SEM_OPTION_TYPE, SEM_TICK_SIZE, SEM_EXPIRY_FLAG,
    SEM_EXCH_INSTRUMENT_TYPE, SEM_SERIES, SM_SYMBOL_NAME. Still resolved
    by keyword rather than hardcoded, matching `_load_instrument_master`'s
    own defensiveness against Dhan renaming a column -- but
    `underlying_symbol` is deliberately NOT taken from any single column
    (see _underlying_from_trading_symbol's docstring for why). Called by
    _load_fo_instrument_master only on a cache miss (in-memory *and* --
    if a client is given -- the shared dhan_fo_instruments table,
    migration 0035).

    Exchange/instrument-type combination kept mirrors this app's own
    existing bhavcopy split exactly (nse_fo_provider.py /
    bse_fo_provider.py, migration 0031's "stock options are NSE-only"
    guarantee): stock futures/options (FUTSTK/OPTSTK) NSE-only, index
    futures/options (FUTIDX/OPTIDX -- NIFTY/BANKNIFTY/FINNIFTY on NSE,
    SENSEX/BANKEX on BSE) from either exchange. Confirmed live there's no
    cross-exchange collision (no BSE NIFTY/BANKNIFTY row, no NSE
    SENSEX/BANKEX row) -- first-fix history: an earlier version of this
    function excluded FUTIDX/OPTIDX entirely, which silently dropped
    every index CSP/CC leg and index position (NIFTY, BANKNIFTY,
    FINNIFTY, SENSEX) from live pricing -- confirmed live via the
    "Not live-priced" diagnostic (see refresh_bar.py's _fmt_fo_contract)
    naming exactly those symbols."""
    df = raw_df

    def find_col(*keywords: str) -> str | None:
        for col in df.columns:
            upper = col.upper()
            if all(k in upper for k in keywords):
                return col
        return None

    sec_id_col = find_col("SECURITY", "ID")
    exch_col = find_col("EXCH")
    instrument_col = find_col("INSTRUMENT", "NAME")
    trading_symbol_col = find_col("TRADING", "SYMBOL")
    expiry_col = find_col("EXPIRY", "DATE")
    strike_col = find_col("STRIKE")
    option_type_col = find_col("OPTION", "TYPE")

    if not all([sec_id_col, exch_col, instrument_col, trading_symbol_col, expiry_col, strike_col, option_type_col]):
        raise ProviderError("Dhan F&O instrument master schema unrecognized; update column resolution")

    df = df.rename(
        columns={
            sec_id_col: "security_id",
            trading_symbol_col: "trading_symbol",
            expiry_col: "expiry_date",
            strike_col: "strike_price",
            option_type_col: "option_type",
        }
    )
    exch = df[exch_col].astype(str).str.upper()
    instrument = df[instrument_col].astype(str).str.upper()
    is_index = instrument.isin(["FUTIDX", "OPTIDX"])
    is_stock = instrument.isin(["FUTSTK", "OPTSTK"])
    # Kept (not just used to filter and discarded) -- get_fo_quotes needs
    # to know which exchange segment (NSE_FNO/BSE_FNO) a resolved
    # security_id actually lives on. Confirmed live: SENSEX/BANKEX
    # (BSE-only index legs, allowed cross-exchange above) resolve to a
    # real security_id fine, but Dhan's LTP endpoint silently returns
    # nothing for a BSE-listed security_id queried under "NSE_FNO" --
    # indistinguishable from "contract not found" in the Stock & Option
    # Data Refresh summary until this column existed to fix it.
    df["exchange"] = exch
    df = df[(is_index & exch.isin(["NSE", "BSE"])) | (is_stock & (exch == "NSE"))]
    df["expiry_date"] = pd.to_datetime(df["expiry_date"], errors="coerce").dt.date
    df["strike_price"] = pd.to_numeric(df["strike_price"], errors="coerce").fillna(0.0)
    # A future's raw option_type is "XX" (confirmed live) rather than
    # blank/CE/PE -- normalize by allow-list instead of hardcoding that
    # one placeholder: anything that isn't exactly CE/PE becomes 'FUT'.
    option_type = df["option_type"].astype(str).str.upper().str.strip()
    df["option_type"] = option_type.where(option_type.isin(["CE", "PE"]), "FUT")
    df["underlying_symbol"] = [
        _underlying_from_trading_symbol(ts, ot) for ts, ot in zip(df["trading_symbol"], df["option_type"])
    ]
    if df.empty:
        raise ProviderError(
            "Dhan F&O instrument master matched zero stock/index futures or option rows -- exchange/"
            "instrument-type column resolution is likely wrong for this download; update column resolution"
        )
    df["security_id"] = df["security_id"].astype(str)
    return df[["security_id", "underlying_symbol", "expiry_date", "strike_price", "option_type", "exchange"]]


def _load_fo_instrument_master(client: Client | None = None) -> pd.DataFrame:
    """F&O counterpart of _load_instrument_master -- same in-memory ->
    shared dhan_fo_instruments table (migration 0035) chain, same
    deliberate no-download-on-miss behavior when `client` is given (see
    _load_instrument_master's own docstring for why) -- persisted
    independently from the equity slice (provider_name="fo" vs "equity"
    in provider_fetch_log), but both are always refreshed together by
    refresh_dhan_instrument_master."""
    today = now_ist().date().isoformat()
    with _master_cache_lock:
        cached = _fo_master_cache.get(today)
    if cached is not None:
        return cached

    if client is not None:
        rows = dhan_instrument_repo.get_fo_instruments(client)
        if not rows:
            raise ProviderError(
                'Dhan F&O instrument master has never been fetched -- click "Refresh Instrument Master - '
                'Dhan" in Settings\' Data Provider section first.'
            )
        df = pd.DataFrame(rows)
        df["expiry_date"] = pd.to_datetime(df["expiry_date"]).dt.date
        df["strike_price"] = df["strike_price"].astype(float)
    else:
        df = _download_fo_master(_get_raw_instrument_master())

    with _master_cache_lock:
        _fo_master_cache.clear()  # only "today" is ever relevant
        _fo_master_cache[today] = df
    return df


def refresh_dhan_instrument_master(client: Client) -> dict:
    """Settings' "Refresh Instrument Master - Dhan" button
    (src/utils/data_provider_settings.py) -- the *only* thing that
    downloads Dhan's instrument master CSV and persists the equity/F&O
    slices (dhan_equity_instruments/dhan_fo_instruments, migration 0035)
    now that _load_instrument_master/_load_fo_instrument_master no
    longer do so implicitly on a cache miss (see their own docstrings for
    why that was decoupled from Stock & Option Data Refresh). Downloads
    the raw CSV once (shared via _get_raw_instrument_master, same as the
    equity/F&O legs of a live-price refresh already share it), filters
    both slices from it, persists both, and logs both fetches.

    Also updates this process's own in-memory caches immediately, not
    just the DB ones -- otherwise a resolve call in *this same process*
    right after clicking the button would keep serving whatever it
    already had cached for today until the process restarts or the IST
    date rolls over, making the button look like it did nothing.

    Needs no broker connection or access token -- the instrument master
    (images.dhan.co/api-data/api-scrip-master.csv) is public Dhan
    reference data, not account-specific. Returns
    `{"equity_count", "fo_count"}` for the button's own success message."""
    raw = _get_raw_instrument_master()
    today = now_ist().date().isoformat()

    equity_df = _download_equity_master(raw)
    started_at = datetime.now(IST)
    dhan_instrument_repo.replace_equity_instruments(client, equity_df.to_dict("records"))
    fetch_log_repo.log_fetch(
        client,
        ProviderFetchLog(
            provider_name="equity",
            fetch_type=FetchType.DHAN_INSTRUMENT_MASTER,
            status=FetchStatus.SUCCESS,
            started_at=started_at,
            finished_at=datetime.now(IST),
        ),
    )

    fo_df = _download_fo_master(raw)
    started_at = datetime.now(IST)
    fo_db_rows = fo_df.assign(
        expiry_date=fo_df["expiry_date"].astype(str), strike_price=fo_df["strike_price"].astype(float)
    ).to_dict("records")
    dhan_instrument_repo.replace_fo_instruments(client, fo_db_rows)
    fetch_log_repo.log_fetch(
        client,
        ProviderFetchLog(
            provider_name="fo",
            fetch_type=FetchType.DHAN_INSTRUMENT_MASTER,
            status=FetchStatus.SUCCESS,
            started_at=started_at,
            finished_at=datetime.now(IST),
        ),
    )

    with _master_cache_lock:
        _equity_master_cache.clear()
        _equity_master_cache[today] = equity_df
        _fo_master_cache.clear()
        _fo_master_cache[today] = fo_df

    return {"equity_count": len(equity_df), "fo_count": len(fo_df)}


def resolve_fo_security_id(
    symbol: str, expiry_date: date, strike_price: float, option_type: str, client: Client | None = None
) -> str:
    """Resolves one F&O contract (underlying + expiry + strike + right)
    to its Dhan security_id -- the derivatives counterpart of
    resolve_security_id above. `option_type='FUT'` matches a futures
    contract (`strike_price` is ignored, a future has none); `'CE'`/`'PE'`
    matches an option at that exact strike. Same natural key
    (symbol, expiry_date, strike_price, option_type) this app already
    uses for option_contracts (migration 0007) and user_live_prices'
    F&O rows (migration 0032)."""
    master = _load_fo_instrument_master(client)
    match = master[
        (master["underlying_symbol"].astype(str).str.upper() == symbol.upper())
        & (master["expiry_date"] == expiry_date)
        & (master["option_type"] == option_type)
    ]
    if option_type != "FUT":
        match = match[(match["strike_price"] - strike_price).abs() < 0.01]
    if match.empty:
        raise ProviderError(
            f"no Dhan security_id found for {symbol!r} {expiry_date} {option_type} {strike_price!r}"
        )
    return str(match.iloc[0]["security_id"])


class DhanProvider(PriceDataProvider):
    name = "dhan"

    def __init__(
        self,
        client_id: str,
        access_token: str,
        timeout: float = 15.0,
        supabase_client: Client | None = None,
    ):
        if not client_id or not access_token:
            raise ProviderError("DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN are required for the Dhan provider")
        self._client_id = client_id
        self._access_token = access_token
        self._timeout = timeout
        # Optional -- when given, the instrument-master loaders persist
        # their result to the shared dhan_equity_instruments/
        # dhan_fo_instruments cache (migration 0035) instead of only
        # caching in-memory for this process. None keeps today's exact
        # in-memory-only behavior (e.g. the plain cron path via
        # factory.py::get_price_provider, which has no Supabase client to
        # give a generic PriceDataProvider).
        self._supabase_client = supabase_client

    @property
    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "access-token": self._access_token,
            "client-id": self._client_id,
        }

    @retry(
        retry=retry_if_exception_type(ProviderError),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=20),
        reraise=True,
    )
    def _post(self, url: str, payload: dict) -> dict:
        _throttle()
        try:
            resp = httpx.post(url, json=payload, headers=self._headers, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Dhan request to {url} failed: {exc}") from exc
        if resp.status_code >= 500 or resp.status_code == 429:
            raise ProviderError(f"Dhan transient error {resp.status_code}: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise ProviderError(f"Dhan request error {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def get_historical_daily(self, symbol: str, from_date: date, to_date: date) -> list[PricePoint]:
        security_id = resolve_security_id(symbol, self._supabase_client)
        points: list[PricePoint] = []
        # Dhan's historical endpoint caps each request window; chunk in ~85 day
        # slices to stay safely under the documented 90-day limit.
        window_start = from_date
        while window_start <= to_date:
            window_end = min(window_start + timedelta(days=85), to_date)
            payload = {
                "securityId": security_id,
                "exchangeSegment": "NSE_EQ",
                "instrument": "EQUITY",
                "expiryCode": 0,
                "fromDate": window_start.isoformat(),
                "toDate": window_end.isoformat(),
            }
            data = self._post(HISTORICAL_ENDPOINT, payload)
            points.extend(self._parse_historical(symbol, data))
            window_start = window_end + timedelta(days=1)
        points.sort(key=lambda p: p.trade_date)
        return points

    def _parse_historical(self, symbol: str, data: dict) -> list[PricePoint]:
        # Dhan returns parallel arrays: open/high/low/close/volume/timestamp
        timestamps = data.get("timestamp") or data.get("start_Time") or []
        opens = data.get("open", [])
        highs = data.get("high", [])
        lows = data.get("low", [])
        closes = data.get("close", [])
        volumes = data.get("volume", [])
        points = []
        for i, ts in enumerate(timestamps):
            trade_date = datetime.fromtimestamp(ts, tz=IST).date()
            close = closes[i] if i < len(closes) else None
            points.append(
                PricePoint(
                    symbol=symbol,
                    trade_date=trade_date,
                    open=opens[i] if i < len(opens) else None,
                    high=highs[i] if i < len(highs) else None,
                    low=lows[i] if i < len(lows) else None,
                    close=close,
                    adjusted_close=close,  # Dhan does not separately expose adjusted close
                    volume=int(volumes[i]) if i < len(volumes) and volumes[i] is not None else None,
                    source=self.name,
                )
            )
        return points

    def get_quote(self, symbol: str) -> Quote:
        return self.get_quotes([symbol])[symbol]

    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        # Loads (and lets a systemic failure from) the instrument master
        # propagate BEFORE the per-symbol loop below -- confirmed live as
        # its own separate bug: _load_instrument_master's equity filter
        # used to match zero rows (see its docstring), and because
        # @lru_cache never caches a raised exception, every one of the 60+
        # symbols in the loop below re-triggered a fresh failing download
        # attempt and got silently caught by the per-symbol `except
        # ProviderError: continue` -- so a total, systemic resolution
        # failure looked identical to "just didn't resolve", and got
        # swallowed into an empty result with no error at all, not even
        # the DhanAuthError/ProviderError _refresh_user_live_prices knows
        # how to surface.
        _load_instrument_master(self._supabase_client)
        # A symbol resolve_security_id can't find (an ETF/fund Dhan's
        # equity instrument master doesn't carry, a renamed/delisted
        # ticker) is skipped rather than aborting the whole batch --
        # confirmed live: widening the watched-symbol universe to every
        # tracked ETF (migration 0032) meant one unresolvable ETF made
        # this raise ProviderError, which load_live_dhan_prices' caller
        # catches and turns into an EMPTY result for every symbol in the
        # batch, not just the bad one -- a single bad ETF was silently
        # wiping out live pricing for the entire Nifty50 universe too.
        id_to_symbol: dict[str, str] = {}
        for symbol in symbols:
            try:
                id_to_symbol[resolve_security_id(symbol, self._supabase_client)] = symbol
            except ProviderError:
                continue
        if not id_to_symbol:
            return {}
        payload = {"NSE_EQ": [int(sid) for sid in id_to_symbol]}
        data = self._post(LTP_ENDPOINT, payload)
        now = datetime.now(IST)
        result: dict[str, Quote] = {}
        nse_eq = data.get("data", {}).get("NSE_EQ", {})
        for sec_id, symbol in id_to_symbol.items():
            entry = nse_eq.get(str(sec_id)) or nse_eq.get(sec_id)
            if entry is None:
                continue
            result[symbol] = Quote(symbol=symbol, latest_price=entry["last_price"], as_of=now, source=self.name)
        return result

    # ------------------------------------------------------------------
    # Per-user portfolio sync (pages/6_My_Broker.py "Connect Dhan account")
    # -- reuses this same class's auth/header handling for a per-user
    # client_id/access_token pair (rather than this app's own single
    # settings.dhan_client_id/dhan_access_token), since the request
    # mechanics are identical. Deliberately not decorated with the
    # `@retry` used by _post above: these are manual, user-initiated
    # "Sync now" clicks, not part of the automated price pipeline, so a
    # failure -- especially an expired token -- should surface immediately
    # rather than retrying for up to ~20s first.
    # ------------------------------------------------------------------
    def _request(self, method: str, url: str, *, json: dict | None = None):
        _throttle()
        try:
            resp = httpx.request(method, url, json=json, headers=self._headers, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Dhan request to {url} failed: {exc}") from exc
        if resp.status_code == 401:
            raise DhanAuthError(f"Dhan access token rejected (401): {resp.text[:200]}")
        if resp.status_code >= 400:
            raise ProviderError(f"Dhan request error {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def get_holdings(self) -> list[dict]:
        """Raw rows from GET /v2/holdings -- one per equity/ETF holding.
        Response shape per Dhan's v2 docs as researched; verify against a
        live account before relying on this, same caveat as the rest of
        this module."""
        return self._request("GET", f"{BASE_URL}/holdings") or []

    def get_positions(self) -> list[dict]:
        """Raw rows from GET /v2/positions -- one per open F&O position."""
        return self._request("GET", f"{BASE_URL}/positions") or []

    def renew_access_token(self) -> str:
        """GET /v2/RenewToken -- extends a still-active Dhan Web access
        token by another 24 hours, without the user visiting web.dhan.co
        again (the pain point on mobile, away from a laptop). Per Dhan's
        docs this ONLY works on a token that hasn't expired yet -- calling
        it on an already-expired token 401s exactly like any other
        rejected token (mapped to DhanAuthError below), and the caller
        should fall back to asking for a freshly-pasted one. Also
        documented as only renewing tokens generated via Dhan Web (i.e.
        the plaintext-pasted kind this app stores) -- not one issued
        through Dhan's separate API-key/secret flow, which this app
        doesn't use anyway.

        **Confirmed live: this must be a GET, not a POST** -- an earlier
        version of this method used `httpx.post`, which Dhan rejected
        with a generic `DH-905 "Missing required fields, bad values for
        parameters etc."` 400 (not a 401/405, so nothing about the error
        itself pointed at the HTTP method) every single time, even with
        headers otherwise identical to what's below. Cross-checked
        against Dhan's own official `dhanhq` Python client
        (`DhanLogin.renew_token`, `src/dhanhq/auth.py`), which calls
        `requests.get(url, headers=headers)` -- no body, no query params,
        same two headers -- and that matches Dhan's docs' own "no request
        body" note for this endpoint, which (wrongly) reads as
        method-agnostic until you see the reference implementation.

        Uses its own header dict rather than self._headers: this endpoint
        is documented to want `dhanClientId` (camelCase), not the
        `client-id` header every other v2 endpoint in this class uses.

        **Response shape confirmed live** (not what the docs/third-party
        write-ups implied): `{"createTime", "expiryTime", "token"}` --
        the new token is under `token`, NOT `accessToken` like
        DhanHQ-py's own docstrings/other Dhan endpoints suggested. A
        first version of this method trusted that unverified `accessToken`
        guess and always raised "no accessToken" despite Dhan returning a
        real 200 with a real token under a different key -- fixed by
        checking `token` first, falling back to `accessToken` in case a
        future/different account ever returns the other shape."""
        _throttle()
        headers = {
            "Accept": "application/json",
            "access-token": self._access_token,
            "dhanClientId": self._client_id,
        }
        try:
            resp = httpx.get(f"{BASE_URL}/RenewToken", headers=headers, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Dhan token renewal request failed: {exc}") from exc
        if resp.status_code == 401:
            raise DhanAuthError(f"Dhan access token rejected (401): {resp.text[:200]}")
        if resp.status_code >= 400:
            raise ProviderError(f"Dhan token renewal error {resp.status_code}: {resp.text[:200]}")
        body = resp.json() or {}
        new_token = body.get("token") or body.get("accessToken")
        if not new_token:
            raise ProviderError(f"Dhan token renewal response had no token: {resp.text[:200]}")
        return new_token

    def get_trade_history(self, from_date: date, to_date: date, max_pages: int = 500) -> list[dict]:
        """Raw rows from GET /v2/trades/{from-date}/{to-date}/{page} -- one
        per executed fill, paginated (page starts at 0, Dhan returns an
        empty list once exhausted). Response shape per Dhan's v2 docs as
        researched; verify against a live account before relying on this,
        same caveat as the rest of this module.

        `max_pages` is a safety cap, not a documented Dhan limit -- if
        pagination ever misbehaves (e.g. the `page` segment turns out to be
        ignored server-side and every page echoes the same non-empty
        batch), this raises instead of looping forever. 500 pages is far
        beyond what even a very active account should hit for a normal
        sync window."""
        rows: list[dict] = []
        page = 0
        while page < max_pages:
            url = f"{BASE_URL}/trades/{from_date.isoformat()}/{to_date.isoformat()}/{page}"
            batch = self._request("GET", url) or []
            if not batch:
                return rows
            rows.extend(batch)
            page += 1
        raise ProviderError(
            f"Dhan trade history did not terminate after {max_pages} pages ({len(rows)} rows so far) -- "
            "pagination may not be behaving as documented; narrow the date range and retry."
        )

    def get_ltp_by_security_id(self, security_ids_by_segment: dict[str, list[str]]) -> dict[str, float]:
        """POST /v2/marketfeed/ltp for arbitrary exchange segments (e.g.
        NSE_FNO, IDX_I for index options) -- unlike get_quotes() above,
        which is hardcoded to NSE_EQ for this app's own equity price
        pipeline. Returns {security_id: last_price} flattened across
        segments (security_id is unique across all of them, so the segment
        isn't needed in the result key)."""
        payload = {segment: [int(sid) for sid in ids] for segment, ids in security_ids_by_segment.items() if ids}
        if not payload:
            return {}
        data = self._request("POST", LTP_ENDPOINT, json=payload)
        result: dict[str, float] = {}
        for segment_data in (data.get("data") or {}).values():
            for sec_id, entry in segment_data.items():
                result[str(sec_id)] = entry["last_price"]
        return result

    def get_fo_quotes(self, contracts: list[tuple[str, date, float, str]]) -> dict[tuple[str, date, float, str], float]:
        """Live LTP for a batch of futures/option contracts -- each a
        (symbol, expiry_date, strike_price, option_type) tuple, same
        natural key user_live_prices' F&O rows use (migration 0032).
        `option_type='FUT'` for a futures contract, 'CE'/'PE' for an
        option (`strike_price` is ignored for a future, still present in
        the key for a uniform shape). Resolves each contract to a Dhan
        security_id via resolve_fo_security_id and batches them all into
        one get_ltp_by_security_id call, split into NSE_FNO/BSE_FNO
        segments by each resolved id's own exchange (see
        _download_fo_master's "exchange" column) -- same multi-instrument
        batching get_quotes already relies on for equities, but a real
        Dhan segment can't be hardcoded here the way NSE_EQ is there:
        confirmed live, querying a BSE-listed security_id (SENSEX/BANKEX,
        the only F&O legs that resolve cross-exchange) under a hardcoded
        "NSE_FNO" silently returned nothing for it, indistinguishable
        from "Dhan has no matching contract" in the Stock & Option Data
        Refresh summary. A contract Dhan's F&O master doesn't resolve (an
        expired/delisted strike, a schema-drift miss) is silently skipped
        rather than aborting the whole batch -- this feeds a best-effort
        live-price overlay (Dashboard CSP/CC, portfolio positions), not
        an all-or-nothing fetch."""
        if not contracts:
            return {}
        # Loads (and lets a systemic failure propagate from) the F&O
        # instrument master BEFORE the per-contract loop below -- same
        # fix as get_quotes' own equivalent call, and for the same
        # reason: @lru_cache never caches a raised exception, so a
        # systemic failure would otherwise re-trigger on every single
        # contract in the loop and get silently caught by the
        # per-contract `except ProviderError: continue`, indistinguishable
        # from "just didn't resolve".
        master = _load_fo_instrument_master(self._supabase_client)
        exchange_by_security_id = dict(zip(master["security_id"], master["exchange"]))
        security_id_to_contract: dict[str, tuple[str, date, float, str]] = {}
        for contract in contracts:
            symbol, expiry_date, strike_price, option_type = contract
            try:
                security_id = resolve_fo_security_id(
                    symbol, expiry_date, strike_price, option_type, self._supabase_client
                )
            except ProviderError:
                continue
            security_id_to_contract[security_id] = contract
        ids_by_segment: dict[str, list[str]] = {"NSE_FNO": [], "BSE_FNO": []}
        for security_id in security_id_to_contract:
            segment = "BSE_FNO" if exchange_by_security_id.get(security_id) == "BSE" else "NSE_FNO"
            ids_by_segment[segment].append(security_id)
        prices_by_security_id = self.get_ltp_by_security_id(ids_by_segment)
        return {
            contract: prices_by_security_id[security_id]
            for security_id, contract in security_id_to_contract.items()
            if security_id in prices_by_security_id
        }

    def get_margin_for_legs(self, legs: list[dict]) -> dict | None:
        """Combined margin required (`POST /v2/margincalculator/multi`)
        for a set of option legs -- e.g. one Trade's own Position legs on
        My Portfolio Trades, so the page can show what Dhan would
        actually block for holding that trade, hedge benefit included
        where Dhan applies one.

        Each leg dict needs `symbol`/`expiry_date`/`strike_price`/
        `option_type`/`qty` (signed: negative = short/SELL, positive =
        long/BUY) plus `avg_price` -- the same shape `build_trade_legs`/
        `compute_positions_view` already produce, so a caller can pass a
        trade's own Position legs straight through with no reshaping.
        `avg_price` (the price this account actually holds the leg at) is
        what's sent as each scrip's `price` -- Dhan's calculator only
        needs a price to size the SPAN/exposure scenario, not to value
        the position against a live quote. `productType` is hardcoded to
        `"MARGIN"` -- this app doesn't track a position's actual product
        type, and every real F&O position checked live on this account
        was `MARGIN` anyway.

        Resolves each leg's security_id + exchange segment (NSE_FNO/
        BSE_FNO) via the same FO instrument master `get_fo_quotes` above
        already uses -- a leg that doesn't resolve (an expired/delisted
        strike, a schema-drift miss) is silently skipped rather than
        aborting the whole calculation, same convention `get_fo_quotes`
        uses for a live-price batch. Returns `None` if `legs` is empty or
        none of them resolve.

        **Response shape confirmed live, not what Dhan's own docs
        describe** (different key casing entirely):
        `{"clientId", "totalMargin", "spanMargin", "exposure",
        "equityMargin", "foMargin", "commodity", "currency",
        "hedgeBenefit", "userFundLimit", "insufficientFund"}`. Also
        confirmed live: this endpoint 400s with `"dhanClientId is
        required"` unless `dhanClientId` is repeated in the **request
        body** itself, not just the `client-id`/`access-token` headers
        every other v2 endpoint relies on alone -- `self._headers`
        already covers the headers, `dhanClientId` is added to the body
        below. `hedgeBenefit` was `0.0` for a real two-leg naked Strangle
        (two same-underlying short legs, confirmed live) -- don't assume
        Dhan applies a nonzero benefit for every multi-leg trade; a
        genuinely offsetting structure may behave differently, untested
        here."""
        if not legs:
            return None
        master = _load_fo_instrument_master(self._supabase_client)
        exchange_by_security_id = dict(zip(master["security_id"], master["exchange"]))
        scrip_list = []
        for leg in legs:
            try:
                security_id = resolve_fo_security_id(
                    leg["symbol"], leg["expiry_date"], leg["strike_price"], leg["option_type"], self._supabase_client
                )
            except ProviderError:
                continue
            segment = "BSE_FNO" if exchange_by_security_id.get(security_id) == "BSE" else "NSE_FNO"
            qty = leg["qty"]
            scrip_list.append(
                {
                    "exchangeSegment": segment,
                    "transactionType": "SELL" if qty < 0 else "BUY",
                    "quantity": abs(int(qty)),
                    "productType": "MARGIN",
                    "securityId": security_id,
                    "price": leg["avg_price"],
                }
            )
        if not scrip_list:
            return None
        payload = {
            "dhanClientId": self._client_id,
            "includePosition": False,
            "includeOrder": False,
            "scripList": scrip_list,
        }
        return self._request("POST", f"{BASE_URL}/margincalculator/multi", json=payload)
