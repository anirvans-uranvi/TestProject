"""Futures & Options (F&O) domain models.

These mirror the four F&O tables from migration 0007: a *contract*
dimension (the open-contracts registry) and a flat *daily-price* fact
table, for each of futures and options. Field names match the DB columns
so `model_dump(mode="json")` upserts cleanly, exactly like
`src/models/market_data.py`.

All price/OI/volume fields come straight from the NSE F&O UDiFF bhavcopy.
Greeks / implied volatility are deliberately absent (not published by the
exchange; scoped out).
"""
from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict

from src.models.enums import OptionType


class FuturesContract(BaseModel):
    """One open (or once-open) stock-futures contract: its static identity."""

    model_config = ConfigDict(from_attributes=True)

    symbol: str
    expiry_date: date
    contract_name: str | None = None
    nse_token: str | None = None
    lot_size: int | None = None
    is_open: bool = True
    first_seen_date: date | None = None
    last_seen_date: date | None = None


class FuturesDailyPrice(BaseModel):
    """One trading day's OHLC/OI/volume row for a single futures contract."""

    model_config = ConfigDict(from_attributes=True)

    symbol: str
    expiry_date: date
    trade_date: date
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    last_price: float | None = None
    prev_close: float | None = None
    settlement_price: float | None = None
    underlying_price: float | None = None
    open_interest: int | None = None
    change_in_oi: int | None = None
    volume: int | None = None
    turnover: float | None = None
    num_trades: int | None = None
    source: str = "unknown"


class OptionContract(BaseModel):
    """One open (or once-open) stock-option contract: its static identity."""

    model_config = ConfigDict(from_attributes=True)

    symbol: str
    expiry_date: date
    strike_price: float
    option_type: OptionType
    contract_name: str | None = None
    nse_token: str | None = None
    lot_size: int | None = None
    is_open: bool = True
    first_seen_date: date | None = None
    last_seen_date: date | None = None


class OptionDailyPrice(BaseModel):
    """One trading day's OHLC/OI/volume row for a single option contract."""

    model_config = ConfigDict(from_attributes=True)

    symbol: str
    expiry_date: date
    strike_price: float
    option_type: OptionType
    trade_date: date
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    last_price: float | None = None
    prev_close: float | None = None
    settlement_price: float | None = None
    underlying_price: float | None = None
    open_interest: int | None = None
    change_in_oi: int | None = None
    volume: int | None = None
    turnover: float | None = None
    num_trades: int | None = None
    source: str = "unknown"
