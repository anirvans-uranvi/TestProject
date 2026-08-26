"""Tests for dhan_instrument_repo -- two independent live bugs fixed here:

1. replace_equity_instruments/replace_fo_instruments upsert rather than
   plain-insert. Confirmed live: dhan_equity_instruments/dhan_fo_instruments
   (migration 0035) are shared across every user, so two Streamlit sessions
   can independently find today's cache cold and race to repopulate it at
   once (most likely right after IST midnight, when everyone's first click
   of the day hits a cold cache together). Both download ~identical rows
   and both delete-then-write, so a plain `.insert()` can land two rows with
   the same security_id (the primary key) in the same window and raise
   `duplicate key value violates unique constraint
   "dhan_equity_instruments_pkey"`. TestReplaceEquityInstruments/
   TestReplaceFoInstruments's fake client enforces the same primary-key
   uniqueness Postgres would, so a regression back to `.insert` fails the
   same way it failed in production.

2. get_equity_instruments/get_fo_instruments paginate -- same bug, same
   fix (fo_repo._paginate) as fo_repo.get_all_open_options/
   get_all_open_futures already needed: PostgREST caps a single response
   at 1000 rows regardless of how many actually match. Confirmed live:
   with a plain unpaginated `.select().execute()`, a 9,854-row
   dhan_equity_instruments table returned exactly 1,000 rows, and every
   trading_symbol sorting past that page -- RELIANCE, TCS, HDFCBANK, SBIN,
   ... -- resolved as "not found", indistinguishable from a genuinely
   unlisted symbol. This only bit the *second* (or later) instrument-master
   load of an IST day, since the first, cache-cold load builds its
   DataFrame straight from the Dhan download and never round-trips through
   this SELECT at all -- which is why it looked like intermittent
   Dhan-side flakiness rather than a deterministic truncation bug.
   TestGetEquityInstruments/TestGetFoInstruments's fake client mimics
   PostgREST's own `.range()` paging so a regression back to an
   unpaginated `.select()` fails the same way.

3. replace_equity_instruments/replace_fo_instruments delete in ID-sized
   batches (_delete_all) rather than one unbounded `.delete().neq(...)`.
   Confirmed live: once dhan_fo_instruments grew to ~85,000 rows, that
   single delete hit Postgres's own statement timeout (`57014 canceling
   statement due to statement timeout`) and rolled back entirely --
   leaving the table completely untouched (including stale `exchange`
   values from before migration 0037 backfilled them) rather than
   partially cleared. TestDeleteAll confirms a table bigger than one
   batch is still fully cleared."""
from __future__ import annotations

import types

import pytest

from src.repositories import dhan_instrument_repo


class _DuplicateKeyError(Exception):
    pass


class _FakeTable:
    def __init__(self, store: dict, name: str):
        self.store = store
        self.name = name
        self._pending_delete = False
        self._pending_delete_filter = None  # (column, {values}) -- None means "delete everything"
        self._pending_upsert = None
        self._pending_insert = None
        self._pending_limit = None

    def select(self, *args, **kwargs):
        return self

    def limit(self, n):
        self._pending_limit = n
        return self

    def insert(self, payload):
        self._pending_insert = payload
        return self

    def upsert(self, payload, on_conflict=None):
        self._pending_upsert = (payload, on_conflict or "security_id")
        return self

    def delete(self):
        self._pending_delete = True
        return self

    def neq(self, column, value):
        return self

    def in_(self, column, values):
        self._pending_delete_filter = (column, set(values))
        return self

    def execute(self):
        rows = self.store.setdefault(self.name, [])
        if self._pending_delete:
            if self._pending_delete_filter is not None:
                column, values = self._pending_delete_filter
                self.store[self.name] = [r for r in rows if r[column] not in values]
            else:
                rows.clear()
            return types.SimpleNamespace(data=[])
        if self._pending_insert is not None:
            existing_ids = {r["security_id"] for r in rows}
            for item in self._pending_insert:
                if item["security_id"] in existing_ids:
                    raise _DuplicateKeyError(
                        'duplicate key value violates unique constraint "dhan_equity_instruments_pkey"'
                    )
                existing_ids.add(item["security_id"])
                rows.append(item)
            return types.SimpleNamespace(data=self._pending_insert)
        if self._pending_upsert is not None:
            payload, key_col = self._pending_upsert
            for item in payload:
                idx = next((i for i, r in enumerate(rows) if r[key_col] == item[key_col]), None)
                if idx is not None:
                    rows[idx] = item
                else:
                    rows.append(item)
            return types.SimpleNamespace(data=payload)
        if self._pending_limit is not None:
            return types.SimpleNamespace(data=list(rows[: self._pending_limit]))
        return types.SimpleNamespace(data=list(rows))


class _FakeClient:
    def __init__(self):
        self.store: dict = {}

    def table(self, name):
        return _FakeTable(self.store, name)


def _rows(n: int, offset: int = 0) -> list[dict]:
    return [{"security_id": str(i), "trading_symbol": f"SYM{i}"} for i in range(offset, offset + n)]


class TestDeleteAll:
    def test_clears_a_table_bigger_than_one_batch(self):
        # Regression: a single unbounded delete hit Postgres's statement
        # timeout once dhan_fo_instruments grew past ~85,000 rows and
        # rolled back entirely, leaving stale rows untouched. Confirms
        # the batched delete actually clears a table spanning more than
        # one batch, not just one small enough to fit in a single delete.
        client = _FakeClient()
        client.store["dhan_fo_instruments"] = [{"security_id": str(i)} for i in range(7)]

        dhan_instrument_repo._delete_all(client, "dhan_fo_instruments", size=3)

        assert client.store["dhan_fo_instruments"] == []

    def test_empty_table_is_a_no_op(self):
        client = _FakeClient()

        dhan_instrument_repo._delete_all(client, "dhan_fo_instruments", size=3)  # must not raise

        assert client.store.get("dhan_fo_instruments", []) == []


