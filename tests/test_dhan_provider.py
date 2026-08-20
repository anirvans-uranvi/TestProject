"""Tests for DhanProvider's per-user portfolio-sync methods (get_holdings,
get_positions, get_ltp_by_security_id) -- header construction and the
401 -> DhanAuthError mapping used by pages/6_My_Broker.py's "Connect Dhan
account" flow. The pre-existing price-pipeline methods (get_quote/
get_quotes/get_historical_daily) are untouched and untested here.

Also covers the F&O instrument resolution added for live ETF/futures/
option LTP (migration 0032): _load_fo_instrument_master's column
resolution and NSE/FUTSTK-OPTSTK filtering, resolve_fo_security_id's
underlying+expiry+strike+option_type matching, and get_fo_quotes'
batching/skip-unresolved behavior."""
from datetime import date

import httpx
import pandas as pd
import pytest

import src.data_providers.dhan_provider as dhan_provider
from src.data_providers.base import ProviderError
from src.data_providers.dhan_provider import DhanAuthError, DhanProvider, resolve_fo_security_id


class _FakeResponse:
    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text or (str(json_data) if json_data is not None else "")

    def json(self):
        return self._json_data


@pytest.fixture(autouse=True)
def _no_throttle_sleep(monkeypatch):
    # _throttle() sleeps against the real wall clock -- neutralize it so
    # tests run instantly regardless of call order/timing.
    monkeypatch.setattr(dhan_provider, "_throttle", lambda: None)


class TestGetHoldings:
    def test_sends_client_id_and_access_token_headers(self, monkeypatch):
        captured = {}

        def fake_request(method, url, json=None, headers=None, timeout=None):
            captured["method"] = method
            captured["url"] = url
            captured["headers"] = headers
            return _FakeResponse(200, json_data=[{"tradingSymbol": "SBIN"}])

        monkeypatch.setattr(httpx, "request", fake_request)
        provider = DhanProvider(client_id="CID1", access_token="TOKEN1")

        result = provider.get_holdings()

        assert result == [{"tradingSymbol": "SBIN"}]
        assert captured["method"] == "GET"
        assert captured["url"].endswith("/holdings")
        assert captured["headers"]["client-id"] == "CID1"
        assert captured["headers"]["access-token"] == "TOKEN1"

    def test_401_raises_dhan_auth_error(self, monkeypatch):
        monkeypatch.setattr(httpx, "request", lambda *a, **k: _FakeResponse(401, text="bad token"))
        provider = DhanProvider(client_id="CID1", access_token="EXPIRED")

        with pytest.raises(DhanAuthError):
            provider.get_holdings()

    def test_other_error_status_raises_generic_provider_error_not_auth_error(self, monkeypatch):
        monkeypatch.setattr(httpx, "request", lambda *a, **k: _FakeResponse(500, text="boom"))
        provider = DhanProvider(client_id="CID1", access_token="TOKEN1")

        with pytest.raises(ProviderError) as exc_info:
            provider.get_holdings()
        assert not isinstance(exc_info.value, DhanAuthError)


class TestGetPositions:
    def test_returns_raw_rows(self, monkeypatch):
        monkeypatch.setattr(httpx, "request", lambda *a, **k: _FakeResponse(200, json_data=[{"netQty": -780}]))
        provider = DhanProvider(client_id="CID1", access_token="TOKEN1")

        assert provider.get_positions() == [{"netQty": -780}]


class TestGetLtpBySecurityId:
    def test_flattens_nested_segment_response(self, monkeypatch):
        def fake_request(method, url, json=None, headers=None, timeout=None):
            assert method == "POST"
            assert json == {"NSE_FNO": [49081], "IDX_I": [13]}
            return _FakeResponse(
                200,
                json_data={
                    "data": {
                        "NSE_FNO": {"49081": {"last_price": 1.1}},
                        "IDX_I": {"13": {"last_price": 23000.5}},
                    },
                    "status": "success",
                },
            )

        monkeypatch.setattr(httpx, "request", fake_request)
        provider = DhanProvider(client_id="CID1", access_token="TOKEN1")

        result = provider.get_ltp_by_security_id({"NSE_FNO": ["49081"], "IDX_I": ["13"]})

        assert result == {"49081": 1.1, "13": 23000.5}

    def test_empty_input_short_circuits_without_a_request(self, monkeypatch):
        def fake_request(*args, **kwargs):
            raise AssertionError("should not make a request for an empty security id map")

        monkeypatch.setattr(httpx, "request", fake_request)
        provider = DhanProvider(client_id="CID1", access_token="TOKEN1")

        assert provider.get_ltp_by_security_id({}) == {}


