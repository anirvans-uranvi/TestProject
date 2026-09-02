"""Tests for src/utils/data_provider_settings.py's non-UI helpers.
_auto_classify_new_trades is the only function here worth unit testing
directly -- everything else in this module renders Streamlit widgets
(forms, buttons) with no existing test harness in this codebase; see
_sync_dhan's own lack of coverage. portfolio_repo.* calls
are monkeypatched directly (this module imports the package, not
individual functions) rather than built against a real/fake Supabase
client -- same style tests/test_dhan_provider.py's DB-cache tests use.
"""
from __future__ import annotations

import src.utils.data_provider_settings as data_provider_settings
from src.models.enums import OptionType
from src.models.portfolio import PortfolioHolding, PortfolioPosition, PortfolioTradeMeta


def _holding(*, raw_name, symbol, broker="Dhan", qty=100.0):
    return PortfolioHolding(
        user_id="u1", portfolio_name="My Portfolio", broker=broker, raw_name=raw_name,
        symbol=symbol, qty=qty, avg_price=100.0, investment=qty * 100.0,
    )


def _position(*, raw_name, symbol, option_type, qty, broker="Dhan"):
    return PortfolioPosition(
        user_id="u1", portfolio_name="My Portfolio", broker=broker, raw_name=raw_name,
        symbol=symbol, option_type=option_type, qty=qty, avg_price=10.0,
    )


