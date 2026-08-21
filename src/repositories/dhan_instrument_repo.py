"""Read/write access to the Dhan instrument-master cache (migration 0035)
-- dhan_equity_instruments/dhan_fo_instruments, the persisted, shared-
across-users result of downloading and filtering Dhan's instrument master
CSV (src/data_providers/dhan_provider.py's _load_instrument_master/
_load_fo_instrument_master). Plain dicts in/out, no pandas -- pandas stays
confined to the provider layer, same separation every other repo in this
codebase follows.

Both tables are "current state", not history -- replace_* always clears
the whole table first, same delete-then-insert convention as
fo_repo.clear_dashboard_fo_metrics/upsert_dashboard_fo_metrics.
"""
from __future__ import annotations

from supabase import Client

# supabase-py caps a single request; chunk large batches (the equity slice
# alone is typically several thousand rows).
_CHUNK = 500


def _chunked(rows: list[dict], size: int = _CHUNK):
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


def get_equity_instruments(client: Client) -> list[dict]:
    resp = client.table("dhan_equity_instruments").select("security_id, trading_symbol").execute()
    return resp.data or []


def replace_equity_instruments(client: Client, rows: list[dict]) -> None:
    client.table("dhan_equity_instruments").delete().neq("security_id", "").execute()
    for chunk in _chunked(rows):
        client.table("dhan_equity_instruments").insert(chunk).execute()


def get_fo_instruments(client: Client) -> list[dict]:
    resp = (
        client.table("dhan_fo_instruments")
        .select("security_id, underlying_symbol, expiry_date, strike_price, option_type")
        .execute()
    )
    return resp.data or []


def replace_fo_instruments(client: Client, rows: list[dict]) -> None:
    client.table("dhan_fo_instruments").delete().neq("security_id", "").execute()
    for chunk in _chunked(rows):
        client.table("dhan_fo_instruments").insert(chunk).execute()
