"""Tests for portfolio_repo's replace-on-upload semantics: re-uploading a
broker's holdings should delete every existing row for that
(user_id, portfolio_name, broker) and insert only the freshly parsed set,
leaving every other broker/portfolio untouched -- including a portfolio
that's never been uploaded to before, which is how a brand-new portfolio
gets created (nothing to delete, just an insert)."""
import types
from datetime import date

from src.models.enums import OptionType
from src.models.portfolio import (
    BrokerConnection,
    PortfolioHolding,
    PortfolioPosition,
    PortfolioPositionMeta,
    PortfolioTradeGroup,
    PortfolioTradeMeta,
)
from src.repositories import portfolio_repo


class _FakeTable:
    def __init__(self, store, calls, name):
        self.store = store
        self.calls = calls
        self.name = name
        self._pending_delete = False
        self._pending_upsert = None
        self._filters: dict = {}

    def select(self, *args, **kwargs):
        return self

    def insert(self, payload):
        self.calls.append(("insert", self.name, payload))
        self.store.setdefault(self.name, []).extend(payload)
        return self

    def upsert(self, payload, on_conflict=None):
        self.calls.append(("upsert", self.name, payload))
        self._pending_upsert = (payload, on_conflict)
        return self

    def delete(self):
        self._pending_delete = True
        return self

    def eq(self, column, value):
        self._filters[column] = value
        return self

    def limit(self, _n):
        return self

    def execute(self):
        if self._pending_upsert is not None:
            payload, on_conflict = self._pending_upsert
            rows = self.store.setdefault(self.name, [])
            key_cols = [c.strip() for c in (on_conflict or "").split(",") if c.strip()]
            for item in payload:
                existing_idx = next(
                    (i for i, r in enumerate(rows) if key_cols and all(r.get(k) == item.get(k) for k in key_cols)),
                    None,
                )
                if existing_idx is not None:
                    rows[existing_idx] = item
                else:
                    rows.append(item)
            return types.SimpleNamespace(data=payload)

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


def _position_row(portfolio_name, broker, raw_name, symbol="NIFTY"):
    return {
        "user_id": "u1",
        "portfolio_name": portfolio_name,
        "broker": broker,
        "raw_name": raw_name,
        "symbol": symbol,
        "expiry_date": None,
        "strike_price": None,
        "option_type": None,
        "qty": -100,
        "avg_price": 10,
        "ltp": 5,
        "uploaded_at": None,
    }


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

    def test_also_deletes_positions_within_the_named_portfolio_only(self):
        client = _FakeClient()
        client.store["portfolio_holdings"] = [_row("Portfolio 1", "Zerodha", "SBIN")]
        client.store["portfolio_positions"] = [
            _position_row("Portfolio 1", "Zerodha", "NIFTY2681123000PE"),
            _position_row("Portfolio 2", "Zerodha", "KEEP"),
        ]

        portfolio_repo.delete_portfolio(client, "u1", "Portfolio 1")

        assert (
            "delete",
            "portfolio_positions",
            {"user_id": "u1", "portfolio_name": "Portfolio 1"},
        ) in client.calls
        remaining = client.store["portfolio_positions"]
        assert len(remaining) == 1
        assert remaining[0]["raw_name"] == "KEEP"

    def test_also_deletes_broker_connections_within_the_named_portfolio_only(self):
        client = _FakeClient()
        client.store["broker_connections"] = [
            _connection_row("Portfolio 1", "Dhan"),
            _connection_row("Portfolio 2", "Dhan"),
        ]

        portfolio_repo.delete_portfolio(client, "u1", "Portfolio 1")

        remaining = client.store["broker_connections"]
        assert len(remaining) == 1
        assert remaining[0]["portfolio_name"] == "Portfolio 2"

    def test_also_deletes_trade_groups_within_the_named_portfolio_only(self):
        client = _FakeClient()
        client.store["portfolio_holdings"] = [_row("Portfolio 1", "Zerodha", "SBIN")]
        client.store["portfolio_trade_groups"] = [
            _trade_group_row("Portfolio 1", "Zerodha", "NIFTY2681123000PE", "NIFTY"),
            _trade_group_row("Portfolio 2", "Zerodha", "KEEP", "NIFTY"),
        ]

        portfolio_repo.delete_portfolio(client, "u1", "Portfolio 1")

        remaining = client.store["portfolio_trade_groups"]
        assert len(remaining) == 1
        assert remaining[0]["raw_name"] == "KEEP"

    def test_also_deletes_trade_meta_within_the_named_portfolio_only(self):
        client = _FakeClient()
        client.store["portfolio_holdings"] = [_row("Portfolio 1", "Zerodha", "SBIN")]
        client.store["portfolio_trade_meta"] = [
            _trade_meta_row("Portfolio 1", "SBIN"),
            _trade_meta_row("Portfolio 2", "KEEP"),
        ]

        portfolio_repo.delete_portfolio(client, "u1", "Portfolio 1")

        remaining = client.store["portfolio_trade_meta"]
        assert len(remaining) == 1
        assert remaining[0]["trade_id"] == "KEEP"

    def test_also_deletes_position_meta_within_the_named_portfolio_only(self):
        client = _FakeClient()
        client.store["portfolio_holdings"] = [_row("Portfolio 1", "Zerodha", "SBIN")]
        client.store["portfolio_position_meta"] = [
            _position_meta_row("Portfolio 1", "Zerodha", "SBIN"),
            _position_meta_row("Portfolio 2", "Zerodha", "KEEP"),
        ]

        portfolio_repo.delete_portfolio(client, "u1", "Portfolio 1")

        remaining = client.store["portfolio_position_meta"]
        assert len(remaining) == 1
        assert remaining[0]["raw_name"] == "KEEP"

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


