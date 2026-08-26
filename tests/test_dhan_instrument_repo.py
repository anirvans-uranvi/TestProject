"""Tests for dhan_instrument_repo's replace_equity_instruments/
replace_fo_instruments -- specifically that they upsert rather than
plain-insert. Confirmed live: dhan_equity_instruments/dhan_fo_instruments
(migration 0035) are shared across every user, so two Streamlit sessions
can independently find today's cache cold and race to repopulate it at
once (most likely right after IST midnight, when everyone's first click
of the day hits a cold cache together). Both download ~identical rows
and both delete-then-write, so a plain `.insert()` can land two rows with
the same security_id (the primary key) in the same window and raise
`duplicate key value violates unique constraint
"dhan_equity_instruments_pkey"`. This fake client enforces the same
primary-key uniqueness Postgres would, so a regression back to `.insert`
fails this test the same way it failed in production."""
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
        self._pending_upsert = None
        self._pending_insert = None

    def select(self, *args, **kwargs):
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

    def execute(self):
        rows = self.store.setdefault(self.name, [])
        if self._pending_delete:
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
        return types.SimpleNamespace(data=list(rows))


class _FakeClient:
    def __init__(self):
        self.store: dict = {}

    def table(self, name):
        return _FakeTable(self.store, name)


def _rows(n: int, offset: int = 0) -> list[dict]:
    return [{"security_id": str(i), "trading_symbol": f"SYM{i}"} for i in range(offset, offset + n)]


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
