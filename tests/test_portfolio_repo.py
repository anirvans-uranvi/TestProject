"""Tests for portfolio_repo's replace-on-upload semantics: re-uploading a
broker's holdings should delete every existing row for that
(user_id, portfolio_name, broker) and insert only the freshly parsed set,
leaving every other broker/portfolio untouched -- including a portfolio
that's never been uploaded to before, which is how a brand-new portfolio
gets created (nothing to delete, just an insert)."""
import types

from src.models.portfolio import PortfolioHolding
from src.repositories import portfolio_repo


class _FakeTable:
    def __init__(self, store, calls, name):
        self.store = store
        self.calls = calls
        self.name = name
        self._pending_delete = False
        self._filters: dict = {}

    def select(self, *args, **kwargs):
        return self

    def insert(self, payload):
        self.calls.append(("insert", self.name, payload))
        self.store.setdefault(self.name, []).extend(payload)
        return self

    def delete(self):
        self._pending_delete = True
        return self

    def eq(self, column, value):
        self._filters[column] = value
        return self

    def execute(self):
        rows = self.store.get(self.name, [])
        matching = [r for r in rows if all(r.get(k) == v for k, v in self._filters.items())]
        if self._pending_delete:
            self.calls.append(("delete", self.name, dict(self._filters)))
            self.store[self.name] = [r for r in rows if r not in matching]
        return types.SimpleNamespace(data=matching)


class _FakeClient:
    def __init__(self):
        self.store: dict = {}
        self.calls: list = []

    def table(self, name):
        return _FakeTable(self.store, self.calls, name)


def _row(portfolio_name, broker, raw_name, symbol="SYM"):
    return {
        "user_id": "u1",
        "portfolio_name": portfolio_name,
        "broker": broker,
        "raw_name": raw_name,
        "symbol": symbol,
        "qty": 1,
        "avg_price": 1,
        "investment": 1,
        "uploaded_at": None,
    }


class TestReplaceBrokerHoldings:
    def test_deletes_only_the_target_portfolio_and_brokers_rows(self):
        client = _FakeClient()
        client.store["portfolio_holdings"] = [
            _row("Portfolio 1", "Zerodha", "OLD"),
            _row("Portfolio 1", "Dhan", "KEEP_OTHER_BROKER"),
            _row("Portfolio 2", "Zerodha", "KEEP_OTHER_PORTFOLIO"),
        ]
        holdings = [
            PortfolioHolding(
                user_id="u1", portfolio_name="Portfolio 1", broker="Zerodha",
                raw_name="SBIN", symbol="SBIN", qty=10, avg_price=900, investment=9000,
            ),
        ]

        portfolio_repo.replace_broker_holdings(client, "u1", "Portfolio 1", "Zerodha", holdings)

        assert (
            "delete",
            "portfolio_holdings",
            {"user_id": "u1", "portfolio_name": "Portfolio 1", "broker": "Zerodha"},
        ) in client.calls
        remaining = client.store["portfolio_holdings"]
        assert not any(r["raw_name"] == "OLD" for r in remaining)
        assert any(r["raw_name"] == "KEEP_OTHER_BROKER" and r["portfolio_name"] == "Portfolio 1" for r in remaining)
        assert any(r["raw_name"] == "KEEP_OTHER_PORTFOLIO" and r["portfolio_name"] == "Portfolio 2" for r in remaining)
        assert any(r["raw_name"] == "SBIN" and r["symbol"] == "SBIN" for r in remaining)

    def test_a_never_before_seen_portfolio_name_just_inserts_with_nothing_to_delete(self):
        # This is how "New portfolio" creates a portfolio: no existing
        # rows match (user_id, portfolio_name, broker), so the delete is
        # a no-op and only the insert has any effect.
        client = _FakeClient()
        client.store["portfolio_holdings"] = [_row("Portfolio 1", "Zerodha", "EXISTING")]
        holdings = [
            PortfolioHolding(
                user_id="u1", portfolio_name="Brand New Portfolio", broker="Dhan",
                raw_name="Coal India", symbol="COALINDIA", qty=5, avg_price=400, investment=2000,
            ),
        ]

        portfolio_repo.replace_broker_holdings(client, "u1", "Brand New Portfolio", "Dhan", holdings)

        remaining = client.store["portfolio_holdings"]
        assert any(r["raw_name"] == "EXISTING" for r in remaining)
        assert any(r["raw_name"] == "Coal India" and r["portfolio_name"] == "Brand New Portfolio" for r in remaining)

    def test_empty_holdings_deletes_existing_rows_and_inserts_nothing(self):
        client = _FakeClient()
        client.store["portfolio_holdings"] = [_row("Portfolio 1", "Zerodha", "OLD")]

        portfolio_repo.replace_broker_holdings(client, "u1", "Portfolio 1", "Zerodha", [])

        assert client.store["portfolio_holdings"] == []
        assert not any(call[0] == "insert" for call in client.calls)

    def test_insert_payload_omits_uploaded_at_so_the_db_default_applies(self):
        client = _FakeClient()
        holdings = [
            PortfolioHolding(
                user_id="u1", portfolio_name="Portfolio 1", broker="Zerodha",
                raw_name="SBIN", symbol="SBIN", qty=10, avg_price=900, investment=9000,
            ),
        ]

        portfolio_repo.replace_broker_holdings(client, "u1", "Portfolio 1", "Zerodha", holdings)

        insert_calls = [c for c in client.calls if c[0] == "insert"]
        assert len(insert_calls) == 1
        assert "uploaded_at" not in insert_calls[0][2][0]