class TestListPortfolioSymbols:
    def test_returns_distinct_resolved_symbols_for_the_requested_user_only(self):
        client = _FakeClient()
        client.store["portfolio_holdings"] = [
            _row("Portfolio 1", "Zerodha", "SBIN", symbol="SBIN"),
            _row("Portfolio 2", "Dhan", "Hindustan Zinc", symbol="HINDZINC"),
            {**_row("Portfolio 1", "Zerodha", "OTHER", symbol="OTHER"), "user_id": "u2"},
        ]

        result = portfolio_repo.list_portfolio_symbols(client, "u1")

        assert result == ["HINDZINC", "SBIN"]

    def test_same_symbol_across_brokers_or_portfolios_is_not_duplicated(self):
        client = _FakeClient()
        client.store["portfolio_holdings"] = [
            _row("Portfolio 1", "Zerodha", "SBIN", symbol="SBIN"),
            _row("Portfolio 2", "Dhan", "SBIN", symbol="SBIN"),
        ]

        result = portfolio_repo.list_portfolio_symbols(client, "u1")

        assert result == ["SBIN"]

    def test_unresolved_rows_with_no_symbol_are_excluded(self):
        client = _FakeClient()
        client.store["portfolio_holdings"] = [
            _row("Portfolio 1", "Dhan", "Some Unmatched Fund", symbol=None),
        ]

        result = portfolio_repo.list_portfolio_symbols(client, "u1")

        assert result == []


class TestReplaceBrokerPositions:
    def test_deletes_only_the_target_portfolio_and_brokers_rows(self):
        client = _FakeClient()
        client.store["portfolio_positions"] = [
            _position_row("Portfolio 1", "Zerodha", "OLD"),
            _position_row("Portfolio 1", "Dhan", "KEEP_OTHER_BROKER"),
            _position_row("Portfolio 2", "Zerodha", "KEEP_OTHER_PORTFOLIO"),
        ]
        positions = [
            PortfolioPosition(
                user_id="u1", portfolio_name="Portfolio 1", broker="Zerodha",
                raw_name="NIFTY2681123000PE", symbol="NIFTY", expiry_date=date(2026, 8, 11),
                strike_price=23000, option_type=OptionType.PE, qty=-780, avg_price=10.15, ltp=1.1,
            ),
        ]

        portfolio_repo.replace_broker_positions(client, "u1", "Portfolio 1", "Zerodha", positions)

        assert (
            "delete",
            "portfolio_positions",
            {"user_id": "u1", "portfolio_name": "Portfolio 1", "broker": "Zerodha"},
        ) in client.calls
        remaining = client.store["portfolio_positions"]
        assert not any(r["raw_name"] == "OLD" for r in remaining)
        assert any(r["raw_name"] == "KEEP_OTHER_BROKER" for r in remaining)
        assert any(r["raw_name"] == "KEEP_OTHER_PORTFOLIO" for r in remaining)
        assert any(r["raw_name"] == "NIFTY2681123000PE" and r["symbol"] == "NIFTY" for r in remaining)

    def test_empty_positions_deletes_existing_rows_and_inserts_nothing(self):
        client = _FakeClient()
        client.store["portfolio_positions"] = [_position_row("Portfolio 1", "Zerodha", "OLD")]

        portfolio_repo.replace_broker_positions(client, "u1", "Portfolio 1", "Zerodha", [])

        assert client.store["portfolio_positions"] == []
        assert not any(call[0] == "insert" for call in client.calls)

    def test_insert_payload_omits_uploaded_at_so_the_db_default_applies(self):
        client = _FakeClient()
        positions = [
            PortfolioPosition(
                user_id="u1", portfolio_name="Portfolio 1", broker="Zerodha",
                raw_name="NIFTY2681123000PE", symbol="NIFTY", expiry_date=date(2026, 8, 11),
                strike_price=23000, option_type=OptionType.PE, qty=-780, avg_price=10.15, ltp=1.1,
            ),
        ]

        portfolio_repo.replace_broker_positions(client, "u1", "Portfolio 1", "Zerodha", positions)

        insert_calls = [c for c in client.calls if c[0] == "insert"]
        assert len(insert_calls) == 1
        assert "uploaded_at" not in insert_calls[0][2][0]


