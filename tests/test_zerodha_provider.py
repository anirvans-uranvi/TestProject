"""Tests for ZerodhaProvider's Kite Connect flow (login_url, checksum-based
generate_session, get_holdings, get_positions) -- used by
pages/6_My_Broker.py's "Connect Zerodha account" flow. No live Kite
Connect calls; all HTTP is mocked."""
import hashlib

import httpx
import pytest

import src.data_providers.zerodha_provider as zerodha_provider
from src.data_providers.base import ProviderError
from src.data_providers.zerodha_provider import ZerodhaAuthError, ZerodhaProvider


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
    monkeypatch.setattr(zerodha_provider, "_throttle", lambda: None)


class TestLoginUrl:
    def test_includes_api_key_and_v3(self):
        provider = ZerodhaProvider(api_key="KEY1", api_secret="SECRET1")
        assert provider.login_url() == "https://kite.zerodha.com/connect/login?v=3&api_key=KEY1"


class TestGenerateSession:
    def test_computes_checksum_and_returns_access_token(self, monkeypatch):
        captured = {}

        def fake_post(url, data=None, headers=None, timeout=None):
            captured["url"] = url
            captured["data"] = data
            captured["headers"] = headers
            return _FakeResponse(200, json_data={"data": {"access_token": "NEWTOKEN"}})

        monkeypatch.setattr(httpx, "post", fake_post)
        provider = ZerodhaProvider(api_key="KEY1", api_secret="SECRET1")

        access_token = provider.generate_session("REQTOKEN")

        expected_checksum = hashlib.sha256(b"KEY1REQTOKENSECRET1").hexdigest()
        assert access_token == "NEWTOKEN"
        assert captured["url"].endswith("/session/token")
        assert captured["data"] == {"api_key": "KEY1", "request_token": "REQTOKEN", "checksum": expected_checksum}

    def test_stores_the_returned_token_for_subsequent_calls(self, monkeypatch):
        monkeypatch.setattr(
            httpx, "post", lambda *a, **k: _FakeResponse(200, json_data={"data": {"access_token": "NEWTOKEN"}})
        )
        provider = ZerodhaProvider(api_key="KEY1", api_secret="SECRET1")
        provider.generate_session("REQTOKEN")

        assert provider._headers["Authorization"] == "token KEY1:NEWTOKEN"

    def test_error_status_raises_provider_error(self, monkeypatch):
        monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(403, text="bad checksum"))
        provider = ZerodhaProvider(api_key="KEY1", api_secret="SECRET1")

        with pytest.raises(ProviderError):
            provider.generate_session("REQTOKEN")

    def test_missing_access_token_in_response_raises_provider_error(self, monkeypatch):
        monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(200, json_data={"data": {}}))
        provider = ZerodhaProvider(api_key="KEY1", api_secret="SECRET1")

        with pytest.raises(ProviderError):
            provider.generate_session("REQTOKEN")


class TestGetHoldings:
    def test_sends_authorization_header_and_returns_data_list(self, monkeypatch):
        captured = {}

        def fake_get(url, headers=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers
            return _FakeResponse(200, json_data={"data": [{"tradingsymbol": "SBIN"}]})

        monkeypatch.setattr(httpx, "get", fake_get)
        provider = ZerodhaProvider(api_key="KEY1", api_secret="SECRET1", access_token="TOKEN1")

        result = provider.get_holdings()

        assert result == [{"tradingsymbol": "SBIN"}]
        assert captured["url"].endswith("/portfolio/holdings")
        assert captured["headers"]["Authorization"] == "token KEY1:TOKEN1"
        assert captured["headers"]["X-Kite-Version"] == "3"

    def test_401_raises_zerodha_auth_error(self, monkeypatch):
        monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResponse(401, text="expired session"))
        provider = ZerodhaProvider(api_key="KEY1", api_secret="SECRET1", access_token="EXPIRED")

        with pytest.raises(ZerodhaAuthError):
            provider.get_holdings()

    def test_403_raises_zerodha_auth_error(self, monkeypatch):
        monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResponse(403, text="expired session"))
        provider = ZerodhaProvider(api_key="KEY1", api_secret="SECRET1", access_token="EXPIRED")

        with pytest.raises(ZerodhaAuthError):
            provider.get_holdings()

    def test_other_error_status_raises_generic_provider_error_not_auth_error(self, monkeypatch):
        monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResponse(500, text="boom"))
        provider = ZerodhaProvider(api_key="KEY1", api_secret="SECRET1", access_token="TOKEN1")

        with pytest.raises(ProviderError) as exc_info:
            provider.get_holdings()
        assert not isinstance(exc_info.value, ZerodhaAuthError)

    def test_no_access_token_raises_provider_error_before_any_request(self, monkeypatch):
        def fake_get(*args, **kwargs):
            raise AssertionError("should not make a request with no access_token set")

        monkeypatch.setattr(httpx, "get", fake_get)
        provider = ZerodhaProvider(api_key="KEY1", api_secret="SECRET1")

        with pytest.raises(ProviderError):
            provider.get_holdings()


class TestGetPositions:
    def test_returns_only_the_net_list_not_day(self, monkeypatch):
        monkeypatch.setattr(
            httpx,
            "get",
            lambda *a, **k: _FakeResponse(
                200,
                json_data={"data": {"net": [{"tradingsymbol": "NIFTY2681123000PE", "quantity": -75}], "day": [{"x": 1}]}},
            ),
        )
        provider = ZerodhaProvider(api_key="KEY1", api_secret="SECRET1", access_token="TOKEN1")

        result = provider.get_positions()

        assert result == [{"tradingsymbol": "NIFTY2681123000PE", "quantity": -75}]

    def test_missing_net_key_returns_empty_list(self, monkeypatch):
        monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResponse(200, json_data={"data": {}}))
        provider = ZerodhaProvider(api_key="KEY1", api_secret="SECRET1", access_token="TOKEN1")

        assert provider.get_positions() == []
