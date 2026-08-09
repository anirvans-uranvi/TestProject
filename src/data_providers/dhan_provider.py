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
from functools import lru_cache

import httpx
import pandas as pd
import pytz
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.data_providers.base import PriceDataProvider, ProviderError
from src.models.market_data import PricePoint, Quote

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


@lru_cache(maxsize=1)
def _load_instrument_master() -> pd.DataFrame:
    """Download and cache the NSE-equity slice of Dhan's instrument master.

    Column names in Dhan's compact CSV have varied across releases, so we
    resolve them by fuzzy match instead of hardcoding exact headers.
    """
    try:
        df = pd.read_csv(INSTRUMENT_MASTER_URL, low_memory=False)
    except Exception as exc:  # noqa: BLE001
        raise ProviderError(f"failed to download Dhan instrument master: {exc}") from exc

    def find_col(*keywords: str) -> str | None:
        for col in df.columns:
            upper = col.upper()
            if all(k in upper for k in keywords):
                return col
        return None

    sec_id_col = find_col("SECURITY", "ID")
    symbol_col = find_col("TRADING", "SYMBOL") or find_col("SYMBOL")
    exch_col = find_col("EXCH")
    segment_col = find_col("SEGMENT") or find_col("INSTRUMENT")

    if not all([sec_id_col, symbol_col]):
        raise ProviderError("Dhan instrument master schema unrecognized; update column resolution")

    df = df.rename(columns={sec_id_col: "security_id", symbol_col: "trading_symbol"})
    if exch_col:
        df = df[df[exch_col].astype(str).str.upper().str.contains("NSE", na=False)]
    if segment_col:
        df = df[df[segment_col].astype(str).str.upper().str.contains("EQ", na=False)]
    return df[["security_id", "trading_symbol"]].drop_duplicates("trading_symbol")


class DhanAuthError(ProviderError):
    """Raised on a 401 from the Dhan API -- the access token is invalid or
    (most commonly) has passed its 24-hour expiry. Callers show a distinct
    "regenerate your token" message rather than a generic failure."""


def resolve_security_id(symbol: str) -> str:
    master = _load_instrument_master()
    match = master[master["trading_symbol"].astype(str).str.upper() == symbol.upper()]
    if match.empty:
        raise ProviderError(f"no Dhan security_id found for symbol {symbol!r}")
    return str(match.iloc[0]["security_id"])


class DhanProvider(PriceDataProvider):
    name = "dhan"

    def __init__(self, client_id: str, access_token: str, timeout: float = 15.0):
        if not client_id or not access_token:
            raise ProviderError("DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN are required for the Dhan provider")
        self._client_id = client_id
        self._access_token = access_token
        self._timeout = timeout

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
        security_id = resolve_security_id(symbol)
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
        id_to_symbol = {resolve_security_id(s): s for s in symbols}
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
    # Per-user portfolio sync (pages/6_Portfolio.py "Connect Dhan account")
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