class TestListPositions:
    def test_returns_only_the_requested_users_rows_as_models(self):
        client = _FakeClient()
        client.store["portfolio_positions"] = [
            _position_row("Portfolio 1", "Zerodha", "NIFTY2681123000PE", symbol="NIFTY"),
            {**_position_row("Portfolio 1", "Dhan", "OTHER", symbol="OTHER"), "user_id": "u2"},
        ]

        result = portfolio_repo.list_positions(client, "u1")

        assert len(result) == 1
        assert result[0].symbol == "NIFTY"
        assert result[0].raw_name == "NIFTY2681123000PE"

    def test_positions_across_multiple_portfolios_all_come_back(self):
        client = _FakeClient()
        client.store["portfolio_positions"] = [
            _position_row("Portfolio 1", "Zerodha", "A"),
            _position_row("Portfolio 2", "Dhan", "B"),
        ]

        result = portfolio_repo.list_positions(client, "u1")

        assert {p.portfolio_name for p in result} == {"Portfolio 1", "Portfolio 2"}


def _connection_row(portfolio_name, broker="Dhan", client_id="CID1234", token="TOKEN1", user_id="u1"):
    return {
        "user_id": user_id,
        "portfolio_name": portfolio_name,
        "broker": broker,
        "client_id": client_id,
        "access_token": token,
        "token_saved_at": None,
    }


class TestBrokerConnections:
    def test_get_returns_none_when_not_connected(self):
        client = _FakeClient()

        assert portfolio_repo.get_broker_connection(client, "u1", "Portfolio 1", "Dhan") is None

    def test_upsert_then_get_round_trips(self):
        client = _FakeClient()
        connection = BrokerConnection(
            user_id="u1", portfolio_name="Portfolio 1", broker="Dhan", client_id="CID1234", access_token="TOKEN1",
        )

        portfolio_repo.upsert_broker_connection(client, connection)
        result = portfolio_repo.get_broker_connection(client, "u1", "Portfolio 1", "Dhan")

        assert result is not None
        assert result.client_id == "CID1234"
        assert result.access_token == "TOKEN1"

    def test_upsert_twice_replaces_rather_than_duplicates(self):
        client = _FakeClient()
        first = BrokerConnection(
            user_id="u1", portfolio_name="Portfolio 1", broker="Dhan", client_id="OLD", access_token="OLDTOKEN"
        )
        second = BrokerConnection(
            user_id="u1", portfolio_name="Portfolio 1", broker="Dhan", client_id="NEW", access_token="NEWTOKEN"
        )

        portfolio_repo.upsert_broker_connection(client, first)
        portfolio_repo.upsert_broker_connection(client, second)

        assert len(client.store["broker_connections"]) == 1
        result = portfolio_repo.get_broker_connection(client, "u1", "Portfolio 1", "Dhan")
        assert result.client_id == "NEW"

    def test_delete_removes_only_the_targeted_broker_within_the_portfolio(self):
        client = _FakeClient()
        client.store["broker_connections"] = [
            _connection_row("Portfolio 1", "Dhan"),
            _connection_row("Portfolio 1", "OtherBroker"),
            _connection_row("Portfolio 2", "Dhan"),
        ]

        portfolio_repo.delete_broker_connection(client, "u1", "Portfolio 1", "Dhan")

        remaining = client.store["broker_connections"]
        assert len(remaining) == 2
        assert portfolio_repo.get_broker_connection(client, "u1", "Portfolio 1", "Dhan") is None


