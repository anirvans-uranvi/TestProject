from __future__ import annotations

from datetime import date

from supabase import Client

from src.models.screener import DailyScreenerSnapshot, ScreenerRow


def upsert_daily_snapshot(client: Client, snapshot: DailyScreenerSnapshot) -> None:
    payload = snapshot.model_dump(mode="json", exclude_none=True)
    payload["data_quality"] = snapshot.data_quality.model_dump(mode="json")
    client.table("daily_screener_snapshots").upsert(payload, on_conflict="symbol,snapshot_date").execute()


def get_latest_screener(client: Client) -> list[ScreenerRow]:
    """Reads latest_screener_view -- one joined row per current constituent."""
    resp = client.table("latest_screener_view").select("*").execute()
    return [ScreenerRow.model_validate(r) for r in (resp.data or [])]


def get_latest_screener_row(client: Client, symbol: str) -> ScreenerRow | None:
    resp = client.table("latest_screener_view").select("*").eq("symbol", symbol).limit(1).execute()
    rows = resp.data or []
    return ScreenerRow.model_validate(rows[0]) if rows else None


def get_previous_snapshot(client: Client, symbol: str, before_date: date) -> DailyScreenerSnapshot | None:
    resp = (
        client.table("daily_screener_snapshots")
        .select("*")
        .eq("symbol", symbol)
        .lt("snapshot_date", before_date.isoformat())
        .order("snapshot_date", desc=True)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    return DailyScreenerSnapshot.model_validate(rows[0]) if rows else None


def get_classification_history(client: Client, symbol: str, days: int = 180) -> list[dict]:
    resp = client.rpc("get_classification_history", {"p_symbol": symbol, "p_days": days}).execute()
    return resp.data or []
