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


def value_change_from_pct(current_value: float | None, return_pct: float | None) -> float | None:
    """Absolute change implied by a percentage return over some current
    value. Inverts pct_return: if `return_pct = ((current/base) - 1) * 100`
    then `base = current / (1 + return_pct/100)` and the change is
    `current - base`. `current_value` can be a per-unit price (LTP) or a
    whole position's current value (qty * LTP) -- the relationship is
    linear either way, so the same formula gives either a per-share change
    or a total rupee change for the holding, whichever was passed in."""
    if current_value is None or return_pct is None:
        return None
    denom = 100 + return_pct
    if denom == 0:
        return None
    return current_value * return_pct / denom


def live_return_1d(live_price: float | None, previous_close: float | None, fallback: float | None) -> float | None:
    """Today's 1D return using a live intraday price against the most
    recent EOD close, when both are available -- pct_return(live_price,
    previous_close). Falls back to `fallback` (the stored EOD-vs-EOD
    return_1d already computed by the daily screener refresh) when
    there's no live price for this symbol yet (no broker connected, an
    expired token, an account without live quotes, or the broker simply
    didn't quote this symbol this round)."""
    live = pct_return(live_price, previous_close)
    return live if live is not None else fallback


def return_1d(latest_price: float | None, historical_closes: list[float | None]) -> float | None:
    return return_n_trading_days_ago(latest_price, historical_closes, 1)


def return_5d(latest_price: float | None, historical_closes: list[float | None]) -> float | None:
    return return_n_trading_days_ago(latest_price, historical_closes, 5)


def return_20d(latest_price: float | None, historical_closes: list[float | None]) -> float | None:
    return return_n_trading_days_ago(latest_price, historical_closes, 20)
