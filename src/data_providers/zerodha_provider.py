"""Zerodha Kite Connect API -- per-user portfolio sync for
pages/6_My_Broker.py's "Connect Zerodha account" flow. Unlike
src/data_providers/dhan_provider.py, this is portfolio-sync only, not a
PriceDataProvider -- Zerodha isn't (and isn't planned to be) this app's
own equity/F&O price-pipeline source (see src/config.py's
`market_data_provider: Literal["dhan", "yfinance", "mock"]`), so there's
no ABC to implement here, just the three things the connect flow needs.

Kite Connect works fundamentally differently from Dhan's API:

- **Paid, app-based access.** There is no free/self-serve token the way
  Dhan's web.dhan.co page offers. A Kite Connect "app" (api_key +
  api_secret) must be registered at developers.kite.trade and its
  ₹2000+GST/month subscription paid for, entirely outside this codebase.
- **Login is a redirect flow, not a copy-paste token.** `login_url()`
  sends the user's browser to Zerodha's own login page (this app never
  sees the password/TOTP); Zerodha then redirects back to whatever
  Redirect URL is configured on the Kite Connect app itself, appending
  `?request_token=...&action=login&status=success`. `generate_session()`
  exchanges that `request_token` for an `access_token` -- safe to do
  server-side here since (unlike a browser SPA) Streamlit page code runs
  in the server process, so `api_secret` never reaches the browser.
- **The access_token expires daily at a fixed time (~6am IST the next
  day)**, not on a rolling window like Dhan's 24-hour token -- there is
  no way to avoid a fresh login every trading day with Kite Connect.

Endpoint shapes below follow the Kite Connect v3 HTTP API as documented;
same caveat as dhan_provider.py's own file header -- verify against a
real Kite Connect app/account before relying on this in production.
"""
from __future__ import annotations

import hashlib
import threading
import time

import httpx

from src.data_providers.base import ProviderError

BASE_URL = "https://api.kite.trade"
LOGIN_URL = "https://kite.zerodha.com/connect/login"
KITE_VERSION = "3"

# Kite Connect documents per-second rate limits; stay comfortably under
# them with the same simple client-side throttle dhan_provider.py uses.
_MIN_REQUEST_INTERVAL_SECONDS = 0.34
_last_request_lock = threading.Lock()
_last_request_at = 0.0


def _throttle() -> None:
    global _last_request_at
    with _last_request_lock:
        wait = _MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


class ZerodhaAuthError(ProviderError):
    """Raised when Kite Connect rejects the access token (expired session
    -- most commonly because it's past the fixed daily invalidation time,
    ~6am IST) -- callers show a "log in again" prompt rather than a
    generic error."""


class ZerodhaProvider:
    def __init__(self, api_key: str, api_secret: str, access_token: str | None = None, timeout: float = 15.0):
        if not api_key or not api_secret:
            raise ProviderError("api_key and api_secret are required for the Zerodha provider")
        self._api_key = api_key
        self._api_secret = api_secret
        self._access_token = access_token
        self._timeout = timeout

    def login_url(self) -> str:
        """The URL to send the user's browser to -- Zerodha's own login
        page, which redirects back to this Kite Connect app's configured
        Redirect URL with `?request_token=...` on success."""
        return f"{LOGIN_URL}?v={KITE_VERSION}&api_key={self._api_key}"

    def generate_session(self, request_token: str) -> str:
        """Exchanges a `request_token` (from the login redirect's query
        string) for an access_token. The checksum
        (sha256(api_key + request_token + api_secret)) is what proves
        this request came from the app holding `api_secret`, not just
        anyone who observed the request_token in a URL."""
        checksum = hashlib.sha256(f"{self._api_key}{request_token}{self._api_secret}".encode()).hexdigest()
        _throttle()
        try:
            resp = httpx.post(
                f"{BASE_URL}/session/token",
                data={"api_key": self._api_key, "request_token": request_token, "checksum": checksum},
                headers={"X-Kite-Version": KITE_VERSION},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"Zerodha session request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(f"Zerodha session exchange failed ({resp.status_code}): {resp.text[:200]}")
        data = resp.json()
        access_token = (data.get("data") or {}).get("access_token")
        if not access_token:
            raise ProviderError(f"Zerodha session exchange returned no access_token: {resp.text[:200]}")
        self._access_token = access_token
        return access_token

    @property
    def _headers(self) -> dict:
        if not self._access_token:
            raise ProviderError("Zerodha provider has no access_token -- call generate_session() first")
        return {
            "Authorization": f"token {self._api_key}:{self._access_token}",
            "X-Kite-Version": KITE_VERSION,
        }

    def _get(self, path: str) -> dict:
        _throttle()
        try:
            resp = httpx.get(f"{BASE_URL}{path}", headers=self._headers, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Zerodha request to {path} failed: {exc}") from exc
        if resp.status_code in (401, 403):
            raise ZerodhaAuthError(f"Zerodha access token rejected ({resp.status_code}): {resp.text[:200]}")
        if resp.status_code >= 400:
            raise ProviderError(f"Zerodha request error {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def get_holdings(self) -> list[dict]:
        """Raw rows from GET /portfolio/holdings -- one per equity/ETF
        holding. Includes `last_price` directly, unlike Dhan's holdings
        endpoint (no separate quote call needed)."""
        return (self._get("/portfolio/holdings") or {}).get("data") or []

    def get_positions(self) -> list[dict]:
        """Raw rows from GET /portfolio/positions -- returns only the
        `net` list (currently open positions); `day` (intraday-only
        positions, closed by end of day regardless of `product`) isn't
        relevant to a portfolio-tracking sync. Includes `last_price`
        directly, same as holdings above."""
        data = (self._get("/portfolio/positions") or {}).get("data") or {}
        return data.get("net") or []

    def get_ltp(self, symbols: list[str]) -> dict[str, float]:
        """Live NSE equity LTPs from GET /quote/ltp -- unlike
        get_holdings/get_positions above, this isn't scoped to what the
        user actually holds; it's a live quote for any NSE tradingsymbol,
        the same "underlying's own current price" role
        DhanProvider.get_quotes plays for My CSP's LTP Underlying column.
        `i=NSE:<symbol>` is repeated once per symbol (Kite's documented
        way to batch a quote request); response is keyed
        `"NSE:<symbol>"`, unwrapped back to a plain `{symbol: last_price}`
        dict here so callers don't need to know Kite's key format."""
        if not symbols:
            return {}
        instruments = [f"NSE:{s}" for s in symbols]
        _throttle()
        try:
            resp = httpx.get(
                f"{BASE_URL}/quote/ltp", params=[("i", i) for i in instruments], headers=self._headers, timeout=self._timeout
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"Zerodha request to /quote/ltp failed: {exc}") from exc
        if resp.status_code in (401, 403):
            raise ZerodhaAuthError(f"Zerodha access token rejected ({resp.status_code}): {resp.text[:200]}")
        if resp.status_code >= 400:
            raise ProviderError(f"Zerodha request error {resp.status_code}: {resp.text[:200]}")
        data = (resp.json() or {}).get("data") or {}
        return {
            symbol: entry["last_price"]
            for symbol, instrument in zip(symbols, instruments)
            if (entry := data.get(instrument)) is not None
        }
