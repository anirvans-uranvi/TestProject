"""Tests for snapshot_repo's bulk-fetch helpers."""
import types
from datetime import date

from src.repositories import snapshot_repo


class _FakeSnapshotTable:
    """Mimics .select().in_(...).order(...).execute() -- just enough for
    get_latest_returns_and_pe, which doesn't paginate. `.in_()` actually
    filters (unlike `.order()`, a no-op here) so tests can confirm the
    symbol filter works, not just that the call doesn't crash."""

    def __init__(self, rows: list[dict]):
        self.rows = rows

    def select(self, *args, **kwargs):
        return self

    def in_(self, column: str, values: list[str]):
        wanted = set(values)
        self.rows = [r for r in self.rows if r.get(column) in wanted]
        return self

    def order(self, *args, **kwargs):
        return self

    def execute(self):
        return types.SimpleNamespace(data=self.rows)


class _FakeSnapshotClient:
    def __init__(self, rows: list[dict]):
        self.rows = rows

    def table(self, name):
        return _FakeSnapshotTable(list(self.rows))


class TestGetLatestReturnsAndPe:
    def test_empty_symbols_returns_empty_dict_without_querying(self):
        client = _FakeSnapshotClient([{"symbol": "NIFTYBEES", "return_1d": 1.0}])
        assert snapshot_repo.get_latest_returns_and_pe(client, []) == {}

    def test_returns_fields_from_the_single_most_recent_row_per_symbol(self):
        # Rows must already come back newest-first from the query (mirrors
        # the real .order("snapshot_date", desc=True)) -- this fake doesn't
        # re-sort, it just proves the "first row wins per symbol" dedup.
        client = _FakeSnapshotClient(
            [
                {
                    "symbol": "NIFTYBEES",
                    "snapshot_date": "2026-08-12",
                    "return_1d": 0.5,
                    "return_5d": 1.2,
                    "return_20d": 3.4,
                    "pe_ratio": None,
                },
                {
                    "symbol": "NIFTYBEES",
                    "snapshot_date": "2026-08-11",
                    "return_1d": 9.9,
                    "return_5d": 9.9,
                    "return_20d": 9.9,
                    "pe_ratio": 22.1,
                },
            ]
        )
        result = snapshot_repo.get_latest_returns_and_pe(client, ["NIFTYBEES"])
        assert result == {
            "NIFTYBEES": {"return_1d": 0.5, "return_5d": 1.2, "return_20d": 3.4, "pe_ratio": None}
        }

    def test_does_not_carry_forward_a_null_field_from_an_older_row(self):
        # Real behavior to lock in: unlike fundamentals_repo's carry-forward,
        # this takes the latest row's fields as-is -- a null PE in the
        # newest snapshot stays null even if an older snapshot had a value.
        client = _FakeSnapshotClient(
            [
                {"symbol": "GOLDBEES", "snapshot_date": "2026-08-12", "pe_ratio": None},
                {"symbol": "GOLDBEES", "snapshot_date": "2026-08-11", "pe_ratio": 15.0},
            ]
        )
        result = snapshot_repo.get_latest_returns_and_pe(client, ["GOLDBEES"])
        assert result["GOLDBEES"]["pe_ratio"] is None

    def test_filters_to_only_the_requested_symbols(self):
        client = _FakeSnapshotClient(
            [
                {"symbol": "NIFTYBEES", "snapshot_date": "2026-08-12", "pe_ratio": 1.0},
                {"symbol": "GOLDBEES", "snapshot_date": "2026-08-12", "pe_ratio": 2.0},
            ]
        )
        result = snapshot_repo.get_latest_returns_and_pe(client, ["NIFTYBEES"])
        assert set(result.keys()) == {"NIFTYBEES"}

    def test_no_rows_returns_empty_dict(self):
        client = _FakeSnapshotClient([])
        assert snapshot_repo.get_latest_returns_and_pe(client, ["NIFTYBEES"]) == {}


class _FakeLivePricesTable:
    """A persistent-store fake (unlike _FakeSnapshotTable above, which
    hands out a fresh copy per .table() call) -- needed here since
    upsert_user_live_prices deletes then inserts within one call, and the
    tests need that mutation to actually stick for a follow-up
    get_user_live_prices call."""

    def __init__(self, store: list[dict]):
        self.store = store
        self._filters: dict = {}
        self._pending_delete = False

    def select(self, *args, **kwargs):
        return self

    def insert(self, payload):
        self.store.extend(payload)
        return self

    def delete(self):
        self._pending_delete = True
        return self

    def eq(self, column, value):
        self._filters[column] = value
        return self

    def neq(self, column, value):
        self._filters[column] = ("neq", value)
        return self

    def in_(self, column, values):
        self._filters[column] = ("in", set(values))
        return self

    def _matches(self, row) -> bool:
        for column, expected in self._filters.items():
            if isinstance(expected, tuple) and expected[0] == "in":
                if row.get(column) not in expected[1]:
                    return False
            elif isinstance(expected, tuple) and expected[0] == "neq":
                if row.get(column) == expected[1]:
                    return False
            elif row.get(column) != expected:
                return False
        return True

    def execute(self):
        matching = [r for r in self.store if self._matches(r)]
        if self._pending_delete:
            self.store[:] = [r for r in self.store if r not in matching]
        return types.SimpleNamespace(data=matching)


