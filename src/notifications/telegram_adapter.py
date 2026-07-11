"""Extension point, not wired up in v1.

To implement: store each user's Telegram chat_id (e.g. add a column to
user_settings), construct with a bot token, and in send() POST to
https://api.telegram.org/bot<token>/sendMessage with the rendered
`event.message`. Return True only if Telegram's API confirms delivery.
"""
from __future__ import annotations

from src.models.alert import NotificationEvent
from src.notifications.base import NotificationAdapter


class TelegramAdapter(NotificationAdapter):
    channel = "telegram"

    def send(self, event: NotificationEvent) -> bool:
        raise NotImplementedError("TelegramAdapter is an extension point -- not implemented in v1")
