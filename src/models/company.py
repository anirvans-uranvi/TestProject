from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class Company(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    symbol: str
    name: str
    sector: str | None = None
    industry: str | None = None
    isin: str | None = None
    updated_at: datetime | None = None


class Nifty50Constituent(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    symbol: str
    company_name: str
    sector: str | None = None
    index_effective_from: date
    index_effective_to: date | None = None
    is_current: bool = True