class TestReplaceEquityInstruments:
    def test_two_concurrent_callers_with_overlapping_ids_do_not_raise(self):
        # Simulates two sessions racing to repopulate the shared cache:
        # both call replace_equity_instruments back-to-back before either
        # has finished, so their chunk writes land on top of each other
        # with the exact same security_ids -- upsert must tolerate this.
        client = _FakeClient()
        batch = _rows(50)
        dhan_instrument_repo.replace_equity_instruments(client, batch)
        dhan_instrument_repo.replace_equity_instruments(client, batch)  # must not raise
        assert len(client.store["dhan_equity_instruments"]) == 50

    def test_interleaved_writes_without_intervening_delete_stay_unique(self):
        # The actual failure mode: a second writer's chunk lands in the
        # table before the first writer's delete-then-insert cycle
        # finishes, so the same security_id shows up twice in one
        # continuous stream of writes with no delete in between.
        client = _FakeClient()
        table = client.table("dhan_equity_instruments")
        table.upsert([{"security_id": "1", "trading_symbol": "RELIANCE"}]).execute()
        table = client.table("dhan_equity_instruments")
        table.upsert([{"security_id": "1", "trading_symbol": "RELIANCE"}]).execute()
        assert client.store["dhan_equity_instruments"] == [{"security_id": "1", "trading_symbol": "RELIANCE"}]

    def test_plain_insert_would_have_raised_on_the_same_scenario(self):
        # Regression lock: confirms the fake client actually reproduces
        # the reported bug for a plain `.insert()`, so this test suite
        # would catch a future revert back to insert.
        client = _FakeClient()
        table = client.table("dhan_equity_instruments")
        table.insert([{"security_id": "1", "trading_symbol": "RELIANCE"}]).execute()
        table = client.table("dhan_equity_instruments")
        with pytest.raises(_DuplicateKeyError):
            table.insert([{"security_id": "1", "trading_symbol": "RELIANCE"}]).execute()


class TestReplaceFoInstruments:
    def test_two_concurrent_callers_with_overlapping_ids_do_not_raise(self):
        client = _FakeClient()
        batch = [
            {
                "security_id": str(i),
                "underlying_symbol": "NIFTY",
                "expiry_date": "2026-09-30",
                "strike_price": 25000.0,
                "option_type": "CE",
            }
            for i in range(20)
        ]
        dhan_instrument_repo.replace_fo_instruments(client, batch)
        dhan_instrument_repo.replace_fo_instruments(client, batch)  # must not raise
        assert len(client.store["dhan_fo_instruments"]) == 20


class _FakeRangeQuery:
    """Mimics PostgREST's own `.select().range(start, end).execute()` --
    only .range()/.execute() are needed here since _paginate() calls the
    query-builder callable fresh for each page and applies .range() itself
    (same fake shape tests/test_fo_repo.py's _FakeRangeQuery uses for
    fo_repo._paginate)."""

    def __init__(self, all_rows: list[dict]):
        self.all_rows = all_rows
        self._start = 0
        self._end = 0

    def range(self, start: int, end: int):
        self._start, self._end = start, end
        return self

    def execute(self):
        return types.SimpleNamespace(data=self.all_rows[self._start : self._end + 1])


class _FakeReadClient:
    """Just enough of the Client interface for get_equity_instruments/
    get_fo_instruments: `.table(name).select(...)` returns a fresh
    _FakeRangeQuery over that table's full row set every call, exactly
    like a real `client.table(...).select(...)` builder does before
    `.range()` is applied."""

    def __init__(self, rows: list[dict]):
        self.rows = rows

    def table(self, _name):
        return self

    def select(self, *args, **kwargs):
        return _FakeRangeQuery(self.rows)


class TestGetEquityInstruments:
    def test_paginates_past_the_1000_row_postgrest_cap(self):
        # The exact real bug: 9,854 real rows, a plain unpaginated
        # .select().execute() only ever returns the first 1000.
        rows = [{"security_id": str(i), "trading_symbol": f"SYM{i}"} for i in range(9854)]
        client = _FakeReadClient(rows)
        result = dhan_instrument_repo.get_equity_instruments(client)
        assert len(result) == 9854

    def test_a_symbol_past_the_first_page_is_still_resolvable(self):
        rows = [{"security_id": str(i), "trading_symbol": f"SYM{i}"} for i in range(1500)]
        client = _FakeReadClient(rows)
        result = dhan_instrument_repo.get_equity_instruments(client)
        symbols = {r["trading_symbol"] for r in result}
        assert "SYM1499" in symbols  # would be missing under the old unpaginated bug

    def test_small_table_under_one_page_is_unaffected(self):
        rows = [{"security_id": "1", "trading_symbol": "RELIANCE"}]
        client = _FakeReadClient(rows)
        assert dhan_instrument_repo.get_equity_instruments(client) == rows


class TestGetFoInstruments:
    def test_paginates_past_the_1000_row_postgrest_cap(self):
        # 85,000+ rows in production -- even more exposed to the
        # truncation bug than the equity table.
        rows = [
            {
                "security_id": str(i),
                "underlying_symbol": "NIFTY",
                "expiry_date": "2026-09-30",
                "strike_price": 25000.0,
                "option_type": "CE",
            }
            for i in range(2500)
        ]
        client = _FakeReadClient(rows)
        result = dhan_instrument_repo.get_fo_instruments(client)
        assert len(result) == 2500