class TestAutoClassifyNewTrades:
    def test_new_csp_shaped_trade_gets_classified_and_saved(self, monkeypatch):
        monkeypatch.setattr(data_provider_settings.portfolio_repo, "list_holdings", lambda client, user_id: [])
        monkeypatch.setattr(
            data_provider_settings.portfolio_repo,
            "list_positions",
            lambda client, user_id: [_position(raw_name="X-PE", symbol="X", option_type=OptionType.PE, qty=-75)],
        )
        monkeypatch.setattr(data_provider_settings.portfolio_repo, "list_trade_groups", lambda client, user_id: [])
        monkeypatch.setattr(data_provider_settings.portfolio_repo, "list_trade_meta", lambda client, user_id: [])
        saved = {}
        monkeypatch.setattr(
            data_provider_settings.portfolio_repo,
            "set_trade_meta",
            lambda client, user_id, portfolio_name, trade_id, *, underlying_label, trade_type: saved.update(
                trade_id=trade_id, trade_type=trade_type
            ),
        )

        data_provider_settings._auto_classify_new_trades(client=object(), user_id="u1", portfolio_name="My Portfolio")

        assert saved == {"trade_id": "X", "trade_type": "CSP"}

    def test_already_classified_trade_is_left_untouched_even_if_shape_no_longer_matches(self, monkeypatch):
        # X has a portfolio_trade_meta row already (user classified it
        # before) -- must never be touched here, even though its current
        # legs (a lone long put) don't match any known strategy at all.
        monkeypatch.setattr(data_provider_settings.portfolio_repo, "list_holdings", lambda client, user_id: [])
        monkeypatch.setattr(
            data_provider_settings.portfolio_repo,
            "list_positions",
            lambda client, user_id: [_position(raw_name="X-PE", symbol="X", option_type=OptionType.PE, qty=75)],
        )
        monkeypatch.setattr(data_provider_settings.portfolio_repo, "list_trade_groups", lambda client, user_id: [])
        monkeypatch.setattr(
            data_provider_settings.portfolio_repo,
            "list_trade_meta",
            lambda client, user_id: [
                PortfolioTradeMeta(user_id="u1", portfolio_name="My Portfolio", trade_id="X", trade_type="CSP")
            ],
        )
        calls = []
        monkeypatch.setattr(
            data_provider_settings.portfolio_repo, "set_trade_meta", lambda *a, **k: calls.append((a, k))
        )

        data_provider_settings._auto_classify_new_trades(client=object(), user_id="u1", portfolio_name="My Portfolio")

        assert calls == []

    def test_plain_stock_holding_gets_classified_as_holding(self, monkeypatch):
        # A stock holding with no options/futures at all -- now a
        # recognized shape ("Holding"), not left as the "Trade" default.
        monkeypatch.setattr(
            data_provider_settings.portfolio_repo, "list_holdings", lambda client, user_id: [_holding(raw_name="Y", symbol="Y")]
        )
        monkeypatch.setattr(data_provider_settings.portfolio_repo, "list_positions", lambda client, user_id: [])
        monkeypatch.setattr(data_provider_settings.portfolio_repo, "list_trade_groups", lambda client, user_id: [])
        monkeypatch.setattr(data_provider_settings.portfolio_repo, "list_trade_meta", lambda client, user_id: [])
        saved = {}
        monkeypatch.setattr(
            data_provider_settings.portfolio_repo,
            "set_trade_meta",
            lambda client, user_id, portfolio_name, trade_id, *, underlying_label, trade_type: saved.update(
                trade_id=trade_id, trade_type=trade_type
            ),
        )

        data_provider_settings._auto_classify_new_trades(client=object(), user_id="u1", portfolio_name="My Portfolio")

        assert saved == {"trade_id": "Y", "trade_type": "Holding"}

    def test_unclassifiable_new_trade_writes_nothing(self, monkeypatch):
        # A lone undecoded/futures Position leg (option_type unresolved) --
        # classify_trade_type returns None, so nothing is written (stays
        # default "Trade").
        monkeypatch.setattr(data_provider_settings.portfolio_repo, "list_holdings", lambda client, user_id: [])
        monkeypatch.setattr(
            data_provider_settings.portfolio_repo,
            "list_positions",
            lambda client, user_id: [_position(raw_name="Y-FUT", symbol="Y", option_type=None, qty=-1)],
        )
        monkeypatch.setattr(data_provider_settings.portfolio_repo, "list_trade_groups", lambda client, user_id: [])
        monkeypatch.setattr(data_provider_settings.portfolio_repo, "list_trade_meta", lambda client, user_id: [])
        calls = []
        monkeypatch.setattr(
            data_provider_settings.portfolio_repo, "set_trade_meta", lambda *a, **k: calls.append((a, k))
        )

        data_provider_settings._auto_classify_new_trades(client=object(), user_id="u1", portfolio_name="My Portfolio")

        assert calls == []

    def test_covered_call_spanning_two_brokers_still_classifies_as_one_trade(self, monkeypatch):
        # The holding came in via one broker, the short call via Dhan --
        # both share symbol "Z", so they must still group into one trade
        # and classify as a Covered Call.
        monkeypatch.setattr(
            data_provider_settings.portfolio_repo,
            "list_holdings",
            lambda client, user_id: [_holding(raw_name="Z", symbol="Z", broker="OtherBroker")],
        )
        monkeypatch.setattr(
            data_provider_settings.portfolio_repo,
            "list_positions",
            lambda client, user_id: [
                _position(raw_name="Z-CE", symbol="Z", option_type=OptionType.CE, qty=-50, broker="Dhan")
            ],
        )
        monkeypatch.setattr(data_provider_settings.portfolio_repo, "list_trade_groups", lambda client, user_id: [])
        monkeypatch.setattr(data_provider_settings.portfolio_repo, "list_trade_meta", lambda client, user_id: [])
        saved = {}
        monkeypatch.setattr(
            data_provider_settings.portfolio_repo,
            "set_trade_meta",
            lambda client, user_id, portfolio_name, trade_id, *, underlying_label, trade_type: saved.update(
                trade_id=trade_id, trade_type=trade_type
            ),
        )

        data_provider_settings._auto_classify_new_trades(client=object(), user_id="u1", portfolio_name="My Portfolio")

        assert saved == {"trade_id": "Z", "trade_type": "Covered Call"}

    def test_no_holdings_or_positions_short_circuits_without_any_repo_calls(self, monkeypatch):
        monkeypatch.setattr(data_provider_settings.portfolio_repo, "list_holdings", lambda client, user_id: [])
        monkeypatch.setattr(data_provider_settings.portfolio_repo, "list_positions", lambda client, user_id: [])

        def fail(*a, **k):
            raise AssertionError("should not be called when there are no legs at all")

        monkeypatch.setattr(data_provider_settings.portfolio_repo, "list_trade_groups", fail)
        monkeypatch.setattr(data_provider_settings.portfolio_repo, "list_trade_meta", fail)

        data_provider_settings._auto_classify_new_trades(client=object(), user_id="u1", portfolio_name="My Portfolio")
