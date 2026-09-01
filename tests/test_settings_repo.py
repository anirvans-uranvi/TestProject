"""Tests for settings_repo.get_user_settings -- specifically the
degrade-gracefully fallback for a stored data_provider value the app no
longer recognizes (confirmed live: an account still had 'zerodha' saved
after that broker was removed entirely -- src/utils/
data_provider_settings.py's module docstring -- which crashed every
single page for that account via a hard pydantic.ValidationError)."""
from __future__ import annotations

import types

import pytest
from pydantic import ValidationError

from src.repositories import settings_repo


class _FakeTable:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def select(self, *args, **kwargs):
        return self

    def eq(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def execute(self):
        return types.SimpleNamespace(data=self._rows)


class _FakeClient:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def table(self, name):
        return _FakeTable(self._rows)


def _settings_row(**overrides) -> dict:
    return {
        "user_id": "u1",
        "dividend_yield_threshold": 3.0,
        "peg_threshold": 1.0,
        "stale_data_threshold_minutes": 30,
        "theme": "system",
        "data_provider": "dhan",
        "updated_at": None,
        **overrides,
    }


class TestGetUserSettings:
    def test_no_row_returns_defaults(self):
        client = _FakeClient([])

        result = settings_repo.get_user_settings(client, "u1")

        assert result.user_id == "u1"
        assert result.data_provider == "yfinance_bhavcopy"

    def test_valid_data_provider_validates_normally(self):
        client = _FakeClient([_settings_row(data_provider="dhan")])

        result = settings_repo.get_user_settings(client, "u1")

        assert result.data_provider == "dhan"

    def test_legacy_zerodha_data_provider_degrades_to_the_default(self):
        # The real bug: this used to raise pydantic.ValidationError
        # straight out of require_login(), crashing every page for the
        # account, instead of falling back gracefully.
        client = _FakeClient([_settings_row(data_provider="zerodha")])

        result = settings_repo.get_user_settings(client, "u1")

        assert result.data_provider == "yfinance_bhavcopy"
        assert result.user_id == "u1"  # rest of the row still comes through

    def test_an_unrelated_validation_error_still_raises(self):
        # Only the data_provider case is swallowed -- a genuinely
        # unexpected bad row (e.g. missing the required user_id) must
        # still surface loudly, not be silently papered over.
        client = _FakeClient([_settings_row(user_id=None)])

        with pytest.raises(ValidationError):
            settings_repo.get_user_settings(client, "u1")
