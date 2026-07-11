from __future__ import annotations

from datetime import date

from supabase import Client

from src.models.market_data import PricePoint


def upsert_price_history(client: Client, points: list[PricePoint]) -> None:
    if not points:
        return
    payload = [p.model_dump(mode="json", exclude_none=True) for p in points]
    client.table("price_history").upsert(payload, on_conflict="symbol,trade_date").execute()


def get_price_history(client: Client, symbol: str, from_date: date, to_date: date) -> list[PricePoint]:
    resp = (
        client.table("price_history")
        .select("*")
        .eq("symbol", symbol)
        .gte("trade_date", from_date.isoformat())
        .lte("trade_date", to_date.isoformat())
        .order("trade_date", desc=False)
        .execute()
    )
    return [PricePoint.model_validate(r) for r in (resp.data or [])]


def get_latest_close(client: Client, symbol: str) -> PricePoint | None:
    resp = (
        client.table("price_history")
        .select("*")
        .eq("symbol", symbol)
        .order("trade_date", desc=True)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    return PricePoint.model_validate(rows[0]) if rows else None
