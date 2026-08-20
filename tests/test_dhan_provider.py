"""Tests for DhanProvider's per-user portfolio-sync methods (get_holdings,
get_positions, get_ltp_by_security_id) -- header construction and the
401 -> DhanAuthError mapping used by pages/6_My_Broker.py's "Connect Dhan
account" flow. get_historical_daily is untouched and untested here.

Also covers the F&O instrument resolution added for live ETF/futures/
option LTP (migration 0032): _load_fo_instrument_master's column
resolution and NSE/FUTSTK-OPTSTK filtering, resolve_fo_security_id's
underlying+expiry+strike+option_type matching, and get_fo_quotes'
batching/skip-unresolved behavior -- plus get_quotes' own matching
skip-unresolved fix, added after a live regression: widening the
watched-symbol universe to every tracked ETF (migration 0032) meant one
ETF Dhan's equity instrument master didn't carry made get_quotes raise
and silently wipe out live pricing for the ENTIRE batch, not just that
one symbol."""
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


_EQUITY_MASTER_FIXTURE = pd.DataFrame(
    [
        {"SECURITY_ID": "1001", "TRADING_SYMBOL": "RELIANCE", "EXCH": "NSE", "SEGMENT": "EQ"},
        {"SECURITY_ID": "1002", "TRADING_SYMBOL": "TCS", "EXCH": "NSE", "SEGMENT": "EQ"},
        # No row for "BADETF" -- simulates a tracked symbol Dhan's equity
        # instrument master doesn't carry.
    ]
)


class TestGetQuotes:
    def test_unresolvable_symbol_is_skipped_not_fatal_to_the_whole_batch(self, monkeypatch):
        # Regression: before this fix, one unresolvable symbol (e.g. an
        # ETF Dhan's equity master doesn't carry) made resolve_security_id
        # raise inside the dict comprehension, aborting get_quotes
        # entirely -- load_live_dhan_prices' caller then caught that as a
        # ProviderError and returned {} for the WHOLE batch, silently
        # wiping out live pricing for every other (perfectly resolvable)
        # symbol too. Confirmed live after widening the watched-symbol
        # universe to every tracked ETF (migration 0032).
        dhan_provider._load_instrument_master.cache_clear()
        monkeypatch.setattr(dhan_provider.pd, "read_csv", lambda *a, **k: _EQUITY_MASTER_FIXTURE)

        def fake_post(url, json=None, headers=None, timeout=None):
            assert set(json["NSE_EQ"]) == {1001, 1002}
            return _FakeResponse(
                200,
                json_data={"data": {"NSE_EQ": {"1001": {"last_price": 2950.0}, "1002": {"last_price": 4100.0}}}},
            )

        monkeypatch.setattr(httpx, "post", fake_post)
        provider = DhanProvider(client_id="CID1", access_token="TOKEN1")

        result = provider.get_quotes(["RELIANCE", "TCS", "BADETF"])

        assert set(result) == {"RELIANCE", "TCS"}
        assert result["RELIANCE"].latest_price == 2950.0
        assert result["TCS"].latest_price == 4100.0
        dhan_provider._load_instrument_master.cache_clear()

    def test_all_symbols_unresolvable_returns_empty_without_a_request(self, monkeypatch):
        dhan_provider._load_instrument_master.cache_clear()
        monkeypatch.setattr(dhan_provider.pd, "read_csv", lambda *a, **k: _EQUITY_MASTER_FIXTURE)

        def fake_post(*args, **kwargs):
            raise AssertionError("should not make a request when nothing resolved")

        monkeypatch.setattr(httpx, "post", fake_post)
        provider = DhanProvider(client_id="CID1", access_token="TOKEN1")

        assert provider.get_quotes(["BADETF1", "BADETF2"]) == {}
        dhan_provider._load_instrument_master.cache_clear()


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


