from __future__ import annotations

from supabase import Client

from src.models.fetch_log import ProviderFetchLog


def log_fetch(client: Client, entry: ProviderFetchLog) -> None:
    payload = entry.model_dump(mode="json", exclude={"id"}, exclude_none=True)
    client.table("provider_fetch_log").insert(payload).execute()


def list_recent(client: Client, limit: int = 50) -> list[ProviderFetchLog]:
    resp = client.table("provider_fetch_log").select("*").order("started_at", desc=True).limit(limit).execute()
    return [ProviderFetchLog.model_validate(r) for r in (resp.data or [])]


def get_last_successful_fetch(client: Client, fetch_type: str) -> ProviderFetchLog | None:
    resp = (
        client.table("provider_fetch_log")
        .select("*")
        .eq("fetch_type", fetch_type)
        .eq("status", "success")
        .order("started_at", desc=True)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    return ProviderFetchLog.model_validate(rows[0]) if rows else None
