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
from src.utils.timezones import now_ist, to_ist

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
    faster, despite running "in parallel"."""
    today = now_ist().date().isoformat()
    with _master_cache_lock:
        cached = _raw_master_cache.get(today)
        if cached is not None:
            return cached
        try:
            df = pd.read_csv(INSTRUMENT_MASTER_URL, low_memory=False)
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"failed to download Dhan instrument master: {exc}") from exc
        _raw_master_cache.clear()  # only "today" is ever relevant
        _raw_master_cache[today] = df
        return df


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
    given -- the shared `dhan_equity_instruments` table (migration 0035,
    fresh if refreshed today per `provider_fetch_log`), only falling back
    to a real Dhan download (`_download_equity_master`) when neither has
    today's data. A fresh download is persisted back to `client` (so the
    *next* process/user skips the download too) before being cached
    in-memory. `client=None` (e.g. no Supabase wiring available) keeps
    this in-memory-only, same as before this cache existed."""
    today = now_ist().date().isoformat()
    with _master_cache_lock:
        cached = _equity_master_cache.get(today)
    if cached is not None:
        return cached

    df: pd.DataFrame | None = None
    if client is not None:
        entry = fetch_log_repo.get_last_successful_fetch(client, FetchType.DHAN_INSTRUMENT_MASTER, "equity")
        if entry is not None and to_ist(entry.finished_at).date().isoformat() == today:
            rows = dhan_instrument_repo.get_equity_instruments(client)
            if rows:
                df = pd.DataFrame(rows)

    if df is None:
        df = _download_equity_master(_get_raw_instrument_master())
        if client is not None:
            started_at = datetime.now(IST)
            dhan_instrument_repo.replace_equity_instruments(client, df.to_dict("records"))
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
    return df[["security_id", "underlying_symbol", "expiry_date", "strike_price", "option_type"]]


def _load_fo_instrument_master(client: Client | None = None) -> pd.DataFrame:
    """F&O counterpart of _load_instrument_master -- same in-memory ->
    shared dhan_fo_instruments table (migration 0035) -> real download
    fallback chain, persisted independently from the equity slice
    (provider_name="fo" vs "equity" in provider_fetch_log)."""
    today = now_ist().date().isoformat()
    with _master_cache_lock:
        cached = _fo_master_cache.get(today)
    if cached is not None:
        return cached

    df: pd.DataFrame | None = None
    if client is not None:
        entry = fetch_log_repo.get_last_successful_fetch(client, FetchType.DHAN_INSTRUMENT_MASTER, "fo")
        if entry is not None and to_ist(entry.finished_at).date().isoformat() == today:
            rows = dhan_instrument_repo.get_fo_instruments(client)
            if rows:
                df = pd.DataFrame(rows)
                df["expiry_date"] = pd.to_datetime(df["expiry_date"]).dt.date
                df["strike_price"] = df["strike_price"].astype(float)

    if df is None:
        df = _download_fo_master(_get_raw_instrument_master())
        if client is not None:
            started_at = datetime.now(IST)
            db_rows = df.assign(expiry_date=df["expiry_date"].astype(str), strike_price=df["strike_price"].astype(float)).to_dict(
                "records"
            )
            dhan_instrument_repo.replace_fo_instruments(client, db_rows)
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
        _fo_master_cache.clear()  # only "today" is ever relevant
        _fo_master_cache[today] = df
    return df


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
        one get_ltp_by_security_id call (single NSE_FNO POST, same
        multi-instrument batching get_quotes already relies on for
        equities). A contract Dhan's F&O master doesn't resolve (an
        expired/delisted strike, an index leg, a schema-drift miss) is
        silently skipped rather than aborting the whole batch -- this
        feeds a best-effort live-price overlay (Dashboard CSP/CC,
        portfolio positions), not an all-or-nothing fetch."""
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
        _load_fo_instrument_master(self._supabase_client)
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
        prices_by_security_id = self.get_ltp_by_security_id({"NSE_FNO": list(security_id_to_contract)})
        return {
            contract: prices_by_security_id[security_id]
            for security_id, contract in security_id_to_contract.items()
            if security_id in prices_by_security_id
        }
