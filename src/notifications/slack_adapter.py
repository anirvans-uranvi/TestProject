"""Extension point, not wired up in v1.

To implement: store a per-user (or workspace-wide) Slack incoming-webhook
URL, and in send() POST a JSON payload built from `event` to that webhook.
Return True only on a 2xx response from Slack.
"""
from __future__ import annotations

from src.models.alert import NotificationEvent
from src.notifications.base import NotificationAdapter


class SlackAdapter(NotificationAdapter):
    channel = "slack"

    def send(self, event: NotificationEvent) -> bool:
        raise NotImplementedError("SlackAdapter is an extension point -- not implemented in v1")