_FO_MASTER_FIXTURE = pd.DataFrame(
    [
        # RELIANCE future -- option_type/strike blank in the raw feed,
        # normalized to option_type='FUT'/strike=0 by _load_fo_instrument_master.
        {
            "SEM_SMST_SECURITY_ID": "50001",
            "SEM_EXM_EXCH_ID": "NSE",
            "SEM_INSTRUMENT_NAME": "FUTSTK",
            "UNDERLYING_SYMBOL": "RELIANCE",
            "SEM_EXPIRY_DATE": "2026-08-27",
            "SEM_STRIKE_PRICE": None,
            "SEM_OPTION_TYPE": None,
        },
        {
            "SEM_SMST_SECURITY_ID": "50002",
            "SEM_EXM_EXCH_ID": "NSE",
            "SEM_INSTRUMENT_NAME": "OPTSTK",
            "UNDERLYING_SYMBOL": "RELIANCE",
            "SEM_EXPIRY_DATE": "2026-08-27",
            "SEM_STRIKE_PRICE": 3000.0,
            "SEM_OPTION_TYPE": "CE",
        },
        {
            "SEM_SMST_SECURITY_ID": "50003",
            "SEM_EXM_EXCH_ID": "NSE",
            "SEM_INSTRUMENT_NAME": "OPTSTK",
            "UNDERLYING_SYMBOL": "RELIANCE",
            "SEM_EXPIRY_DATE": "2026-08-27",
            "SEM_STRIKE_PRICE": 2900.0,
            "SEM_OPTION_TYPE": "PE",
        },
        # Same underlying/expiry/strike on BSE -- must be excluded (NSE-only).
        {
            "SEM_SMST_SECURITY_ID": "99999",
            "SEM_EXM_EXCH_ID": "BSE",
            "SEM_INSTRUMENT_NAME": "OPTSTK",
            "UNDERLYING_SYMBOL": "RELIANCE",
            "SEM_EXPIRY_DATE": "2026-08-27",
            "SEM_STRIKE_PRICE": 2900.0,
            "SEM_OPTION_TYPE": "PE",
        },
        # A plain equity row -- must be excluded (FUTSTK/OPTSTK only).
        {
            "SEM_SMST_SECURITY_ID": "11111",
            "SEM_EXM_EXCH_ID": "NSE",
            "SEM_INSTRUMENT_NAME": "EQUITY",
            "UNDERLYING_SYMBOL": "RELIANCE",
            "SEM_EXPIRY_DATE": None,
            "SEM_STRIKE_PRICE": None,
            "SEM_OPTION_TYPE": None,
        },
    ]
)


@pytest.fixture(autouse=True)
def _clear_fo_master_cache():
    dhan_provider._load_fo_instrument_master.cache_clear()
    yield
    dhan_provider._load_fo_instrument_master.cache_clear()


class TestLoadFoInstrumentMaster:
    def test_filters_to_nse_futstk_optstk_only(self, monkeypatch):
        monkeypatch.setattr(dhan_provider.pd, "read_csv", lambda *a, **k: _FO_MASTER_FIXTURE)

        master = dhan_provider._load_fo_instrument_master()

        assert set(master["security_id"]) == {"50001", "50002", "50003"}

    def test_normalizes_blank_option_type_to_fut(self, monkeypatch):
        monkeypatch.setattr(dhan_provider.pd, "read_csv", lambda *a, **k: _FO_MASTER_FIXTURE)

        master = dhan_provider._load_fo_instrument_master()

        future_row = master[master["security_id"] == "50001"].iloc[0]
        assert future_row["option_type"] == "FUT"
        assert future_row["strike_price"] == 0.0


