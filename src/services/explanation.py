"""Plain-English classification explanation for the Stock Detail page."""
from __future__ import annotations

from src.models.enums import ScreenerStatus
from src.models.screener import ScreenerRow


def explain_classification(row: ScreenerRow) -> str:
    if row.status == ScreenerStatus.UNAVAILABLE:
        missing = []
        dq = row.data_quality
        if dq.is_stale:
            missing.append("its data is stale beyond the configured freshness threshold")
        if dq.missing_dividend_data:
            missing.append("dividend yield could not be computed")
        if dq.missing_peg:
            missing.append("PEG ratio is missing")
        if dq.missing_return_1d or dq.missing_return_5d or dq.missing_return_20d:
            missing.append("one or more momentum returns are missing")
        if dq.missing_price:
            missing.append("the latest price is missing")
        reason = "; ".join(missing) if missing else "required inputs are missing"
        return f"{row.name} is Unavailable because {reason}. Missing data is never treated as a failed criterion."

    if row.criterion_a is None:
        dividend_phrase = "its dividend yield could not be checked"
    elif row.criterion_a:
        yield_str = f"{row.ttm_dividend_yield:.2f}%" if row.ttm_dividend_yield is not None else "above the threshold"
        dividend_phrase = f"its trailing dividend yield ({yield_str}) clears the threshold"
    else:
        yield_str = f"{row.ttm_dividend_yield:.2f}%" if row.ttm_dividend_yield is not None else "below the threshold"
        dividend_phrase = f"its trailing dividend yield ({yield_str}) is below the threshold"

    if row.criterion_b is None:
        momentum_phrase = "its momentum returns could not be checked"
    elif row.criterion_b:
        momentum_phrase = "its 1-day, 5-day, and 20-day returns are all positive"
    else:
        momentum_phrase = "at least one of its 1-day, 5-day, or 20-day returns is not positive"

    if row.criterion_c is None:
        peg_phrase = "its PEG ratio could not be checked"
    elif row.criterion_c:
        peg_str = f"{row.peg_ratio:.2f}" if row.peg_ratio is not None else "above the threshold"
        peg_phrase = f"its PEG ratio ({peg_str}) is above the threshold"
    else:
        peg_str = f"{row.peg_ratio:.2f}" if row.peg_ratio is not None else "at or below the threshold"
        peg_phrase = f"its PEG ratio ({peg_str}) is at or below the threshold"

    lead = {
        ScreenerStatus.GREEN: f"{row.name} is Green: all three criteria pass.",
        ScreenerStatus.AMBER: f"{row.name} is Amber: only some criteria pass.",
        ScreenerStatus.RED: f"{row.name} is Red: none of the three criteria pass.",
    }[row.status]

    return f"{lead} Dividend: {dividend_phrase}. Momentum: {momentum_phrase}. PEG: {peg_phrase}."
