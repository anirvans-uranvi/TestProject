from __future__ import annotations

from datetime import datetime

from postgrest.exceptions import APIError
from supabase import Client

from src.models.alert import NotificationLogEntry

# Postgres unique_violation SQLSTATE, raised when dedupe_key already exists.
UNIQUE_VIOLATION = "23505"


def insert_notification(client: Client, entry: NotificationLogEntry) -> bool:
    """Returns False (no-op) if this exact notification was already sent --
    the unique constraint on dedupe_key is the authoritative dedupe guard."""
    payload = entry.model_dump(mode="json", exclude={"id"}, exclude_none=True)
    try:
        client.table("notification_log").insert(payload).execute()
        return True
    except APIError as exc:
        if getattr(exc, "code", None) == UNIQUE_VIOLATION:
            return False
        raise


def list_notifications(client: Client, user_id: str, limit: int = 50) -> list[NotificationLogEntry]:
    resp = (
        client.table("notification_log")
        .select("*")
        .eq("user_id", user_id)
        .order("triggered_at", desc=True)
        .limit(limit)
        .execute()
    )
    return [NotificationLogEntry.model_validate(r) for r in (resp.data or [])]


def mark_read(client: Client, user_id: str, notification_id: str) -> None:
    client.table("notification_log").update({"read_at": datetime.utcnow().isoformat()}).eq(
        "user_id", user_id
    ).eq("id", notification_id).execute()
