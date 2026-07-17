from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict

from src.models.enums import DividendType


class PricePoint(BaseModel):
    """A single day's OHLCV row, as normalized into price_history."""

    model_config = ConfigDict(from_attributes=True)

    symbol: str
    trade_date: date
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    adjusted_close: float | None = None
    volume: int | None = None
    source: str = "unknown"

    @property
    def effective_close(self) -> float | None:
        """Prefer the adjusted close for return calculations, per spec."""
        return self.adjusted_close if self.adjusted_close is not None else self.close


class Quote(BaseModel):
    """Latest real-time (or delayed) quote from the price provider."""

    symbol: str
    latest_price: float
    as_of: datetime
    source: str = "unknown"


class FundamentalSnapshot(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    symbol: str
    as_of_date: date
    pe_ratio: float | None = None
    peg_ratio: float | None = None
    eps: float | None = None
    market_cap: float | None = None
    week_52_high: float | None = None
    week_52_low: float | None = None
    source: str = "unknown"
    is_stale: bool = False


class DividendEvent(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    symbol: str
    ex_date: date
    amount_per_share: float
    dividend_type: DividendType = DividendType.FINAL
    source: str = "unknown"