class TestResolveFoSecurityId:
    def test_resolves_future(self, monkeypatch):
        monkeypatch.setattr(dhan_provider.pd, "read_csv", lambda *a, **k: _FO_MASTER_FIXTURE)

        assert resolve_fo_security_id("RELIANCE", date(2026, 8, 27), 0.0, "FUT") == "50001"

    def test_resolves_option_by_exact_strike(self, monkeypatch):
        monkeypatch.setattr(dhan_provider.pd, "read_csv", lambda *a, **k: _FO_MASTER_FIXTURE)

        assert resolve_fo_security_id("RELIANCE", date(2026, 8, 27), 3000.0, "CE") == "50002"
        assert resolve_fo_security_id("RELIANCE", date(2026, 8, 27), 2900.0, "PE") == "50003"

    def test_no_match_raises_provider_error(self, monkeypatch):
        monkeypatch.setattr(dhan_provider.pd, "read_csv", lambda *a, **k: _FO_MASTER_FIXTURE)

        with pytest.raises(ProviderError):
            resolve_fo_security_id("RELIANCE", date(2026, 8, 27), 9999.0, "CE")

    def test_wrong_exchange_row_never_matches(self, monkeypatch):
        # The BSE-sourced fixture row shares underlying/expiry/strike with
        # the real NSE PE row (50003) -- confirms NSE-only filtering, not
        # just that *a* match exists.
        monkeypatch.setattr(dhan_provider.pd, "read_csv", lambda *a, **k: _FO_MASTER_FIXTURE)

        assert resolve_fo_security_id("RELIANCE", date(2026, 8, 27), 2900.0, "PE") == "50003"


class TestGetFoQuotes:
    def test_batches_resolved_contracts_into_one_request(self, monkeypatch):
        monkeypatch.setattr(dhan_provider.pd, "read_csv", lambda *a, **k: _FO_MASTER_FIXTURE)
        captured = {}

        def fake_request(method, url, json=None, headers=None, timeout=None):
            captured["json"] = json
            return _FakeResponse(
                200,
                json_data={
                    "data": {
                        "NSE_FNO": {
                            "50001": {"last_price": 2950.0},
                            "50002": {"last_price": 45.5},
                            "50003": {"last_price": 32.0},
                        }
                    }
                },
            )

        monkeypatch.setattr(httpx, "request", fake_request)
        provider = DhanProvider(client_id="CID1", access_token="TOKEN1")
        contracts = [
            ("RELIANCE", date(2026, 8, 27), 0.0, "FUT"),
            ("RELIANCE", date(2026, 8, 27), 3000.0, "CE"),
            ("RELIANCE", date(2026, 8, 27), 2900.0, "PE"),
        ]

        result = provider.get_fo_quotes(contracts)

        assert set(captured["json"]["NSE_FNO"]) == {50001, 50002, 50003}
        assert result == {
            ("RELIANCE", date(2026, 8, 27), 0.0, "FUT"): 2950.0,
            ("RELIANCE", date(2026, 8, 27), 3000.0, "CE"): 45.5,
            ("RELIANCE", date(2026, 8, 27), 2900.0, "PE"): 32.0,
        }

    def test_unresolvable_contract_is_skipped_not_fatal(self, monkeypatch):
        monkeypatch.setattr(dhan_provider.pd, "read_csv", lambda *a, **k: _FO_MASTER_FIXTURE)

        def fake_request(method, url, json=None, headers=None, timeout=None):
            return _FakeResponse(200, json_data={"data": {"NSE_FNO": {"50002": {"last_price": 45.5}}}})

        monkeypatch.setattr(httpx, "request", fake_request)
        provider = DhanProvider(client_id="CID1", access_token="TOKEN1")
        contracts = [
            ("RELIANCE", date(2026, 8, 27), 3000.0, "CE"),
            ("RELIANCE", date(2026, 8, 27), 9999.0, "CE"),  # no such strike -- unresolvable
        ]

        result = provider.get_fo_quotes(contracts)

        assert result == {("RELIANCE", date(2026, 8, 27), 3000.0, "CE"): 45.5}

    def test_empty_input_short_circuits_without_a_request(self, monkeypatch):
        def fake_request(*args, **kwargs):
            raise AssertionError("should not make a request for an empty contract list")

        monkeypatch.setattr(httpx, "request", fake_request)
        provider = DhanProvider(client_id="CID1", access_token="TOKEN1")

        assert provider.get_fo_quotes([]) == {}
