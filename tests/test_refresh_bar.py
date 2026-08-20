"""Tests for src.utils.refresh_bar's _refresh_user_live_prices -- the
"Market Data Refresh" button's per-user live-quote branch, only run when
the account's Data Provider setting is Dhan/Zerodha. The rest of
refresh_bar.py is Streamlit UI glue (buttons, captions, spinners) in the
same "not unit tested, only this cache/repo-touching helper is" spirit as
every other page-local formatter in this codebase."""
from src.models.enums import CompanyType
from src.models.portfolio import BrokerConnection
from src.utils import refresh_bar


class _Company:
    def __init__(self, symbol, company_type=CompanyType.EQUITY):
        self.symbol = symbol
        self.company_type = company_type


def _patch_universe(
    monkeypatch, *, constituents=("SBIN", "TCS"), portfolio_symbols=("JIOFIN",), etfs=(), positions=(), fo_metrics=()
):
    monkeypatch.setattr(
        refresh_bar.companies_repo, "list_current_constituents", lambda client: [_Company(s) for s in constituents]
    )
    monkeypatch.setattr(refresh_bar.portfolio_repo, "list_portfolio_symbols", lambda client, user_id: list(portfolio_symbols))
    monkeypatch.setattr(
        refresh_bar.companies_repo,
        "list_all_companies",
        lambda client: [_Company(s, CompanyType.ETF) for s in etfs],
    )
    monkeypatch.setattr(refresh_bar.portfolio_repo, "list_positions", lambda client, user_id: list(positions))
    monkeypatch.setattr(refresh_bar.fo_repo, "get_dashboard_fo_metrics", lambda client: list(fo_metrics))


