"""Notification channel abstraction so email/Telegram/Slack/browser-push
can be added later without touching alert_service."""
from __future__ import annotations

from abc import ABC, abstractmethod

from src.models.alert import NotificationEvent


class NotificationAdapter(ABC):
    channel: str

    @abstractmethod
    def send(self, event: NotificationEvent) -> bool:
        """Deliver one notification. Returns True if delivered/recorded,
        False if suppressed (e.g. a dedupe no-op)."""
