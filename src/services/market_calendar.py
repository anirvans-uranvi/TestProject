"""NSE trading-day/market-state logic, always in Asia/Kolkata.

The holiday list is hardcoded per calendar year and MUST be refreshed
annually (NSE publishes it each December) -- see the `NSE_HOLIDAYS`
mapping below. Missing a year falls back to weekday-only calculation.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytz

from src.models.enums import MarketState

IST = pytz.timezone("Asia/Kolkata")

MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
PRE_OPEN_START = time(9, 0)

# Source: NSE 2026 trading-holiday circular (verify against
# nseindia.com/resources/exchange-communication-holidays before relying on
# this for real trading decisions -- exchanges occasionally amend the list).
NSE_HOLIDAYS: dict[int, frozenset[date]] = {
    2026: frozenset(
        {
            date(2026, 1, 15),  # Special trading holiday (Maharashtra municipal elections)
            date(2026, 1, 26),  # Republic Day
            date(2026, 3, 3),   # Holi
            date(2026, 3, 26),  # Shri Ram Navami
            date(2026, 3, 31),  # Shri Mahavir Jayanti
            date(2026, 4, 3),   # Good Friday
            date(2026, 4, 14),  # Dr. Baba Saheb Ambedkar Jayanti
            date(2026, 5, 1),   # Maharashtra Day
            date(2026, 5, 28),  # Bakri Id
            date(2026, 6, 26),  # Muharram
            date(2026, 9, 14),  # Ganesh Chaturthi
            date(2026, 10, 2),  # Mahatma Gandhi Jayanti
            date(2026, 10, 20),  # Dussehra
            date(2026, 11, 10),  # Diwali - Balipratipada
            date(2026, 11, 24),  # Prakash Gurpurb Sri Guru Nanak Dev
            date(2026, 12, 25),  # Christmas
        }
    ),
}


def is_trading_day(d: date) -> bool:
    if d.weekday() >= 5:  # Sat/Sun
        return False
    return d not in NSE_HOLIDAYS.get(d.year, frozenset())


def previous_trading_day(d: date) -> date:
    prev = d - timedelta(days=1)
    while not is_trading_day(prev):
        prev -= timedelta(days=1)
    return prev


def trading_days_between(from_date: date, to_date: date) -> list[date]:
    days = []
    d = from_date
    while d <= to_date:
        if is_trading_day(d):
            days.append(d)
        d += timedelta(days=1)
    return days


def get_market_state(
    now: datetime | None = None,
    last_successful_fetch_at: datetime | None = None,
    stale_threshold_minutes: int = 30,
) -> MarketState:
    """Open/Closed/Pre-open/Data Delayed for the Dashboard header badge."""
    now = now.astimezone(IST) if now else datetime.now(IST)

    if not is_trading_day(now.date()):
        return MarketState.CLOSED

    current_time = now.time()
    if current_time < PRE_OPEN_START or current_time > MARKET_CLOSE:
        return MarketState.CLOSED
    if PRE_OPEN_START <= current_time < MARKET_OPEN:
        return MarketState.PRE_OPEN

    # Market is open per the clock; check whether we actually have fresh data.
    if last_successful_fetch_at is None:
        return MarketState.DATA_DELAYED
    age_minutes = (now - last_successful_fetch_at.astimezone(IST)).total_seconds() / 60
    if age_minutes > stale_threshold_minutes:
        return MarketState.DATA_DELAYED
    return MarketState.OPEN
