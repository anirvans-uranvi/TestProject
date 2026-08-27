"""Regression tests for src/utils/session.py's get_user_client_cached().

These target one specific bug: a refreshed access/refresh token pair
from supabase-auth's set_session() was previously discarded at the end
of every call instead of being written back into st.session_state,
which (since Supabase rotates refresh tokens on every use) silently and
permanently broke every DB call in a browser session the first time the
access token happened to expire mid-session -- see get_user_client_cached()'s
own docstring for the full mechanics.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
import streamlit as st

from src.utils import session as session_module


class _FakeAuth:
    def __init__(self, session: object | None):
        self._session = session

    def get_session(self) -> object | None:
        return self._session


class _FakeClient:
    def __init__(self, session: object | None):
        self.auth = _FakeAuth(session)


@pytest.fixture(autouse=True)
def _session_state(monkeypatch):
    state = {"sb_access_token": "old-access", "sb_refresh_token": "old-refresh"}
    monkeypatch.setattr(st, "session_state", state)
    return state


class TestGetUserClientCached:
    def test_persists_refreshed_tokens_from_get_session(self, monkeypatch, _session_state):
        refreshed = SimpleNamespace(access_token="new-access", refresh_token="new-refresh")
        fake_client = _FakeClient(refreshed)
        monkeypatch.setattr(session_module, "get_user_client", lambda *a, **k: fake_client)

        result = session_module.get_user_client_cached()

        assert result is fake_client
        assert _session_state["sb_access_token"] == "new-access"
        assert _session_state["sb_refresh_token"] == "new-refresh"

    def test_leaves_tokens_unchanged_when_get_session_returns_none(self, monkeypatch, _session_state):
        fake_client = _FakeClient(None)
        monkeypatch.setattr(session_module, "get_user_client", lambda *a, **k: fake_client)

        session_module.get_user_client_cached()

        assert _session_state["sb_access_token"] == "old-access"
        assert _session_state["sb_refresh_token"] == "old-refresh"

    def test_signs_out_and_reruns_when_the_refresh_token_is_rejected(self, monkeypatch, _session_state):
        def _raise_invalid_grant(*_a, **_k):
            raise RuntimeError("invalid_grant: refresh token already used")

        monkeypatch.setattr(session_module, "get_user_client", _raise_invalid_grant)

        rerun_calls = []
        monkeypatch.setattr(st, "rerun", lambda: rerun_calls.append(True))

        with pytest.raises(RuntimeError):
            session_module.get_user_client_cached()

        assert rerun_calls == [True]
        assert "sb_access_token" not in _session_state
        assert "sb_refresh_token" not in _session_state
