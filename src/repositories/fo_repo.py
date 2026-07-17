"""Read/write access to the F&O tables (migration 0007).

Writes go through the service-role client (bypasses RLS); reads work under
either client. Contract dimensions and daily-price facts are upserted on
their natural keys, mirroring price_repo/snapshot_repo. The "latest" reads
hit the `latest_*_view`s so the current term structure / option chain come
back in one query.
"""
from __future__ import annotations

from datetime import date

from supabase import Client

from src.models.enums import OptionType
from src.models.fo import (
    FuturesContract,
    FuturesDailyPrice,
    OptionContract,
    OptionDailyPrice,
)

# supabase-py caps a single request; chunk large option batches.
_CHUNK = 500


def _chunked(rows: list[dict], size: int = _CHUNK):
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


# ---------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------

def upsert_futures_contracts(client: Client, contracts: list[FuturesContract]) -> None:
    if not contracts:
        return
    payload = [c.model_dump(mode="json", exclude_none=True) for c in contracts]
    for chunk in _chunked(payload):
        client.table("futures_contracts").upsert(chunk, on_conflict="symbol,expiry_date").execute()


def upsert_futures_prices(client: Client, prices: list[FuturesDailyPrice]) -> None:
    if not prices:
        return
    payload = [p.model_dump(mode="json", exclude_none=True) for p in prices]
    for chunk in _chunked(payload):
        client.table("futures_daily_prices").upsert(chunk, on_conflict="symbol,expiry_date,trade_date").execute()


def upsert_option_contracts(client: Client, contracts: list[OptionContract]) -> None:
    if not contracts:
        return
    payload = [c.model_dump(mode="json", exclude_none=True) for c in contracts]
    for chunk in _chunked(payload):
        client.table("option_contracts").upsert(chunk, on_conflict="symbol,expiry_date,strike_price,option_type").execute()


def upsert_option_prices(client: Client, prices: list[OptionDailyPrice]) -> None:
    if not prices:
        return
    payload = [p.model_dump(mode="json", exclude_none=True) for p in prices]
    for chunk in _chunked(payload):
        client.table("option_daily_prices").upsert(chunk, on_conflict="symbol,expiry_date,strike_price,option_type,trade_date").execute()


def refresh_open_flags(client: Client, as_of: date) -> None:
    """Contracts appear in the bhavcopy only while live, so `is_open` must be
    (re)derived against the real calendar, not the file's own trade date:
    open iff expiry has not passed as of `as_of`."""
    iso = as_of.isoformat()
    for table in ("futures_contracts", "option_contracts"):
        client.table(table).update({"is_open": True}).gte("expiry_date", iso).execute()
        client.table(table).update({"is_open": False}).lt("expiry_date", iso).execute()


# ---------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------

def list_fo_symbols(client: Client) -> list[str]:
    """Underlyings that currently have at least one open futures contract."""
    resp = client.table("futures_contracts").select("symbol").eq("is_open", True).execute()
    return sorted({r["symbol"] for r in (resp.data or [])})


def get_open_futures(client: Client, symbol: str) -> list[dict]:
    """Open futures term structure (near/next/far) with latest prices."""
    resp = (
        client.table("latest_futures_view")
        .select("*")
        .eq("symbol", symbol)
        .order("expiry_date", desc=False)
        .execute()
    )
    return resp.data or []


def get_futures_daily(
    client: Client, symbol: str, expiry_date: date, from_date: date, to_date: date
) -> list[FuturesDailyPrice]:
    resp = (
        client.table("futures_daily_prices")
        .select("*")
        .eq("symbol", symbol)
        .eq("expiry_date", expiry_date.isoformat())
        .gte("trade_date", from_date.isoformat())
        .lte("trade_date", to_date.isoformat())
        .order("trade_date", desc=False)
        .execute()
    )
    return [FuturesDailyPrice.model_validate(r) for r in (resp.data or [])]


def list_option_expiries(client: Client, symbol: str) -> list[date]:
    resp = (
        client.table("option_contracts")
        .select("expiry_date")
        .eq("symbol", symbol)
        .eq("is_open", True)
        .execute()
    )
    return sorted({date.fromisoformat(r["expiry_date"]) for r in (resp.data or [])})


def get_option_chain(client: Client, symbol: str, expiry_date: date) -> list[dict]:
    """Latest row per open CE/PE contract for one symbol+expiry (raw, one row
    per option leg; use fo_service.shape_option_chain to pivot to strikes)."""
    resp = (
        client.table("latest_option_chain_view")
        .select("*")
        .eq("symbol", symbol)
        .eq("expiry_date", expiry_date.isoformat())
        .order("strike_price", desc=False)
        .execute()
    )
    return resp.data or []


def get_option_daily(
    client: Client,
    symbol: str,
    expiry_date: date,
    strike_price: float,
    option_type: OptionType,
    from_date: date,
    to_date: date,
) -> list[OptionDailyPrice]:
    resp = (
        client.table("option_daily_prices")
        .select("*")
        .eq("symbol", symbol)
        .eq("expiry_date", expiry_date.isoformat())
        .eq("strike_price", strike_price)
        .eq("option_type", option_type.value)
        .gte("trade_date", from_date.isoformat())
        .lte("trade_date", to_date.isoformat())
        .order("trade_date", desc=False)
        .execute()
    )
    return [OptionDailyPrice.model_validate(r) for r in (resp.data or [])]
