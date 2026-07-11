"""Provider-agnostic interfaces. Every concrete provider (Dhan, mock,
manual-CSV) implements one of these so the rest of the app never depends
on a specific vendor's request/response shape."""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from src.models.market_data import DividendEvent, FundamentalSnapshot, PricePoint, Quote


class ProviderError(Exception):
    """Raised on any provider fetch failure; caller logs to provider_fetch_log."""


class PriceDataProvider(ABC):
    name: str

    @abstractmethod
    def get_quote(self, symbol: str) -> Quote:
        """Latest (possibly delayed) traded price for a single symbol."""

    @abstractmethod
    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """Latest price for many symbols in one batch where the vendor supports it."""

    @abstractmethod
    def get_historical_daily(self, symbol: str, from_date: date, to_date: date) -> list[PricePoint]:
        """Daily OHLCV, oldest -> newest, adjusted_close populated where available."""


class FundamentalsDataProvider(ABC):
    name: str

    @abstractmethod
    def get_fundamentals(self, symbol: str, as_of: date) -> FundamentalSnapshot | None:
        """PE / PEG / EPS / market cap as of (or nearest available to) `as_of`."""

    @abstractmethod
    def get_dividend_history(self, symbol: str, from_date: date, to_date: date) -> list[DividendEvent]:
        """Ex-dividend events with per-share cash amounts in the given window."""
