"""Extension point, not wired up in v1.

To implement: construct with SMTP/provider credentials (e.g. from
Settings), and in send() render `event` into an email and dispatch via
your provider's SDK (SES, SendGrid, Resend, etc.). Return True only on
confirmed delivery/queue-acceptance so alert_service's caller can decide
whether to also fall back to the in-app channel.
"""
from __future__ import annotations

from src.models.alert import NotificationEvent
from src.notifications.base import NotificationAdapter


class EmailAdapter(NotificationAdapter):
    channel = "email"

    def send(self, event: NotificationEvent) -> bool:
        raise NotImplementedError("EmailAdapter is an extension point -- not implemented in v1")
