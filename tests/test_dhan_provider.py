"""Tests for DhanProvider's per-user portfolio-sync methods (get_holdings,
get_positions, get_ltp_by_security_id) -- header construction and the
401 -> DhanAuthError mapping used by pages/6_Portfolio.py's "Connect Dhan
account" flow. The pre-existing price-pipeline methods (get_quote/
get_quotes/get_historical_daily) are untouched and untested here."""
import httpx
import pytest

import src.data_providers.dhan_provider as dhan_provider
from src.data_providers.base import ProviderError
from src.data_providers.dhan_provider import DhanAuthError, DhanProvider


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
