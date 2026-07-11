"""Trailing-12-month dividend yield calculation.

Whether the underlying dividend-event data is *missing* (fetch failure /
no fundamentals coverage) versus *confirmed zero* (company simply paid no
dividends in the window) is a data-quality judgment the caller must make
based on provider/fetch-log state — this module only does the math. An
empty `dividend_events` list is treated as a legitimately zero sum.
"""
from __future__ import annotations

from datetime import date, timedelta

from src.models.market_data import DividendEvent

TTM_WINDOW_DAYS = 365


def ttm_dividend_sum(
    dividend_events: list[DividendEvent],
    as_of_date: date,
    window_days: int = TTM_WINDOW_DAYS,
) -> float:
    window_start = as_of_date - timedelta(days=window_days)
    return sum(
        e.amount_per_share
        for e in dividend_events
        if window_start <= e.ex_date <= as_of_date
    )


def ttm_dividend_yield(
    dividend_events: list[DividendEvent],
    as_of_date: date,
    latest_price: float | None,
    window_days: int = TTM_WINDOW_DAYS,
) -> float | None:
    """(sum of TTM cash dividends / latest price) * 100.

    Returns None only when the yield is mathematically undefined (no
    price to divide by), not when there were simply no dividends.
    """
    if latest_price is None or latest_price <= 0:
        return None
    total = ttm_dividend_sum(dividend_events, as_of_date, window_days)
    return (total / latest_price) * 100
