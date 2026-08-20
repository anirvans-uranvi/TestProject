"""Tests for src.utils.refresh_bar's _refresh_user_live_prices -- the
"Market Data Refresh" button's per-user live-quote branch, only run when
the account's Data Provider setting is Dhan/Zerodha. The rest of
refresh_bar.py is Streamlit UI glue (buttons, captions, spinners) in the
same "not unit tested, only this cache/repo-touching helper is" spirit as
every other page-local formatter in this codebase."""
from datetime import date, datetime

from src.data_providers.base import ProviderError
from src.data_providers.dhan_provider import DhanAuthError
from src.models.enums import CompanyType, OptionType
from src.models.market_data import Quote
from src.models.portfolio import BrokerConnection, PortfolioPosition
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


def _patch_dhan_provider(monkeypatch, *, quotes=None, quotes_error=None, fo_quotes=None, fo_quotes_error=None):
    """Installs a fake refresh_bar.DhanProvider -- _refresh_user_live_prices
    calls DhanProvider directly (not the cached load_live_dhan_prices
    wrapper) so it can tell a real error apart from "nothing to quote",
    see that function's own docstring for why. `captured` records the
    client_id/access_token/symbols/contracts each fake method was called
    with, for assertions."""
    captured = {}

    class _FakeDhanProvider:
        def __init__(self, client_id, access_token):
            captured["client_id"] = client_id
            captured["access_token"] = access_token

        def get_quotes(self, symbols):
            captured["symbols"] = symbols
            if quotes_error is not None:
                raise quotes_error
            return quotes or {}

        def get_fo_quotes(self, contracts):
            captured["contracts"] = contracts
            if fo_quotes_error is not None:
                raise fo_quotes_error
            return fo_quotes or {}

    monkeypatch.setattr(refresh_bar, "DhanProvider", _FakeDhanProvider)
    return captured


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
        captured = _patch_dhan_provider(
            monkeypatch,
            quotes={
                "SBIN": Quote(symbol="SBIN", latest_price=811.9, as_of=datetime(2026, 8, 20, 13, 0), source="dhan"),
                "TCS": Quote(symbol="TCS", latest_price=4100.0, as_of=datetime(2026, 8, 20, 13, 0), source="dhan"),
            },
        )
        upsert_calls = []
        monkeypatch.setattr(
            refresh_bar.snapshot_repo, "upsert_user_live_prices", lambda client, user_id, prices: upsert_calls.append(prices)
        )

        result = refresh_bar._refresh_user_live_prices(client=object(), user_id="u1", broker="Dhan")

        assert captured["client_id"] == "CID1"
        assert captured["access_token"] == "TOKEN1"
        assert set(captured["symbols"]) == {"SBIN", "TCS", "JIOFIN"}  # union, no duplicates
        assert upsert_calls == [{"SBIN": 811.9, "TCS": 4100.0}]
        assert result == {"broker": "Dhan", "quoted": 2, "total": 3, "fo_quoted": 0, "fo_total": 0, "fo_error": None, "fo_missing": []}

    def test_dhan_widens_the_equity_universe_with_every_tracked_etf(self, monkeypatch):
        connection = BrokerConnection(user_id="u1", broker="Dhan", client_id="CID1", access_token="TOKEN1")
        monkeypatch.setattr(refresh_bar.portfolio_repo, "get_broker_connection", lambda client, user_id, broker: connection)
        _patch_universe(monkeypatch, constituents=("SBIN",), portfolio_symbols=(), etfs=("NIFTYBEES", "GOLDBEES"))
        captured = _patch_dhan_provider(monkeypatch)
        monkeypatch.setattr(refresh_bar.snapshot_repo, "upsert_user_live_prices", lambda *a, **k: None)

        refresh_bar._refresh_user_live_prices(client=object(), user_id="u1", broker="Dhan")

        assert set(captured["symbols"]) == {"SBIN", "NIFTYBEES", "GOLDBEES"}

    def test_dhan_auth_error_surfaces_as_an_error_not_a_silent_zero(self, monkeypatch):
        # Regression: DhanAuthError (expired token, or a Dhan account
        # missing the separate paid "Data APIs" subscription market
        # quotes require) used to be swallowed by load_live_dhan_prices
        # into an empty {}, showing a misleading "cached 0 of N" success
        # message with no indication anything was actually wrong.
        connection = BrokerConnection(user_id="u1", broker="Dhan", client_id="CID1", access_token="TOKEN1")
        monkeypatch.setattr(refresh_bar.portfolio_repo, "get_broker_connection", lambda client, user_id, broker: connection)
        _patch_universe(monkeypatch, constituents=("SBIN",), portfolio_symbols=())
        _patch_dhan_provider(monkeypatch, quotes_error=DhanAuthError("Dhan access token rejected (401): bad token"))
        upsert_calls = []
        monkeypatch.setattr(
            refresh_bar.snapshot_repo, "upsert_user_live_prices", lambda *a, **k: upsert_calls.append(a)
        )

        result = refresh_bar._refresh_user_live_prices(client=object(), user_id="u1", broker="Dhan")

        assert "error" in result
        assert "401" in result["error"]
        assert not upsert_calls  # bails out before ever writing/attempting F&O

    def test_dhan_generic_provider_error_also_surfaces_as_an_error(self, monkeypatch):
        connection = BrokerConnection(user_id="u1", broker="Dhan", client_id="CID1", access_token="TOKEN1")
        monkeypatch.setattr(refresh_bar.portfolio_repo, "get_broker_connection", lambda client, user_id, broker: connection)
        _patch_universe(monkeypatch, constituents=("SBIN",), portfolio_symbols=())
        _patch_dhan_provider(monkeypatch, quotes_error=ProviderError("Dhan request to ... failed: network error"))

        result = refresh_bar._refresh_user_live_prices(client=object(), user_id="u1", broker="Dhan")

        assert "error" in result
        assert "network error" in result["error"]

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
        captured = _patch_dhan_provider(monkeypatch)
        monkeypatch.setattr(refresh_bar.snapshot_repo, "upsert_user_live_prices", lambda *a, **k: None)
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

    def test_partial_fo_miss_is_named_not_just_counted(self, monkeypatch):
        connection = BrokerConnection(user_id="u1", broker="Dhan", client_id="CID1", access_token="TOKEN1")
        monkeypatch.setattr(refresh_bar.portfolio_repo, "get_broker_connection", lambda client, user_id, broker: connection)
        position = PortfolioPosition(
            user_id="u1", portfolio_name="My Portfolio", broker="Dhan", raw_name="RELIANCE FUT",
            symbol="RELIANCE", expiry_date=date(2026, 8, 27), qty=1, avg_price=2900.0,
        )
        _patch_universe(monkeypatch, constituents=(), portfolio_symbols=(), positions=[position])
        # Dhan quotes the RELIANCE future but the fake never resolves it
        # (fo_quotes stays empty) -- simulates a real partial miss.
        _patch_dhan_provider(monkeypatch, fo_quotes={})
        monkeypatch.setattr(refresh_bar.snapshot_repo, "upsert_user_live_prices", lambda *a, **k: None)
        monkeypatch.setattr(refresh_bar.snapshot_repo, "upsert_user_live_fo_prices", lambda *a, **k: None)

        result = refresh_bar._refresh_user_live_prices(client=object(), user_id="u1", broker="Dhan")

        assert result["fo_missing"] == [("RELIANCE", date(2026, 8, 27), 0.0, "FUT")]

    def test_dhan_fo_auth_error_does_not_fail_the_equity_leg(self, monkeypatch):
        # Equity quotes already succeeded and got cached -- a *separate*
        # F&O-only failure (e.g. the F&O instrument master 401ing on its
        # own request) shouldn't discard that.
        connection = BrokerConnection(user_id="u1", broker="Dhan", client_id="CID1", access_token="TOKEN1")
        monkeypatch.setattr(refresh_bar.portfolio_repo, "get_broker_connection", lambda client, user_id, broker: connection)
        position = PortfolioPosition(
            user_id="u1", portfolio_name="My Portfolio", broker="Dhan", raw_name="RELIANCE FUT",
            symbol="RELIANCE", expiry_date=date(2026, 8, 27), qty=1, avg_price=2900.0,
        )
        _patch_universe(monkeypatch, constituents=("SBIN",), portfolio_symbols=(), positions=[position])
        _patch_dhan_provider(
            monkeypatch,
            quotes={"SBIN": Quote(symbol="SBIN", latest_price=811.9, as_of=datetime(2026, 8, 20, 13, 0), source="dhan")},
            fo_quotes_error=DhanAuthError("401"),
        )
        upsert_calls = []
        monkeypatch.setattr(
            refresh_bar.snapshot_repo, "upsert_user_live_prices", lambda client, user_id, prices: upsert_calls.append(prices)
        )
        fo_upsert_calls = []
        monkeypatch.setattr(
            refresh_bar.snapshot_repo, "upsert_user_live_fo_prices", lambda client, user_id, prices: fo_upsert_calls.append(prices)
        )

        result = refresh_bar._refresh_user_live_prices(client=object(), user_id="u1", broker="Dhan")

        assert "error" not in result
        assert upsert_calls == [{"SBIN": 811.9}]
        assert fo_upsert_calls == [{}]
        assert result["fo_quoted"] == 0
        assert result["fo_total"] == 1
        assert result["fo_error"] == "401"  # surfaced, not silently swallowed

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
        assert result == {"broker": "Zerodha", "quoted": 1, "total": 1, "fo_quoted": 0, "fo_total": 0, "fo_error": None, "fo_missing": []}

    def test_no_symbols_quoted_still_upserts_the_empty_dict(self, monkeypatch):
        # upsert_user_live_prices itself no-ops on an empty dict (see
        # snapshot_repo) -- this just confirms the caller doesn't special
        # case it away.
        connection = BrokerConnection(user_id="u1", broker="Dhan", client_id="CID1", access_token="TOKEN1")
        monkeypatch.setattr(refresh_bar.portfolio_repo, "get_broker_connection", lambda client, user_id, broker: connection)
        _patch_universe(monkeypatch, constituents=("SBIN",), portfolio_symbols=())
        _patch_dhan_provider(monkeypatch)
        upsert_calls = []
        monkeypatch.setattr(
            refresh_bar.snapshot_repo, "upsert_user_live_prices", lambda client, user_id, prices: upsert_calls.append(prices)
        )

        result = refresh_bar._refresh_user_live_prices(client=object(), user_id="u1", broker="Dhan")

        assert upsert_calls == [{}]
        assert result == {"broker": "Dhan", "quoted": 0, "total": 1, "fo_quoted": 0, "fo_total": 0, "fo_error": None, "fo_missing": []}
