"""Green/Amber/Red/Unavailable classification engine.

Criteria (spec-exact):
    A = TTM dividend yield > threshold (default 3%)
    B = 1-day, 5-day, AND 20-day returns all > 0%  (exactly 0% is neutral, fails)
    C = PEG ratio <= threshold (default 1.0) -- a lower PEG is the desirable
        side (conventionally, PEG <= 1 suggests a stock priced reasonably
        relative to its earnings growth), so this criterion passes AT OR
        BELOW the threshold, unlike A and B which pass ABOVE theirs.

Rules:
    Unavailable — any of A/B/C cannot be computed (missing inputs), or the
                  data is stale beyond the configured threshold.
    Green       — A, B, and C all pass.
    Red         — none of A, B, C pass.
    Amber       — one or two of A, B, C pass.

A criterion evaluating to None (missing data) NEVER counts as a fail — the
row short-circuits to Unavailable before Green/Amber/Red logic runs.
"""
from __future__ import annotations

from src.models.enums import ScreenerStatus
from src.models.screener import ClassificationResult, DataQuality


def criterion_a(ttm_dividend_yield: float | None, threshold: float = 3.0) -> bool | None:
    if ttm_dividend_yield is None:
        return None
    return ttm_dividend_yield > threshold


def criterion_b(
    return_1d: float | None,
    return_5d: float | None,
    return_20d: float | None,
) -> bool | None:
    if return_1d is None or return_5d is None or return_20d is None:
        return None
    return return_1d > 0 and return_5d > 0 and return_20d > 0


def criterion_c(peg_ratio: float | None, threshold: float = 1.0) -> bool | None:
    if peg_ratio is None:
        return None
    return peg_ratio <= threshold


def criterion_52w_high(latest_price: float | None, week_52_high: float | None, threshold: float = 0.9) -> bool | None:
    """Display-only proximity check (not part of the Green/Amber/Red
    engine above): passes when price is comfortably below its 52-week
    high, i.e. latest_price < threshold * week_52_high."""
    if latest_price is None or week_52_high is None:
        return None
    return latest_price < threshold * week_52_high


def criterion_52w_low(latest_price: float | None, week_52_low: float | None, threshold: float = 1.1) -> bool | None:
    """Display-only proximity check (not part of the Green/Amber/Red
    engine above): passes when price has moved comfortably above its
    52-week low, i.e. latest_price > threshold * week_52_low."""
    if latest_price is None or week_52_low is None:
        return None
    return latest_price > threshold * week_52_low


def classify(
    a: bool | None,
    b: bool | None,
    c: bool | None,
    is_stale: bool = False,
) -> ScreenerStatus:
    if is_stale or a is None or b is None or c is None:
        return ScreenerStatus.UNAVAILABLE
    passed = sum((a, b, c))
    if passed == 3:
        return ScreenerStatus.GREEN
    if passed == 0:
        return ScreenerStatus.RED
    return ScreenerStatus.AMBER


def build_classification(
    ttm_dividend_yield: float | None,
    return_1d: float | None,
    return_5d: float | None,
    return_20d: float | None,
    peg_ratio: float | None,
    is_stale: bool = False,
    stale_minutes: float | None = None,
    dividend_yield_threshold: float = 3.0,
    peg_threshold: float = 1.0,
    latest_price: float | None = None,
    pe_ratio: float | None = None,
) -> ClassificationResult:
    """Assemble the full classification + data-quality picture for one row."""
    a = criterion_a(ttm_dividend_yield, dividend_yield_threshold)
    b = criterion_b(return_1d, return_5d, return_20d)
    c = criterion_c(peg_ratio, peg_threshold)
    status = classify(a, b, c, is_stale)

    dq = DataQuality(
        missing_price=latest_price is None,
        missing_pe=pe_ratio is None,
        missing_peg=peg_ratio is None,
        missing_dividend_data=ttm_dividend_yield is None,
        missing_return_1d=return_1d is None,
        missing_return_5d=return_5d is None,
        missing_return_20d=return_20d is None,
        is_stale=is_stale,
        stale_minutes=stale_minutes,
    )
    return ClassificationResult(status=status, criterion_a=a, criterion_b=b, criterion_c=c, data_quality=dq)
