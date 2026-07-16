from __future__ import annotations

from supabase import Client

from src.models.fetch_log import ProviderFetchLog


def log_fetch(client: Client, entry: ProviderFetchLog) -> None:
    payload = entry.model_dump(mode="json", exclude={"id"}, exclude_none=True)
    client.table("provider_fetch_log").insert(payload).execute()


def list_recent(client: Client, limit: int = 50) -> list[ProviderFetchLog]:
    resp = client.table("provider_fetch_log").select("*").order("started_at", desc=True).limit(limit).execute()
    return [ProviderFetchLog.model_validate(r) for r in (resp.data or [])]


def get_last_successful_fetch(client: Client, fetch_type: str | list[str]) -> ProviderFetchLog | None:
    """`fetch_type` may be a single value or a list -- pass e.g.
    `["intraday_price", "all"]` to find the most recent successful fetch
    across both the cron path's per-mode logging and the on-demand
    manual-refresh Edge Function's single combined "all" entry."""
    query = client.table("provider_fetch_log").select("*").eq("status", "success")
    query = query.in_("fetch_type", fetch_type) if isinstance(fetch_type, list) else query.eq("fetch_type", fetch_type)
    resp = query.order("started_at", desc=True).limit(1).execute()
    rows = resp.data or []
    return ProviderFetchLog.model_validate(rows[0]) if rows else None