class TestRefreshUserLivePrices:
    def test_no_connection_returns_an_error_and_touches_nothing_else(self, monkeypatch):
        monkeypatch.setattr(refresh_bar.portfolio_repo, "get_broker_connection", lambda client, user_id, broker: None)
        upsert_calls = []
        monkeypatch.setattr(
            refresh_bar.snapshot_repo, "upsert_user_live_prices", lambda *a, **k: upsert_calls.append((a, k))
        )

        result = refresh_bar._refresh_user_live_prices(client=object(), user_id="u1", broker="Dhan")

        assert "error" in result
        assert not upsert_calls

    def test_connection_with_no_access_token_returns_an_error(self, monkeypatch):
        connection = BrokerConnection(user_id="u1", broker="Dhan", client_id="CID1")
        monkeypatch.setattr(refresh_bar.portfolio_repo, "get_broker_connection", lambda client, user_id, broker: connection)

        result = refresh_bar._refresh_user_live_prices(client=object(), user_id="u1", broker="Dhan")

        assert "error" in result

    def test_dhan_fetches_over_the_union_of_constituents_and_portfolio_symbols(self, monkeypatch):
        connection = BrokerConnection(user_id="u1", broker="Dhan", client_id="CID1", access_token="TOKEN1")
        monkeypatch.setattr(refresh_bar.portfolio_repo, "get_broker_connection", lambda client, user_id, broker: connection)
        _patch_universe(monkeypatch, constituents=("SBIN", "TCS"), portfolio_symbols=("JIOFIN", "SBIN"))

        captured = {}

        def fake_load_live_dhan_prices(client_id, access_token, symbols, cache_bust):
            captured["client_id"] = client_id
            captured["access_token"] = access_token
            captured["symbols"] = symbols
            return {"SBIN": 811.9, "TCS": 4100.0}

        monkeypatch.setattr(refresh_bar, "load_live_dhan_prices", fake_load_live_dhan_prices)
        upsert_calls = []
        monkeypatch.setattr(
            refresh_bar.snapshot_repo, "upsert_user_live_prices", lambda client, user_id, prices: upsert_calls.append(prices)
        )

        result = refresh_bar._refresh_user_live_prices(client=object(), user_id="u1", broker="Dhan")

        assert captured["client_id"] == "CID1"
        assert captured["access_token"] == "TOKEN1"
        assert set(captured["symbols"]) == {"SBIN", "TCS", "JIOFIN"}  # union, no duplicates
        assert upsert_calls == [{"SBIN": 811.9, "TCS": 4100.0}]
        assert result == {"broker": "Dhan", "quoted": 2, "total": 3, "fo_quoted": 0, "fo_total": 0}

    def test_dhan_widens_the_equity_universe_with_every_tracked_etf(self, monkeypatch):
        connection = BrokerConnection(user_id="u1", broker="Dhan", client_id="CID1", access_token="TOKEN1")
        monkeypatch.setattr(refresh_bar.portfolio_repo, "get_broker_connection", lambda client, user_id, broker: connection)
        _patch_universe(monkeypatch, constituents=("SBIN",), portfolio_symbols=(), etfs=("NIFTYBEES", "GOLDBEES"))

        captured = {}
        monkeypatch.setattr(
            refresh_bar,
            "load_live_dhan_prices",
            lambda client_id, access_token, symbols, cache_bust: captured.setdefault("symbols", symbols) or {},
        )
        monkeypatch.setattr(refresh_bar.snapshot_repo, "upsert_user_live_prices", lambda *a, **k: None)

        refresh_bar._refresh_user_live_prices(client=object(), user_id="u1", broker="Dhan")

        assert set(captured["symbols"]) == {"SBIN", "NIFTYBEES", "GOLDBEES"}

    def test_zerodha_does_not_widen_with_etfs(self, monkeypatch):
        # Confirms the ETF/F&O widening is Dhan-only -- Zerodha has no F&O
        # instrument resolver yet (see _dhan_fo_universe's docstring).
        connection = BrokerConnection(
            user_id="u1", broker="Zerodha", client_id="KEY1", api_secret="SECRET1", access_token="TOKEN1"
        )
        monkeypatch.setattr(refresh_bar.portfolio_repo, "get_broker_connection", lambda client, user_id, broker: connection)
        _patch_universe(monkeypatch, constituents=("SBIN",), portfolio_symbols=(), etfs=("NIFTYBEES",))

        captured = {}
        monkeypatch.setattr(
            refresh_bar,
            "load_live_zerodha_prices",
            lambda api_key, api_secret, access_token, symbols, cache_bust: captured.setdefault("symbols", symbols) or {},
        )
        monkeypatch.setattr(refresh_bar.snapshot_repo, "upsert_user_live_prices", lambda *a, **k: None)

        refresh_bar._refresh_user_live_prices(client=object(), user_id="u1", broker="Zerodha")

        assert set(captured["symbols"]) == {"SBIN"}

    def test_dhan_fo_universe_combines_positions_and_dashboard_metrics(self, monkeypatch):
        from datetime import date

        from src.models.enums import OptionType
        from src.models.portfolio import PortfolioPosition

        connection = BrokerConnection(user_id="u1", broker="Dhan", client_id="CID1", access_token="TOKEN1")
        monkeypatch.setattr(refresh_bar.portfolio_repo, "get_broker_connection", lambda client, user_id, broker: connection)
        position = PortfolioPosition(
            user_id="u1", portfolio_name="My Portfolio", broker="Dhan", raw_name="RELIANCE FUT",
            symbol="RELIANCE", expiry_date=date(2026, 8, 27), qty=1, avg_price=2900.0,
        )
        fo_metric_row = {
            "symbol": "SBIN", "expiry_date": "2026-08-27", "csp_strike": 800.0, "cc_strike": 850.0,
        }
        _patch_universe(monkeypatch, constituents=(), portfolio_symbols=(), positions=[position], fo_metrics=[fo_metric_row])
        monkeypatch.setattr(refresh_bar, "load_live_dhan_prices", lambda *a, **k: {})
        monkeypatch.setattr(refresh_bar.snapshot_repo, "upsert_user_live_prices", lambda *a, **k: None)

        captured = {}

        class _FakeDhanProvider:
            def __init__(self, client_id, access_token):
                pass

            def get_fo_quotes(self, contracts):
                captured["contracts"] = contracts
                return {}

        monkeypatch.setattr(refresh_bar, "DhanProvider", _FakeDhanProvider)
        fo_upsert_calls = []
        monkeypatch.setattr(
            refresh_bar.snapshot_repo, "upsert_user_live_fo_prices", lambda client, user_id, prices: fo_upsert_calls.append(prices)
        )

        result = refresh_bar._refresh_user_live_prices(client=object(), user_id="u1", broker="Dhan")

        assert set(captured["contracts"]) == {
            ("RELIANCE", date(2026, 8, 27), 0.0, "FUT"),
            ("SBIN", date(2026, 8, 27), 800.0, "PE"),
            ("SBIN", date(2026, 8, 27), 850.0, "CE"),
        }
        assert result["fo_total"] == 3
        assert fo_upsert_calls == [{}]

    def test_zerodha_uses_the_zerodha_loader_with_api_secret(self, monkeypatch):
        connection = BrokerConnection(
            user_id="u1", broker="Zerodha", client_id="KEY1", api_secret="SECRET1", access_token="TOKEN1"
        )
        monkeypatch.setattr(refresh_bar.portfolio_repo, "get_broker_connection", lambda client, user_id, broker: connection)
        _patch_universe(monkeypatch, constituents=("SBIN",), portfolio_symbols=())

        captured = {}

        def fake_load_live_zerodha_prices(api_key, api_secret, access_token, symbols, cache_bust):
            captured.update(api_key=api_key, api_secret=api_secret, access_token=access_token)
            return {"SBIN": 811.9}

        monkeypatch.setattr(refresh_bar, "load_live_zerodha_prices", fake_load_live_zerodha_prices)
        monkeypatch.setattr(refresh_bar.snapshot_repo, "upsert_user_live_prices", lambda *a, **k: None)

        result = refresh_bar._refresh_user_live_prices(client=object(), user_id="u1", broker="Zerodha")

        assert captured == {"api_key": "KEY1", "api_secret": "SECRET1", "access_token": "TOKEN1"}
        assert result == {"broker": "Zerodha", "quoted": 1, "total": 1, "fo_quoted": 0, "fo_total": 0}

    def test_no_symbols_quoted_still_upserts_the_empty_dict(self, monkeypatch):
        # upsert_user_live_prices itself no-ops on an empty dict (see
        # snapshot_repo) -- this just confirms the caller doesn't special
        # case it away.
        connection = BrokerConnection(user_id="u1", broker="Dhan", client_id="CID1", access_token="TOKEN1")
        monkeypatch.setattr(refresh_bar.portfolio_repo, "get_broker_connection", lambda client, user_id, broker: connection)
        _patch_universe(monkeypatch, constituents=("SBIN",), portfolio_symbols=())
        monkeypatch.setattr(refresh_bar, "load_live_dhan_prices", lambda *a, **k: {})
        upsert_calls = []
        monkeypatch.setattr(
            refresh_bar.snapshot_repo, "upsert_user_live_prices", lambda client, user_id, prices: upsert_calls.append(prices)
        )

        result = refresh_bar._refresh_user_live_prices(client=object(), user_id="u1", broker="Dhan")

        assert upsert_calls == [{}]
        assert result == {"broker": "Dhan", "quoted": 0, "total": 1, "fo_quoted": 0, "fo_total": 0}
