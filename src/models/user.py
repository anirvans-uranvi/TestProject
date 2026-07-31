from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from src.models.enums import Theme


class UserSettings(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: str
    dividend_yield_threshold: float = 3.0
    peg_threshold: float = 1.0
    stale_data_threshold_minutes: int = 30
    theme: Theme = Theme.SYSTEM
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
