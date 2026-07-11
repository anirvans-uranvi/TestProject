from __future__ import annotations

from supabase import Client

from src.models.alert import NotificationEvent, NotificationLogEntry
from src.models.enums import NotificationChannel
from src.notifications.base import NotificationAdapter
from src.repositories import notification_repo


class InAppAdapter(NotificationAdapter):
    """Writes to notification_log; the Alerts page and st.toast surface it.
    Dedupe is enforced by the table's unique(dedupe_key) constraint."""

    channel = NotificationChannel.IN_APP.value

    def __init__(self, client: Client):
        self._client = client

    def send(self, event: NotificationEvent) -> bool:
        entry = NotificationLogEntry(
            alert_id=event.alert_id,
            user_id=event.user_id,
            symbol=event.symbol,
            message=event.message,
            payload={
                "stock_name": event.stock_name,
                "alert_type": event.alert_type,
                "current_price": event.current_price,
                "relevant_values": event.relevant_values,
                "status_change": event.status_change,
            },
            channel=NotificationChannel.IN_APP,
            triggered_at=event.triggered_at,
            dedupe_key=event.dedupe_key,
        )
        return notification_repo.insert_notification(self._client, entry)
