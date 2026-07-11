"""Moving-average helpers for the Stock Detail price chart."""
from __future__ import annotations

import pandas as pd

DEFAULT_WINDOWS = (20, 50, 200)


def moving_average_series(closes: pd.Series, windows: tuple[int, ...] = DEFAULT_WINDOWS) -> pd.DataFrame:
    """Rolling means for each window, indexed like `closes`.

    Uses min_periods=window so a period without enough trailing history
    renders as NaN (no line) rather than a misleading partial average.
    """
    return pd.DataFrame({f"ma_{w}": closes.rolling(window=w, min_periods=w).mean() for w in windows})


def latest_moving_average(closes: list[float | None], window: int) -> float | None:
    """Scalar MA for a scorecard: None if fewer than `window` valid points."""
    valid = [c for c in closes[-window:] if c is not None]
    if len(closes) < window or len(valid) < window:
        return None
    return sum(valid) / window
