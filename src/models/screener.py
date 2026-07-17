from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict

from src.models.enums import ScreenerStatus


class DataQuality(BaseModel):
    """Which inputs were missing/stale when a row was classified.

    Kept explicit (rather than inferred later) so the Dashboard/Stock
    Detail pages can always explain *why* a row is Unavailable.
    """

    missing_price: bool = False
    missing_pe: bool = False
    missing_peg: bool = False
    missing_dividend_data: bool = False
    missing_return_1d: bool = False
    missing_return_5d: bool = False
    missing_return_20d: bool = False
    is_stale: bool = False
    stale_minutes: float | None = None
    notes: list[str] = []


class ClassificationResult(BaseModel):
    """Pure output of src.calculations.classification.classify()."""

    status: ScreenerStatus
    criterion_a: bool | None  # dividend yield > threshold
    criterion_b: bool | None  # all three momentum periods positive
    criterion_c: bool | None  # PEG > threshold
    data_quality: DataQuality

    @property
    def passed_count(self) -> int:
        return sum(1 for c in (self.criterion_a, self.criterion_b, self.criterion_c) if c is True)


class ScreenerRow(BaseModel):
    """One fully-assembled row for the Dashboard table / API layer."""

    model_config = ConfigDict(from_attributes=True)

    symbol: str
    name: str
    sector: str | None = None
    industry: str | None = None
    snapshot_date: date | None = None
    latest_price: float | None = None
    return_1d: float | None = None
    return_5d: float | None = None
    return_20d: float | None = None
    ttm_dividend_yield: float | None = None
    pe_ratio: float | None = None
    peg_ratio: float | None = None
    week_52_high: float | None = None
    week_52_low: float | None = None
    criterion_a: bool | None = None
    criterion_b: bool | None = None
    criterion_c: bool | None = None
    criterion_52w_high: bool | None = None
    criterion_52w_low: bool | None = None
    status: ScreenerStatus = ScreenerStatus.UNAVAILABLE
    data_quality: DataQuality = DataQuality()


class DailyScreenerSnapshot(BaseModel):
    """Row persisted to daily_screener_snapshots — the audit trail."""

    model_config = ConfigDict(from_attributes=True)

    symbol: str
    snapshot_date: date
    latest_price: float | None = None
    return_1d: float | None = None
    return_5d: float | None = None
    return_20d: float | None = None
    ttm_dividend_yield: float | None = None
    pe_ratio: float | None = None
    peg_ratio: float | None = None
    week_52_high: float | None = None
    week_52_low: float | None = None
    criterion_a: bool | None = None
    criterion_b: bool | None = None
    criterion_c: bool | None = None
    criterion_52w_high: bool | None = None
    criterion_52w_low: bool | None = None
    status: ScreenerStatus
    data_quality: DataQuality = DataQuality()
    created_at: datetime | None = None
