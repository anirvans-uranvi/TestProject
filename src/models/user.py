from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from src.models.enums import Theme

DataProvider = Literal["dhan", "yfinance_bhavcopy"]


class UserSettings(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: str
    dividend_yield_threshold: float = 3.0
    peg_threshold: float = 1.0
    stale_data_threshold_minutes: int = 30
    theme: Theme = Theme.SYSTEM
    data_provider: DataProvider = "yfinance_bhavcopy"
    """Which live source prices this account's stock LTP everywhere it's
    shown (Dashboard, Stock Detail, and the portfolio pages' own broker-
    live overrides). Fundamentals (PEG/dividend) and the full F&O chain
    are NOT provider-branched -- Dhan's API doesn't expose that data, so
    those stay yfinance/NSE+BSE-bhavcopy-sourced regardless of this
    setting (see migration 0028). Zerodha used to be a third option here
    until it was removed entirely -- see
    src/utils/data_provider_settings.py's module docstring for why."""
    updated_at: datetime | None = None


class SavedFilter(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str | None = None
    user_id: str
    name: str
    filter_json: dict = {}
    created_at: datetime | None = None


class UserPosition(BaseModel):
    """Entry/target/stop-loss/notes a user saves for a symbol -- Stock
    Detail's price chart overlays entry/target/stop-loss as reference
    lines for whichever position (if any) is already saved for the
    selected symbol."""

    model_config = ConfigDict(from_attributes=True)

    id: str | None = None
    user_id: str
    symbol: str
    entry_price: float | None = None
    target_price: float | None = None
    stop_loss: float | None = None
    notes: str | None = None
    holding_period_days: int | None = None
    updated_at: datetime | None = None
