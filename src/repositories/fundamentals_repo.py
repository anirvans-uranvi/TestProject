from __future__ import annotations

from supabase import Client

from src.models.market_data import FundamentalSnapshot


def upsert_fundamental_snapshot(client: Client, snapshot: FundamentalSnapshot) -> None:
    payload = snapshot.model_dump(mode="json", exclude_none=True)
    client.table("fundamental_snapshots").upsert(payload, on_conflict="symbol,as_of_date").execute()


_CARRY_FORWARD_FIELDS = ("pe_ratio", "peg_ratio", "eps", "market_cap", "week_52_high", "week_52_low")


def carry_forward_fields(rows: list[dict]) -> dict[str, object]:
    """Pure helper: given fundamental_snapshots rows ordered newest-first,
    return the most recent non-null value of each field in
    _CARRY_FORWARD_FIELDS, searching independently per field.

    A single day's fetch commonly has gaps -- e.g. yfinance's PEG is
    intermittently null for a symbol even on a day PE/EPS came back fine,
    and an older snapshot may still hold a real value for it. Treating
    "missing in today's row" the same as "never available" would flag the
    stock Unavailable even though a perfectly good recent value exists, so
    each field is carried forward from the most recent row where it was
    actually present, not just taken wholesale from the single latest row.
    """
    carried: dict[str, object] = {field: None for field in _CARRY_FORWARD_FIELDS}
    for row in rows:
        for field in _CARRY_FORWARD_FIELDS:
            if carried[field] is None and row.get(field) is not None:
                carried[field] = row[field]
        if all(v is not None for v in carried.values()):
            break
    return carried


def get_latest_fundamentals(client: Client, symbol: str, lookback_rows: int = 200) -> FundamentalSnapshot | None:
    """Latest AVAILABLE value for each field -- see carry_forward_fields()."""
    resp = (
        client.table("fundamental_snapshots")
        .select("*")
        .eq("symbol", symbol)
        .order("as_of_date", desc=True)
        .limit(lookback_rows)
        .execute()
    )
    rows = resp.data or []
    if not rows:
        return None

    carried = carry_forward_fields(rows)
    latest = rows[0]
    return FundamentalSnapshot(
        symbol=symbol,
        as_of_date=latest["as_of_date"],
        pe_ratio=carried["pe_ratio"],
        peg_ratio=carried["peg_ratio"],
        eps=carried["eps"],
        market_cap=carried["market_cap"],
        week_52_high=carried["week_52_high"],
        week_52_low=carried["week_52_low"],
        source=latest["source"],
        is_stale=latest["is_stale"],
    )
