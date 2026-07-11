"""Yahoo Finance-backed provider via the `yfinance` package -- covers
BOTH prices and fundamentals from a single source, unlike Dhan (prices
only) or the screener.in importer (fundamentals only, needs a manual CSV
export). NSE-listed symbols are addressed with a `.NS` suffix.

`yfinance` wraps Yahoo Finance's JSON chart/quote API rather than scraping
HTML pages, and needs no API key. It is still an *unofficial* client,
though: Yahoo's terms restrict automated commercial use, and Yahoo has
rate-limited/blocked yfinance traffic before. It's a reasonable, widely
used choice for a personal/analytical dashboard -- see README
"Limitations" -- but isn't a substitute for a licensed vendor if this
becomes a commercial product.

Real per-event dividend history is available here (`Ticker.dividends`),
so unlike the screener.in importer this does NOT need to fabricate a
synthetic dividend event -- TTM yield is computed the same way as every
other provider, via src.calculations.dividends.ttm_dividend_yield() over
real ex-dividend dates.
"""
from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta

import pandas as pd
import pytz
import yfinance as yf
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.data_providers.base import FundamentalsDataProvider, PriceDataProvider, ProviderError
from src.models.enums import DividendType
from src.models.market_data import DividendEvent, FundamentalSnapshot, PricePoint, Quote

IST = pytz.timezone("Asia/Kolkata")

_MIN_REQUEST_INTERVAL_SECONDS = 0.4
_last_request_lock = threading.Lock()
_last_request_at = 0.0


def _throttle() -> None:
    global _last_request_at
    with _last_request_lock:
        wait = _MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


def _yf_symbol(symbol: str) -> str:
    return f"{symbol}.NS"


_retry_yf_call = retry(
    retry=retry_if_exception_type(ProviderError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=15),
    reraise=True,
)


class YFinancePriceProvider(PriceDataProvider):
    name = "yfinance"

    def get_historical_daily(self, symbol: str, from_date: date, to_date: date) -> list[PricePoint]:
        @_retry_yf_call
        def _fetch() -> pd.DataFrame:
            _throttle()
            try:
                ticker = yf.Ticker(_yf_symbol(symbol))
                return ticker.history(
                    start=from_date.isoformat(),
                    end=(to_date + timedelta(days=1)).isoformat(),
                    auto_adjust=False,
                )
            except Exception as exc:  # noqa: BLE001
                raise ProviderError(f"yfinance historical fetch failed for {symbol}: {exc}") from exc

        hist = _fetch()
        if hist is None or hist.empty:
            return []

        points = []
        for idx, row in hist.iterrows():
            trade_date = idx.date() if hasattr(idx, "date") else idx
            close = float(row["Close"]) if pd.notna(row.get("Close")) else None
            adj_close = float(row["Adj Close"]) if pd.notna(row.get("Adj Close")) else close
            points.append(
                PricePoint(
                    symbol=symbol,
                    trade_date=trade_date,
                    open=float(row["Open"]) if pd.notna(row.get("Open")) else None,
                    high=float(row["High"]) if pd.notna(row.get("High")) else None,
                    low=float(row["Low"]) if pd.notna(row.get("Low")) else None,
                    close=close,
                    adjusted_close=adj_close,
                    volume=int(row["Volume"]) if pd.notna(row.get("Volume")) else None,
                    source=self.name,
                )
            )
        return points

    def get_quote(self, symbol: str) -> Quote:
        @_retry_yf_call
        def _fetch() -> float:
            _throttle()
            try:
                fast_info = yf.Ticker(_yf_symbol(symbol)).fast_info
                price = fast_info.get("lastPrice") if hasattr(fast_info, "get") else fast_info["lastPrice"]
            except Exception as exc:  # noqa: BLE001
                raise ProviderError(f"yfinance quote fetch failed for {symbol}: {exc}") from exc
            if price is None:
                raise ProviderError(f"yfinance returned no lastPrice for {symbol}")
            return float(price)

        return Quote(symbol=symbol, latest_price=_fetch(), as_of=datetime.now(IST), source=self.name)

    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        # yfinance has no simpler true-batch quote call for fast_info than
        # per-symbol requests; _throttle() paces them to be a good citizen.
        result: dict[str, Quote] = {}
        for symbol in symbols:
            try:
                result[symbol] = self.get_quote(symbol)
            except ProviderError:
                continue
        return result


class YFinanceFundamentalsProvider(FundamentalsDataProvider):
    name = "yfinance"

    def get_fundamentals(self, symbol: str, as_of: date) -> FundamentalSnapshot | None:
        @_retry_yf_call
        def _fetch() -> dict:
            _throttle()
            try:
                return yf.Ticker(_yf_symbol(symbol)).get_info()
            except Exception as exc:  # noqa: BLE001
                raise ProviderError(f"yfinance fundamentals fetch failed for {symbol}: {exc}") from exc

        info = _fetch()
        if not info:
            return None

        return FundamentalSnapshot(
            symbol=symbol,
            as_of_date=as_of,
            pe_ratio=info.get("trailingPE"),
            peg_ratio=info.get("pegRatio") or info.get("trailingPegRatio"),
            eps=info.get("trailingEps"),
            market_cap=info.get("marketCap"),
            source=self.name,
            is_stale=False,
        )

    def get_dividend_history(self, symbol: str, from_date: date, to_date: date) -> list[DividendEvent]:
        @_retry_yf_call
        def _fetch() -> pd.Series:
            _throttle()
            try:
                return yf.Ticker(_yf_symbol(symbol)).dividends
            except Exception as exc:  # noqa: BLE001
                raise ProviderError(f"yfinance dividend fetch failed for {symbol}: {exc}") from exc

        series = _fetch()
        if series is None or series.empty:
            return []

        events = []
        for idx, amount in series.items():
            ex_date = idx.date() if hasattr(idx, "date") else idx
            if from_date <= ex_date <= to_date and amount:
                events.append(
                    DividendEvent(
                        symbol=symbol, ex_date=ex_date, amount_per_share=float(amount),
                        dividend_type=DividendType.FINAL, source=self.name,
                    )
                )
        return events
