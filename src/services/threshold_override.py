"""daily_screener_snapshots (and the latest_screener_view built on top of
it) are computed server-side against the *default* thresholds at refresh
time -- that's the stable audit trail. A signed-in user can configure their
own dividend-yield / PEG / staleness thresholds in Settings, so the pages
re-run the pure classification function client-side against the stored
raw inputs (which are threshold-independent) to reflect that choice
without needing a fresh server-side recompute per user.
"""
from __future__ import annotations

from src.calculations.classification import build_classification
from src.calculations.returns import live_return_1d
from src.models.screener import ScreenerRow
from src.models.user import UserSettings


def recompute_with_user_thresholds(
    row: ScreenerRow, settings: UserSettings, live_price: float | None = None
) -> ScreenerRow:
    """`live_price`, when given, overrides both `latest_price` and
    `return_1d` (via `live_return_1d` -- live_price vs this row's own
    stored `latest_price`, which is always the most recent EOD close
    until today's own EOD refresh lands) before classification runs, so
    Momentum/status react to a live intraday quote instead of staying
    pinned to yesterday's EOD-vs-EOD figure. `None` (the default) keeps
    today's behavior exactly -- no live quote for this symbol."""
    stale_minutes = row.data_quality.stale_minutes
    is_stale = (
        stale_minutes > settings.stale_data_threshold_minutes
        if stale_minutes is not None
        else row.data_quality.is_stale
    )

    return_1d = row.return_1d
    latest_price = row.latest_price
    if live_price is not None:
        return_1d = live_return_1d(live_price, row.latest_price, row.return_1d)
        latest_price = live_price

    result = build_classification(
        ttm_dividend_yield=row.ttm_dividend_yield,
        return_1d=return_1d,
        return_5d=row.return_5d,
        return_20d=row.return_20d,
        peg_ratio=row.peg_ratio,
        is_stale=is_stale,
        stale_minutes=stale_minutes,
        dividend_yield_threshold=settings.dividend_yield_threshold,
        peg_threshold=settings.peg_threshold,
        latest_price=latest_price,
        pe_ratio=row.pe_ratio,
    )

    return row.model_copy(
        update={
            "latest_price": latest_price,
            "return_1d": return_1d,
            "criterion_a": result.criterion_a,
            "criterion_b": result.criterion_b,
            "criterion_c": result.criterion_c,
            "status": result.status,
            "data_quality": result.data_quality,
        }
    )


def apply_user_thresholds(
    rows: list[ScreenerRow], settings: UserSettings, live_prices: dict[str, float] | None = None
) -> list[ScreenerRow]:
    live_prices = live_prices or {}
    return [recompute_with_user_thresholds(row, settings, live_prices.get(row.symbol)) for row in rows]
