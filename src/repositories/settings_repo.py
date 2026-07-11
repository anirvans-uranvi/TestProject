from __future__ import annotations

from supabase import Client

from src.config import get_settings
from src.models.user import SavedFilter, UserPosition, UserSettings


def get_user_settings(client: Client, user_id: str) -> UserSettings:
    resp = client.table("user_settings").select("*").eq("user_id", user_id).limit(1).execute()
    rows = resp.data or []
    if rows:
        return UserSettings.model_validate(rows[0])
    defaults = get_settings()
    return UserSettings(
        user_id=user_id,
        dividend_yield_threshold=defaults.default_dividend_yield_threshold,
        peg_threshold=defaults.default_peg_threshold,
        stale_data_threshold_minutes=defaults.default_stale_data_threshold_minutes,
    )


def upsert_user_settings(client: Client, settings: UserSettings) -> None:
    payload = settings.model_dump(mode="json", exclude_none=True)
    client.table("user_settings").upsert(payload, on_conflict="user_id").execute()


def list_saved_filters(client: Client, user_id: str) -> list[SavedFilter]:
    resp = client.table("saved_filters").select("*").eq("user_id", user_id).order("created_at").execute()
    return [SavedFilter.model_validate(r) for r in (resp.data or [])]


def upsert_saved_filter(client: Client, saved_filter: SavedFilter) -> None:
    payload = saved_filter.model_dump(mode="json", exclude_none=True)
    client.table("saved_filters").upsert(payload, on_conflict="user_id,name").execute()


def delete_saved_filter(client: Client, user_id: str, filter_id: str) -> None:
    client.table("saved_filters").delete().eq("user_id", user_id).eq("id", filter_id).execute()


def get_user_position(client: Client, user_id: str, symbol: str) -> UserPosition | None:
    resp = (
        client.table("user_positions")
        .select("*")
        .eq("user_id", user_id)
        .eq("symbol", symbol)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    return UserPosition.model_validate(rows[0]) if rows else None


def upsert_user_position(client: Client, position: UserPosition) -> None:
    payload = position.model_dump(mode="json", exclude_none=True)
    client.table("user_positions").upsert(payload, on_conflict="user_id,symbol").execute()