class TestDeletePortfolio:
    def test_deletes_every_broker_within_the_named_portfolio_only(self):
        client = _FakeClient()
        client.store["portfolio_holdings"] = [
            _row("Portfolio 1", "Zerodha", "SBIN"),
            _row("Portfolio 1", "Dhan", "COALINDIA"),
            _row("Portfolio 2", "Zerodha", "KEEP"),
        ]

        portfolio_repo.delete_portfolio(client, "u1", "Portfolio 1")

        assert (
            "delete",
            "portfolio_holdings",
            {"user_id": "u1", "portfolio_name": "Portfolio 1"},
        ) in client.calls
        remaining = client.store["portfolio_holdings"]
        assert len(remaining) == 1
        assert remaining[0]["raw_name"] == "KEEP"
        assert remaining[0]["portfolio_name"] == "Portfolio 2"

    def test_does_not_touch_other_users_portfolio_of_the_same_name(self):
        client = _FakeClient()
        client.store["portfolio_holdings"] = [
            _row("Portfolio 1", "Zerodha", "MINE"),
            {**_row("Portfolio 1", "Zerodha", "OTHER_USER"), "user_id": "u2"},
        ]

        portfolio_repo.delete_portfolio(client, "u1", "Portfolio 1")

        remaining = client.store["portfolio_holdings"]
        assert len(remaining) == 1
        assert remaining[0]["raw_name"] == "OTHER_USER"


class TestListHoldings:
    def test_returns_only_the_requested_users_rows_as_models(self):
        client = _FakeClient()
        client.store["portfolio_holdings"] = [
            _row("Portfolio 1", "Zerodha", "SBIN", symbol="SBIN"),
            {**_row("Portfolio 1", "Dhan", "OTHER", symbol="OTHER"), "user_id": "u2"},
        ]

        result = portfolio_repo.list_holdings(client, "u1")

        assert len(result) == 1
        assert result[0].symbol == "SBIN"
        assert result[0].raw_name == "SBIN"

    def test_holdings_across_multiple_portfolios_all_come_back(self):
        client = _FakeClient()
        client.store["portfolio_holdings"] = [
            _row("Portfolio 1", "Zerodha", "SBIN"),
            _row("Portfolio 2", "Dhan", "COALINDIA"),
        ]

        result = portfolio_repo.list_holdings(client, "u1")

        assert {h.portfolio_name for h in result} == {"Portfolio 1", "Portfolio 2"}
