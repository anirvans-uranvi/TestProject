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
from src.models.screener import ScreenerRow
from src.models.user import UserSettings


def recompute_with_user_thresholds(row: ScreenerRow, settings: UserSettings) -> ScreenerRow:
    stale_minutes = row.data_quality.stale_minutes
    is_stale = (
        stale_minutes > settings.stale_data_threshold_minutes
        if stale_minutes is not None
        else row.data_quality.is_stale
    )

    result = build_classification(
        ttm_dividend_yield=row.ttm_dividend_yield,
        return_1d=row.return_1d,
        return_5d=row.return_5d,
        return_20d=row.return_20d,
        peg_ratio=row.peg_ratio,
        is_stale=is_stale,
        stale_minutes=stale_minutes,
        dividend_yield_threshold=settings.dividend_yield_threshold,
        peg_threshold=settings.peg_threshold,
        latest_price=row.latest_price,
        pe_ratio=row.pe_ratio,
    )

    return row.model_copy(
        update={
            "criterion_a": result.criterion_a,
            "criterion_b": result.criterion_b,
            "criterion_c": result.criterion_c,
            "status": result.status,
            "data_quality": result.data_quality,
        }
    )


def apply_user_thresholds(rows: list[ScreenerRow], settings: UserSettings) -> list[ScreenerRow]:
    return [recompute_with_user_thresholds(row, settings) for row in rows]