class _FakeLivePricesClient:
    def __init__(self):
        self.store: list[dict] = []

    def table(self, name):
        assert name == "user_live_prices"
        return _FakeLivePricesTable(self.store)


class TestUserLivePrices:
    def test_get_with_no_symbols_returns_empty_dict_without_querying(self):
        client = _FakeLivePricesClient()
        assert snapshot_repo.get_user_live_prices(client, "u1", []) == {}

    def test_upsert_then_get_round_trips(self):
        client = _FakeLivePricesClient()
        snapshot_repo.upsert_user_live_prices(client, "u1", {"JIOFIN": 243.6, "SBIN": 811.9})

        result = snapshot_repo.get_user_live_prices(client, "u1", ["JIOFIN", "SBIN"])

        assert result == {"JIOFIN": 243.6, "SBIN": 811.9}

    def test_get_only_returns_the_requested_users_rows(self):
        client = _FakeLivePricesClient()
        snapshot_repo.upsert_user_live_prices(client, "u1", {"JIOFIN": 243.6})
        snapshot_repo.upsert_user_live_prices(client, "u2", {"JIOFIN": 999.0})

        assert snapshot_repo.get_user_live_prices(client, "u1", ["JIOFIN"]) == {"JIOFIN": 243.6}

    def test_upsert_replaces_only_the_given_symbols_leaving_others_untouched(self):
        client = _FakeLivePricesClient()
        snapshot_repo.upsert_user_live_prices(client, "u1", {"JIOFIN": 243.6, "SBIN": 800.0})

        snapshot_repo.upsert_user_live_prices(client, "u1", {"JIOFIN": 245.0})

        result = snapshot_repo.get_user_live_prices(client, "u1", ["JIOFIN", "SBIN"])
        assert result == {"JIOFIN": 245.0, "SBIN": 800.0}
        assert len(client.store) == 2  # no stale duplicate left behind for JIOFIN

    def test_upsert_with_no_prices_is_a_no_op(self):
        client = _FakeLivePricesClient()
        snapshot_repo.upsert_user_live_prices(client, "u1", {})
        assert client.store == []

    def test_equity_rows_are_invisible_to_the_fo_reader(self):
        # option_type='EQ' rows and F&O rows share one table (migration
        # 0032) -- confirms the two read paths never cross-contaminate.
        client = _FakeLivePricesClient()
        snapshot_repo.upsert_user_live_prices(client, "u1", {"RELIANCE": 2950.0})

        contract = ("RELIANCE", date(2026, 8, 27), 3000.0, "CE")
        assert snapshot_repo.get_user_live_fo_prices(client, "u1", [contract]) == {}


class TestUserLiveFoPrices:
    def test_upsert_then_get_round_trips(self):
        client = _FakeLivePricesClient()
        future = ("RELIANCE", date(2026, 8, 27), 0.0, "FUT")
        call = ("SBIN", date(2026, 8, 27), 800.0, "CE")
        snapshot_repo.upsert_user_live_fo_prices(client, "u1", {future: 2950.0, call: 12.5})

        result = snapshot_repo.get_user_live_fo_prices(client, "u1", [future, call])

        assert result == {future: 2950.0, call: 12.5}

    def test_get_only_returns_requested_contracts_not_every_fo_row(self):
        client = _FakeLivePricesClient()
        wanted = ("RELIANCE", date(2026, 8, 27), 3000.0, "CE")
        other = ("RELIANCE", date(2026, 8, 27), 2900.0, "PE")
        snapshot_repo.upsert_user_live_fo_prices(client, "u1", {wanted: 45.5, other: 32.0})

        assert snapshot_repo.get_user_live_fo_prices(client, "u1", [wanted]) == {wanted: 45.5}

    def test_fo_rows_are_invisible_to_the_equity_reader(self):
        client = _FakeLivePricesClient()
        contract = ("RELIANCE", date(2026, 8, 27), 3000.0, "CE")
        snapshot_repo.upsert_user_live_fo_prices(client, "u1", {contract: 45.5})

        assert snapshot_repo.get_user_live_prices(client, "u1", ["RELIANCE"]) == {}

    def test_upsert_replaces_only_the_given_contracts(self):
        client = _FakeLivePricesClient()
        put = ("RELIANCE", date(2026, 8, 27), 2900.0, "PE")
        call = ("RELIANCE", date(2026, 8, 27), 3000.0, "CE")
        snapshot_repo.upsert_user_live_fo_prices(client, "u1", {put: 32.0, call: 45.5})

        snapshot_repo.upsert_user_live_fo_prices(client, "u1", {put: 33.0})

        result = snapshot_repo.get_user_live_fo_prices(client, "u1", [put, call])
        assert result == {put: 33.0, call: 45.5}

    def test_get_with_no_contracts_returns_empty_dict_without_querying(self):
        client = _FakeLivePricesClient()
        assert snapshot_repo.get_user_live_fo_prices(client, "u1", []) == {}

    def test_upsert_with_no_prices_is_a_no_op(self):
        client = _FakeLivePricesClient()
        snapshot_repo.upsert_user_live_fo_prices(client, "u1", {})
        assert client.store == []
