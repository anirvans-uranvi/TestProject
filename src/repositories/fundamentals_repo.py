from __future__ import annotations

from supabase import Client

from src.models.market_data import FundamentalSnapshot


def upsert_fundamental_snapshot(client: Client, snapshot: FundamentalSnapshot) -> None:
    payload = snapshot.model_dump(mode="json", exclude_none=True)
    client.table("fundamental_snapshots").upsert(payload, on_conflict="symbol,as_of_date").execute()


def get_latest_fundamentals(client: Client, symbol: str) -> FundamentalSnapshot | None:
    resp = (
        client.table("fundamental_snapshots")
        .select("*")
        .eq("symbol", symbol)
        .order("as_of_date", desc=True)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    return FundamentalSnapshot.model_validate(rows[0]) if rows else None