# Shaped exactly like a real download (confirmed live against
# https://images.dhan.co/api-data/api-scrip-master.csv): SM_SYMBOL_NAME
# is blank for every stock F&O row (not modeled here at all -- the
# resolver never reads it), strike_price/option_type are real sentinels
# for a future ("-0.01"/"XX", not blank), and the underlying has to come
# from SEM_TRADING_SYMBOL. NAM-INDIA covers a hyphenated underlying.
_FO_MASTER_FIXTURE = pd.DataFrame(
    [
        {
            "SEM_SMST_SECURITY_ID": "50001",
            "SEM_EXM_EXCH_ID": "NSE",
            "SEM_INSTRUMENT_NAME": "FUTSTK",
            "SEM_TRADING_SYMBOL": "RELIANCE-Aug2026-FUT",
            "SEM_EXPIRY_DATE": "2026-08-27",
            "SEM_STRIKE_PRICE": -0.01,
            "SEM_OPTION_TYPE": "XX",
        },
        {
            "SEM_SMST_SECURITY_ID": "50002",
            "SEM_EXM_EXCH_ID": "NSE",
            "SEM_INSTRUMENT_NAME": "OPTSTK",
            "SEM_TRADING_SYMBOL": "RELIANCE-Aug2026-3000-CE",
            "SEM_EXPIRY_DATE": "2026-08-27",
            "SEM_STRIKE_PRICE": 3000.0,
            "SEM_OPTION_TYPE": "CE",
        },
        {
            "SEM_SMST_SECURITY_ID": "50003",
            "SEM_EXM_EXCH_ID": "NSE",
            "SEM_INSTRUMENT_NAME": "OPTSTK",
            "SEM_TRADING_SYMBOL": "RELIANCE-Aug2026-2900-PE",
            "SEM_EXPIRY_DATE": "2026-08-27",
            "SEM_STRIKE_PRICE": 2900.0,
            "SEM_OPTION_TYPE": "PE",
        },
        # Hyphenated underlying -- confirms right-anchored parsing, not a
        # naive split-on-first-hyphen.
        {
            "SEM_SMST_SECURITY_ID": "50004",
            "SEM_EXM_EXCH_ID": "NSE",
            "SEM_INSTRUMENT_NAME": "OPTSTK",
            "SEM_TRADING_SYMBOL": "NAM-INDIA-Aug2026-720-CE",
            "SEM_EXPIRY_DATE": "2026-08-27",
            "SEM_STRIKE_PRICE": 720.0,
            "SEM_OPTION_TYPE": "CE",
        },
        # Same underlying/expiry/strike on BSE -- must be excluded (NSE-only).
        {
            "SEM_SMST_SECURITY_ID": "99999",
            "SEM_EXM_EXCH_ID": "BSE",
            "SEM_INSTRUMENT_NAME": "OPTSTK",
            "SEM_TRADING_SYMBOL": "RELIANCE-Aug2026-2900-PE",
            "SEM_EXPIRY_DATE": "2026-08-27",
            "SEM_STRIKE_PRICE": 2900.0,
            "SEM_OPTION_TYPE": "PE",
        },
        # A plain equity row -- must be excluded (FUTSTK/OPTSTK only).
        {
            "SEM_SMST_SECURITY_ID": "11111",
            "SEM_EXM_EXCH_ID": "NSE",
            "SEM_INSTRUMENT_NAME": "EQUITY",
            "SEM_TRADING_SYMBOL": "RELIANCE",
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

        assert set(master["security_id"]) == {"50001", "50002", "50003", "50004"}

    def test_normalizes_xx_option_type_to_fut(self, monkeypatch):
        # Dhan's real feed uses 'XX' (not blank) for a future's option
        # type -- confirmed live.
        monkeypatch.setattr(dhan_provider.pd, "read_csv", lambda *a, **k: _FO_MASTER_FIXTURE)

        master = dhan_provider._load_fo_instrument_master()

        future_row = master[master["security_id"] == "50001"].iloc[0]
        assert future_row["option_type"] == "FUT"

    def test_underlying_symbol_parsed_from_trading_symbol_not_the_blank_sm_symbol_name_column(self, monkeypatch):
        monkeypatch.setattr(dhan_provider.pd, "read_csv", lambda *a, **k: _FO_MASTER_FIXTURE)

        master = dhan_provider._load_fo_instrument_master()

        assert master[master["security_id"] == "50002"].iloc[0]["underlying_symbol"] == "RELIANCE"

    def test_underlying_symbol_with_internal_hyphen_is_not_truncated(self, monkeypatch):
        # "NAM-INDIA-Aug2026-720-CE" -- a naive split-on-first-hyphen
        # would wrongly resolve this to "NAM".
        monkeypatch.setattr(dhan_provider.pd, "read_csv", lambda *a, **k: _FO_MASTER_FIXTURE)

        master = dhan_provider._load_fo_instrument_master()

        assert master[master["security_id"] == "50004"].iloc[0]["underlying_symbol"] == "NAM-INDIA"


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
