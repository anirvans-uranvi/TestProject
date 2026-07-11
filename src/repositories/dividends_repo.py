from __future__ import annotations

from datetime import date

from supabase import Client

from src.models.market_data import DividendEvent


def upsert_dividend_events(client: Client, events: list[DividendEvent]) -> None:
    if not events:
        return
    payload = [e.model_dump(mode="json", exclude_none=True) for e in events]
    client.table("dividend_events").upsert(payload, on_conflict="symbol,ex_date,amount_per_share").execute()


def get_dividend_events(client: Client, symbol: str, from_date: date, to_date: date) -> list[DividendEvent]:
    resp = (
        client.table("dividend_events")
        .select("*")
        .eq("symbol", symbol)
        .gte("ex_date", from_date.isoformat())
        .lte("ex_date", to_date.isoformat())
        .order("ex_date", desc=False)
        .execute()
    )
    return [DividendEvent.model_validate(r) for r in (resp.data or [])]
