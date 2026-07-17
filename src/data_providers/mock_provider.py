"""Offline providers so the app runs end-to-end with zero paid credentials.

Price/fundamentals series are deterministic per symbol (seeded RNG) so repeat
runs and tests see stable numbers, while still varying enough across symbols
to exercise the full Green/Amber/Red/Unavailable spread on the Dashboard.
"""
from __future__ import annotations

import calendar
import math
import random
from datetime import date, datetime, timedelta

import pytz

from src.data_providers.base import FundamentalsDataProvider, PriceDataProvider
from src.data_providers.nse_fo_provider import FOBhavcopy
from src.models.enums import DividendType, OptionType
from src.models.fo import (
    FuturesContract,
    FuturesDailyPrice,
    OptionContract,
    OptionDailyPrice,
)
from src.models.market_data import DividendEvent, FundamentalSnapshot, PricePoint, Quote

IST = pytz.timezone("Asia/Kolkata")
MOCK_FO_SOURCE = "mock_fo"


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
        base_price = _base_price_for(symbol)
        week_52_high = round(base_price * rng.uniform(1.05, 1.4), 2)
        week_52_low = round(base_price * rng.uniform(0.6, 0.95), 2)
        return FundamentalSnapshot(
            symbol=symbol,
            as_of_date=as_of,
            pe_ratio=pe,
            peg_ratio=peg,
            eps=eps,
            market_cap=market_cap,
            week_52_high=week_52_high,
            week_52_low=week_52_low,
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


def _last_thursday(year: int, month: int) -> date:
    """NSE monthly F&O contracts expire on the last Thursday of the month."""
    last_day = calendar.monthrange(year, month)[1]
    d = date(year, month, last_day)
    while d.weekday() != 3:  # 3 == Thursday
        d -= timedelta(days=1)
    return d


def _monthly_expiries(as_of: date, count: int = 3) -> list[date]:
    """The next `count` monthly expiries on/after `as_of` (near/next/far)."""
    expiries: list[date] = []
    year, month = as_of.year, as_of.month
    while len(expiries) < count:
        exp = _last_thursday(year, month)
        if exp >= as_of:
            expiries.append(exp)
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return expiries


def _strike_step(spot: float) -> float:
    if spot < 250:
        return 5.0
    if spot < 500:
        return 10.0
    if spot < 1000:
        return 20.0
    if spot < 2500:
        return 50.0
    return 100.0


class MockFOProvider:
    """Synthetic futures + option chains so the F&O screen, ingestion path
    and tests run with zero network. Deterministic per (symbol, trade_date):
    3 monthly futures expiries and an option chain of strikes stepped around
    a spot derived from the same per-symbol base price the cash mock uses.
    Shaped as an `FOBhavcopy` so it feeds `fo_service.ingest_fo_day`
    identically to the real NSE provider.
    """

    name = MOCK_FO_SOURCE
    strikes_each_side = 10

    def _spot_for(self, symbol: str, trade_date: date) -> float:
        rng = random.Random(_seed_for(symbol) + trade_date.toordinal())
        return round(_base_price_for(symbol) * (1 + rng.uniform(-0.05, 0.05)), 2)

    def fetch_day(self, trade_date: date, universe: set[str] | None = None) -> FOBhavcopy:
        symbols = sorted(universe) if universe else []
        book = FOBhavcopy(trade_date=trade_date)
        for symbol in symbols:
            self._add_symbol(book, symbol, trade_date)
        return book

    def _add_symbol(self, book: FOBhavcopy, symbol: str, trade_date: date) -> None:
        rng = random.Random(_seed_for(symbol) + trade_date.toordinal() * 31)
        spot = self._spot_for(symbol, trade_date)
        lot = 100
        expiries = _monthly_expiries(trade_date, 3)
        step = _strike_step(spot)
        atm = round(spot / step) * step

        for exp_i, expiry in enumerate(expiries):
            days_to_exp = max((expiry - trade_date).days, 1)
            # Futures: small positive basis that shrinks toward expiry.
            fut_price = round(spot * (1 + 0.0008 * days_to_exp / 30 * (exp_i + 1)), 2)
            book.futures_contracts.append(
                FuturesContract(
                    symbol=symbol, expiry_date=expiry,
                    contract_name=f"{symbol}{expiry:%y%b}FUT".upper(),
                    lot_size=lot, is_open=True,
                    first_seen_date=trade_date, last_seen_date=trade_date,
                )
            )
            book.futures_prices.append(
                FuturesDailyPrice(
                    symbol=symbol, expiry_date=expiry, trade_date=trade_date,
                    open=round(fut_price * (1 + rng.uniform(-0.004, 0.004)), 2),
                    high=round(fut_price * (1 + rng.uniform(0, 0.006)), 2),
                    low=round(fut_price * (1 - rng.uniform(0, 0.006)), 2),
                    close=fut_price, last_price=fut_price,
                    prev_close=round(fut_price * (1 + rng.uniform(-0.01, 0.01)), 2),
                    settlement_price=fut_price, underlying_price=spot,
                    open_interest=rng.randint(20, 400) * lot * 100,
                    change_in_oi=rng.randint(-40, 40) * lot * 100,
                    volume=rng.randint(1_000, 60_000),
                    turnover=round(fut_price * lot * rng.randint(1_000, 60_000), 2),
                    num_trades=rng.randint(100, 5_000),
                    source=MOCK_FO_SOURCE,
                )
            )

            for k in range(-self.strikes_each_side, self.strikes_each_side + 1):
                strike = round(atm + k * step, 2)
                if strike <= 0:
                    continue
                moneyness = (strike - spot) / (spot * 0.10)
                time_value = spot * 0.02 * math.exp(-0.5 * moneyness * moneyness) * (0.6 + 0.4 * (exp_i + 1))
                oi_bell = math.exp(-0.5 * moneyness * moneyness)
                for option_type in (OptionType.CE, OptionType.PE):
                    intrinsic = max(0.0, spot - strike) if option_type == OptionType.CE else max(0.0, strike - spot)
                    price = round(max(0.05, intrinsic + time_value), 2)
                    oi = int(rng.randint(5, 60) * lot * 100 * (0.2 + oi_bell))
                    book.option_contracts.append(
                        OptionContract(
                            symbol=symbol, expiry_date=expiry, strike_price=strike,
                            option_type=option_type,
                            contract_name=f"{symbol}{expiry:%y%b}{int(strike)}{option_type.value}".upper(),
                            lot_size=lot, is_open=True,
                            first_seen_date=trade_date, last_seen_date=trade_date,
                        )
                    )
                    book.option_prices.append(
                        OptionDailyPrice(
                            symbol=symbol, expiry_date=expiry, strike_price=strike,
                            option_type=option_type, trade_date=trade_date,
                            open=round(price * (1 + rng.uniform(-0.03, 0.03)), 2),
                            high=round(price * (1 + rng.uniform(0, 0.06)), 2),
                            low=round(price * (1 - rng.uniform(0, 0.06)), 2),
                            close=price, last_price=price,
                            prev_close=round(price * (1 + rng.uniform(-0.05, 0.05)), 2),
                            settlement_price=price, underlying_price=spot,
                            open_interest=oi,
                            change_in_oi=int(oi * rng.uniform(-0.15, 0.15)),
                            volume=int(rng.randint(0, 5_000) * (0.2 + oi_bell)),
                            turnover=round(price * lot * rng.randint(0, 5_000), 2),
                            num_trades=rng.randint(0, 2_000),
                            source=MOCK_FO_SOURCE,
                        )
                    )
