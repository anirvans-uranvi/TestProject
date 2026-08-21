"""Tests for src/services/edge_refresh.py's Python client for the
manual-refresh/fo-refresh Edge Functions. Only trigger_manual_refresh's
`mode` plumbing is covered here -- the Edge Functions' own mode-branching
logic (supabase/functions/manual-refresh/index.ts) has no Python
counterpart to unit test; see that file's own `deno test` coverage."""
from __future__ import annotations

import httpx
import pytest

from src.services import edge_refresh


class _FakeResponse:
    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text or (str(json_data) if json_data is not None else "")

    def json(self):
        return self._json_data


@pytest.fixture(autouse=True)
def _configure_supabase_url(monkeypatch):
    monkeypatch.setattr(
        edge_refresh, "get_settings", lambda: type("S", (), {"supabase_url": "https://example.supabase.co"})()
    )


class TestTriggerManualRefresh:
    def test_forwards_mode_in_the_json_body(self, monkeypatch):
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            return _FakeResponse(200, json_data={"succeeded": 1, "failed": 0, "total": 1, "symbolsFailed": []})

        monkeypatch.setattr(httpx, "post", fake_post)
        edge_refresh.trigger_manual_refresh("token-123", mode="fundamentals")
        assert captured["json"] == {"mode": "fundamentals"}
        assert captured["url"].endswith("/functions/v1/manual-refresh")

    def test_price_mode_forwarded_too(self, monkeypatch):
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["json"] = json
            return _FakeResponse(200, json_data={"succeeded": 0, "failed": 0, "total": 0, "symbolsFailed": []})

        monkeypatch.setattr(httpx, "post", fake_post)
        edge_refresh.trigger_manual_refresh("token-123", mode="price")
        assert captured["json"] == {"mode": "price"}

    def test_cooldown_response_raises_retriable_error(self, monkeypatch):
        monkeypatch.setattr(
            httpx, "post", lambda *a, **k: _FakeResponse(429, json_data={"message": "wait a bit"})
        )
        with pytest.raises(edge_refresh.ManualRefreshError) as exc_info:
            edge_refresh.trigger_manual_refresh("token-123", mode="price")
        assert exc_info.value.retriable is True

    def test_error_response_raises_non_retriable_error(self, monkeypatch):
        monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(500, json_data={"error": "boom"}))
        with pytest.raises(edge_refresh.ManualRefreshError) as exc_info:
            edge_refresh.trigger_manual_refresh("token-123", mode="fundamentals")
        assert exc_info.value.retriable is False
