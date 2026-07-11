from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from src.models.enums import AlertType, NotificationChannel


class Alert(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str | None = None
    user_id: str
    symbol: str | None = None  # None = portfolio-wide (e.g. refresh_failure)
    alert_type: AlertType
    config: dict = {}
    is_active: bool = True
    cooldown_minutes: int = 60
    last_triggered_at: datetime | None = None
    created_at: datetime | None = None


class NotificationEvent(BaseModel):
    """Produced by alert_service.evaluate_alerts(); not yet persisted."""

    alert_id: str | None
    user_id: str
    symbol: str | None
    stock_name: str | None
    alert_type: AlertType
    message: str
    current_price: float | None
    relevant_values: dict = {}
    status_change: str | None = None
    triggered_at: datetime
    dedupe_key: str
    channel: NotificationChannel = NotificationChannel.IN_APP


class NotificationLogEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str | None = None
    alert_id: str | None = None
    user_id: str
    symbol: str | None = None
    message: str
    payload: dict = {}
    channel: NotificationChannel = NotificationChannel.IN_APP
    triggered_at: datetime
    dedupe_key: str
    read_at: datetime | None = None
