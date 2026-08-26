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


def _paginate(query_builder, page_size: int = 1000) -> list[dict]:
    """Runs `query_builder` (a callable that returns a fresh query) across
    all pages -- same helper (and same real bug) as fo_repo._paginate:
    PostgREST caps a single response at a server-configured max (1000 rows
    on this project) regardless of how many rows actually match. Confirmed
    live here as the same class of silent truncation: get_equity_instruments
    without this returned exactly 1000 of 9,854 rows, and whichever
    trading_symbols sorted past that page (RELIANCE, TCS, HDFCBANK, SBIN,
    ...) resolved as if they didn't exist -- not an error, just a symbol
    that "couldn't be found", indistinguishable from a genuinely unlisted
    one. This only bites the *second* (or later) instrument-master load of
    an IST day -- the first, cache-cold load builds its DataFrame straight
    from the Dhan download and never round-trips through this SELECT at
    all (see dhan_provider.py's _load_instrument_master), which is why this
    looked like intermittent Dhan-side flakiness rather than a deterministic
    bug: whichever caller happens to hit the warm in-memory cache (the same
    Streamlit process that did the download) always saw full resolution,
    while a different process/worker reading the "already fresh today" DB
    cache saw the truncated 1000-row slice every time."""
    rows: list[dict] = []
    offset = 0
    while True:
        page = query_builder().range(offset, offset + page_size - 1).execute().data or []
        rows.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    return rows


def get_equity_instruments(client: Client) -> list[dict]:
    return _paginate(lambda: client.table("dhan_equity_instruments").select("security_id, trading_symbol"))


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
    # Paginated for the same reason get_equity_instruments is -- this
    # table is even more exposed to the truncation bug (85,000+ rows vs.
    # the equity slice's ~10,000), so the DB-cache-read path was silently
    # resolving well under 2% of F&O contracts before this fix.
    return _paginate(
        lambda: client.table("dhan_fo_instruments").select(
            "security_id, underlying_symbol, expiry_date, strike_price, option_type, exchange"
        )
    )


def replace_fo_instruments(client: Client, rows: list[dict]) -> None:
    # upsert, not insert -- same cross-session race as
    # replace_equity_instruments above, on the same shared-cache table
    # family (migration 0035), same fix.
    client.table("dhan_fo_instruments").delete().neq("security_id", "").execute()
    for chunk in _chunked(rows):
        client.table("dhan_fo_instruments").upsert(chunk, on_conflict="security_id").execute()
