from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from src.models.enums import FetchStatus, FetchType


class ProviderFetchLog(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    provider_name: str
    fetch_type: FetchType
    symbol: str | None = None
    status: FetchStatus
    error_message: str | None = None
    retry_count: int = 0
    started_at: datetime
    finished_at: datetime | None = None
