from __future__ import annotations

from datetime import datetime

from supabase import Client

from src.models.alert import Alert


def list_alerts(client: Client, user_id: str) -> list[Alert]:
    resp = client.table("alerts").select("*").eq("user_id", user_id).order("created_at", desc=True).execute()
    return [Alert.model_validate(r) for r in (resp.data or [])]


def list_active_alerts_for_symbol(client: Client, symbol: str | None) -> list[Alert]:
    """Service-role read across all users, used by alert_service during
    evaluation (RLS would otherwise scope this to a single user)."""
    query = client.table("alerts").select("*").eq("is_active", True)
    query = query.eq("symbol", symbol) if symbol else query.is_("symbol", "null")
    resp = query.execute()
    return [Alert.model_validate(r) for r in (resp.data or [])]


def create_alert(client: Client, alert: Alert) -> Alert:
    payload = alert.model_dump(mode="json", exclude={"id"}, exclude_none=True)
    resp = client.table("alerts").insert(payload).execute()
    return Alert.model_validate(resp.data[0])


def update_alert(client: Client, alert_id: str, updates: dict) -> None:
    client.table("alerts").update(updates).eq("id", alert_id).execute()


def delete_alert(client: Client, user_id: str, alert_id: str) -> None:
    client.table("alerts").delete().eq("user_id", user_id).eq("id", alert_id).execute()


def mark_triggered(client: Client, alert_id: str, triggered_at: datetime) -> None:
    client.table("alerts").update({"last_triggered_at": triggered_at.isoformat()}).eq("id", alert_id).execute()