def _trade_group_row(portfolio_name, broker, raw_name, trade_id, user_id="u1"):
    return {
        "user_id": user_id,
        "portfolio_name": portfolio_name,
        "broker": broker,
        "raw_name": raw_name,
        "trade_id": trade_id,
        "updated_at": None,
    }


class TestTradeGroups:
    def test_list_returns_only_the_requested_users_rows_as_models(self):
        client = _FakeClient()
        client.store["portfolio_trade_groups"] = [
            _trade_group_row("Portfolio 1", "Zerodha", "NIFTY2681123000PE", "NIFTY Spread"),
            {**_trade_group_row("Portfolio 1", "Zerodha", "OTHER", "X"), "user_id": "u2"},
        ]

        result = portfolio_repo.list_trade_groups(client, "u1")

        assert len(result) == 1
        assert isinstance(result[0], PortfolioTradeGroup)
        assert result[0].trade_id == "NIFTY Spread"

    def test_set_trade_group_upserts_one_row_per_leg(self):
        client = _FakeClient()

        portfolio_repo.set_trade_group(
            client, "u1", "Portfolio 1", [("Zerodha", "LEG_A"), ("Zerodha", "LEG_B")], "Combined Trade"
        )

        rows = client.store["portfolio_trade_groups"]
        assert len(rows) == 2
        assert {r["raw_name"] for r in rows} == {"LEG_A", "LEG_B"}
        assert all(r["trade_id"] == "Combined Trade" for r in rows)

    def test_set_trade_group_reassigns_an_already_grouped_leg(self):
        client = _FakeClient()
        client.store["portfolio_trade_groups"] = [_trade_group_row("Portfolio 1", "Zerodha", "LEG_A", "Old Trade")]

        portfolio_repo.set_trade_group(client, "u1", "Portfolio 1", [("Zerodha", "LEG_A")], "New Trade")

        rows = client.store["portfolio_trade_groups"]
        assert len(rows) == 1
        assert rows[0]["trade_id"] == "New Trade"

    def test_set_trade_group_with_no_legs_is_a_no_op(self):
        client = _FakeClient()

        portfolio_repo.set_trade_group(client, "u1", "Portfolio 1", [], "Trade")

        assert client.store.get("portfolio_trade_groups", []) == []

    def test_clear_trade_group_overrides_removes_only_the_targeted_legs(self):
        client = _FakeClient()
        client.store["portfolio_trade_groups"] = [
            _trade_group_row("Portfolio 1", "Zerodha", "LEG_A", "Combined Trade"),
            _trade_group_row("Portfolio 1", "Zerodha", "LEG_B", "Combined Trade"),
            _trade_group_row("Portfolio 1", "Dhan", "LEG_A", "Unrelated Trade"),
        ]

        portfolio_repo.clear_trade_group_overrides(client, "u1", "Portfolio 1", [("Zerodha", "LEG_A")])

        remaining = client.store["portfolio_trade_groups"]
        assert len(remaining) == 2
        assert not any(r["broker"] == "Zerodha" and r["raw_name"] == "LEG_A" for r in remaining)
        assert any(r["broker"] == "Dhan" and r["raw_name"] == "LEG_A" for r in remaining)


def _trade_meta_row(portfolio_name, trade_id, underlying_label=None, trade_type="Trade", user_id="u1"):
    return {
        "user_id": user_id,
        "portfolio_name": portfolio_name,
        "trade_id": trade_id,
        "underlying_label": underlying_label,
        "trade_type": trade_type,
        "updated_at": None,
    }


