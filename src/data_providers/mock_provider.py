"""Offline providers so the app runs end-to-end with zero paid credentials.

Price/fundamentals series are deterministic per symbol (seeded RNG) so repeat
runs and tests see stable numbers, while still varying enough across symbols
to exercise the full Green/Amber/Red/Unavailable spread on the Dashboard.
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta

import pytz

from src.data_providers.base import FundamentalsDataProvider, PriceDataProvider
from src.models.enums import DividendType
from src.models.market_data import DividendEvent, FundamentalSnapshot, PricePoint, Quote

IST = pytz.timezone("Asia/Kolkata")


def _trading_days(from_date: date, to_date: date) -> list[date]:
    days = []
    d = from_date
    while d <= to_date:
        if d.weekday() < 5:  # Mon-Fri; mock provider doesn't model NSE holidays
            days.append(d)
        d += timedelta(days=1)
    return days


def _seed_for(symbol: str) -> int:
    return sum(ord(c) for c in symbol) * 2654435761 % (2**32)


def _base_price_for(symbol: str) -> float:
    rng = random.Random(_seed_for(symbol))
    return round(rng.uniform(200, 4000), 2)


class MockPriceProvider(PriceDataProvider):
    name = "mock"

    def _generate_series(self, symbol: str, from_date: date, to_date: date) -> list[PricePoint]:
        rng = random.Random(_seed_for(symbol))
        price = _base_price_for(symbol)
        # small per-symbol drift bias so some names trend up, some down/flat
        drift = rng.uniform(-0.0006, 0.0010)
        points: list[PricePoint] = []
        for d in _trading_days(from_date, to_date):
            change = rng.gauss(drift, 0.016)
            price = max(1.0, price * (1 + change))
            open_ = price * (1 + rng.uniform(-0.004, 0.004))
            high = max(open_, price) * (1 + rng.uniform(0, 0.006))
            low = min(open_, price) * (1 - rng.uniform(0, 0.006))
            volume = rng.randint(200_000, 8_000_000)
            points.append(
                PricePoint(
                    symbol=symbol,
                    trade_date=d,
                    open=round(open_, 2),
                    high=round(high, 2),
                    low=round(low, 2),
                    close=round(price, 2),
                    adjusted_close=round(price, 2),
                    volume=volume,
                    source=self.name,
                )
            )
        return points

    def get_historical_daily(self, symbol: str, from_date: date, to_date: date) -> list[PricePoint]:
        return self._generate_series(symbol, from_date, to_date)

    def get_quote(self, symbol: str) -> Quote:
        today = datetime.now(IST).date()
        series = self._generate_series(symbol, today - timedelta(days=5), today)
        latest = series[-1] if series else None
        price = latest.effective_close if latest else _base_price_for(symbol)
        return Quote(symbol=symbol, latest_price=price, as_of=datetime.now(IST), source=self.name)

    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        return {s: self.get_quote(s) for s in symbols}


class MockFundamentalsProvider(FundamentalsDataProvider):
    name = "mock"

    def get_fundamentals(self, symbol: str, as_of: date) -> FundamentalSnapshot | None:
        rng = random.Random(_seed_for(symbol) + as_of.toordinal())
        pe = round(rng.uniform(8, 90), 2)
        peg = round(rng.uniform(0.3, 3.5), 2)
        eps = round(rng.uniform(5, 400), 2)
        market_cap = round(rng.uniform(50_000, 2_000_000) * 1e7, 2)  # crore -> rupees
        return FundamentalSnapshot(
            symbol=symbol,
            as_of_date=as_of,
            pe_ratio=pe,
            peg_ratio=peg,
            eps=eps,
            market_cap=market_cap,
            source=self.name,
            is_stale=False,
        )

    def get_dividend_history(self, symbol: str, from_date: date, to_date: date) -> list[DividendEvent]:
        rng = random.Random(_seed_for(symbol) + 7)
        events: list[DividendEvent] = []
        # roughly one or two dividend events per year in the window
        d = from_date
        while d <= to_date:
            if rng.random() < 0.006:  # ~ a couple of hits per ~365 daily draws
                amount = round(rng.uniform(2, 45), 2)
                dtype = rng.choice([DividendType.INTERIM, DividendType.FINAL])
                events.append(
                    DividendEvent(symbol=symbol, ex_date=d, amount_per_share=amount, dividend_type=dtype, source=self.name)
                )
            d += timedelta(days=1)
        return events
