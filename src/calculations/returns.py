"""Percentage return calculations. Missing/insufficient data returns None
(never 0) — callers must not treat None as a failed momentum criterion."""
from __future__ import annotations


def pct_return(latest: float | None, base: float | None) -> float | None:
    """((latest / base) - 1) * 100, or None if either input is missing or
    base is zero (which would make the return undefined, not zero)."""
    if latest is None or base is None or base == 0:
        return None
    return ((latest / base) - 1) * 100


def return_n_trading_days_ago(
    latest_price: float | None,
    historical_closes: list[float | None],
    n: int,
) -> float | None:
    """Return over the last `n` trading days.

    `historical_closes` must be ordered oldest -> newest and represent
    completed-session closes (adjusted close preferred), NOT including
    today's live price. The close from `n` trading days ago is
    `historical_closes[-n]`.
    """
    if n <= 0 or latest_price is None:
        return None
    if len(historical_closes) < n:
        return None
    base = historical_closes[-n]
    return pct_return(latest_price, base)


def return_1d(latest_price: float | None, historical_closes: list[float | None]) -> float | None:
    return return_n_trading_days_ago(latest_price, historical_closes, 1)


def return_5d(latest_price: float | None, historical_closes: list[float | None]) -> float | None:
    return return_n_trading_days_ago(latest_price, historical_closes, 5)


def return_20d(latest_price: float | None, historical_closes: list[float | None]) -> float | None:
    return return_n_trading_days_ago(latest_price, historical_closes, 20)