class TestTradeMeta:
    def test_list_returns_only_the_requested_users_rows_as_models(self):
        client = _FakeClient()
        client.store["portfolio_trade_meta"] = [
            _trade_meta_row("Portfolio 1", "RELIANCE", underlying_label="Reliance Industries"),
            {**_trade_meta_row("Portfolio 1", "OTHER"), "user_id": "u2"},
        ]

        result = portfolio_repo.list_trade_meta(client, "u1")

        assert len(result) == 1
        assert isinstance(result[0], PortfolioTradeMeta)
        assert result[0].underlying_label == "Reliance Industries"

    def test_set_trade_meta_upserts_a_new_row(self):
        client = _FakeClient()

        portfolio_repo.set_trade_meta(
            client, "u1", "Portfolio 1", "TATAMTRDVR", underlying_label="Tata Motors Passenger Vehicle", trade_type="Long Term Hold"
        )

        rows = client.store["portfolio_trade_meta"]
        assert len(rows) == 1
        assert rows[0]["underlying_label"] == "Tata Motors Passenger Vehicle"
        assert rows[0]["trade_type"] == "Long Term Hold"
        assert rows[0]["bucket_override"] is None

    def test_set_trade_meta_saves_bucket_override(self):
        client = _FakeClient()

        portfolio_repo.set_trade_meta(
            client, "u1", "Portfolio 1", "NIFTYBEES", underlying_label=None, trade_type="Trade", bucket_override="index"
        )

        rows = client.store["portfolio_trade_meta"]
        assert rows[0]["bucket_override"] == "index"

    def test_set_trade_meta_overwrites_the_existing_row_for_the_same_trade(self):
        client = _FakeClient()
        client.store["portfolio_trade_meta"] = [_trade_meta_row("Portfolio 1", "RELIANCE", trade_type="Trade")]

        portfolio_repo.set_trade_meta(
            client, "u1", "Portfolio 1", "RELIANCE", underlying_label=None, trade_type="Hedge"
        )

        rows = client.store["portfolio_trade_meta"]
        assert len(rows) == 1
        assert rows[0]["trade_type"] == "Hedge"


def _position_meta_row(portfolio_name, broker, raw_name, trade_date=None, stop_loss=None, user_id="u1"):
    return {
        "user_id": user_id,
        "portfolio_name": portfolio_name,
        "broker": broker,
        "raw_name": raw_name,
        "trade_date": trade_date,
        "stop_loss": stop_loss,
        "updated_at": None,
    }


class TestPositionMeta:
    def test_list_returns_only_the_requested_users_rows_as_models(self):
        client = _FakeClient()
        client.store["portfolio_position_meta"] = [
            _position_meta_row("Portfolio 1", "Zerodha", "NIFTY26AUG23100PE", trade_date="2026-08-01", stop_loss=-3375.0),
            {**_position_meta_row("Portfolio 1", "Zerodha", "OTHER"), "user_id": "u2"},
        ]

        result = portfolio_repo.list_position_meta(client, "u1")

        assert len(result) == 1
        assert isinstance(result[0], PortfolioPositionMeta)
        assert result[0].trade_date == date(2026, 8, 1)
        assert result[0].stop_loss == -3375.0

    def test_set_position_trade_date_upserts_a_new_row(self):
        client = _FakeClient()

        portfolio_repo.set_position_trade_date(client, "u1", "Portfolio 1", "Zerodha", "NIFTY26AUG23100PE", date(2026, 8, 1))

        rows = client.store["portfolio_position_meta"]
        assert len(rows) == 1
        assert rows[0]["trade_date"] == "2026-08-01"

    def test_set_position_trade_date_none_clears_it(self):
        client = _FakeClient()

        portfolio_repo.set_position_trade_date(client, "u1", "Portfolio 1", "Zerodha", "NIFTY26AUG23100PE", None)

        rows = client.store["portfolio_position_meta"]
        assert rows[0]["trade_date"] is None

    def test_set_position_stop_loss_upserts_a_new_row(self):
        client = _FakeClient()

        portfolio_repo.set_position_stop_loss(client, "u1", "Portfolio 1", "Zerodha", "NIFTY26AUG23100PE", -3375.0)

        rows = client.store["portfolio_position_meta"]
        assert len(rows) == 1
        assert rows[0]["stop_loss"] == -3375.0

    def test_set_position_stop_loss_overwrites_the_existing_value_for_the_same_leg(self):
        client = _FakeClient()
        client.store["portfolio_position_meta"] = [
            _position_meta_row("Portfolio 1", "Zerodha", "NIFTY26AUG23100PE", stop_loss=-3375.0)
        ]

        portfolio_repo.set_position_stop_loss(client, "u1", "Portfolio 1", "Zerodha", "NIFTY26AUG23100PE", 0.0)

        rows = client.store["portfolio_position_meta"]
        assert len(rows) == 1
        assert rows[0]["stop_loss"] == 0.0
