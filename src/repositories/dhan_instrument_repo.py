"""Read/write access to the Dhan instrument-master cache (migration 0035)
-- dhan_equity_instruments/dhan_fo_instruments, the persisted, shared-
across-users result of downloading and filtering Dhan's instrument master
CSV (src/data_providers/dhan_provider.py's _load_instrument_master/
_load_fo_instrument_master). Plain dicts in/out, no pandas -- pandas stays
confined to the provider layer, same separation every other repo in this
codebase follows.

Both tables are "current state", not history -- replace_* always clears
the whole table first, same delete-then-write convention as
fo_repo.clear_dashboard_fo_metrics/upsert_dashboard_fo_metrics. The write
half is an upsert, not a plain insert -- see replace_equity_instruments'
own docstring for the cross-session race that requires.
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
    # upsert, not insert -- this table is shared across every user
    # (migration 0035's whole point), so two sessions can independently
    # find today's cache cold and race to repopulate it at once (most
    # likely right after IST midnight/market open, when everyone's first
    # click of the day hits a cold cache together). Both download
    # ~identical rows and both delete-then-write, so their inserts can
    # interleave against the same primary key (security_id) -- confirmed
    # live as `duplicate key value violates unique constraint
    # "dhan_equity_instruments_pkey"` from a plain `.insert()`. An upsert
    # makes a colliding chunk overwrite instead of erroring; the two
    # racing writers' data is the same Dhan download anyway, so whichever
    # finishes last just harmlessly re-writes the same rows.
    client.table("dhan_equity_instruments").delete().neq("security_id", "").execute()
    for chunk in _chunked(rows):
        client.table("dhan_equity_instruments").upsert(chunk, on_conflict="security_id").execute()


def get_fo_instruments(client: Client) -> list[dict]:
    resp = (
        client.table("dhan_fo_instruments")
        .select("security_id, underlying_symbol, expiry_date, strike_price, option_type")
        .execute()
    )
    return resp.data or []


def replace_fo_instruments(client: Client, rows: list[dict]) -> None:
    # upsert, not insert -- same cross-session race as
    # replace_equity_instruments above, on the same shared-cache table
    # family (migration 0035), same fix.
    client.table("dhan_fo_instruments").delete().neq("security_id", "").execute()
    for chunk in _chunked(rows):
        client.table("dhan_fo_instruments").upsert(chunk, on_conflict="security_id").execute()
