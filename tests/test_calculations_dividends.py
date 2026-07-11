from datetime import date

import pytest

from src.calculations.dividends import ttm_dividend_sum, ttm_dividend_yield
from src.models.enums import DividendType
from src.models.market_data import DividendEvent


def make_event(symbol: str, ex_date: date, amount: float) -> DividendEvent:
    return DividendEvent(symbol=symbol, ex_date=ex_date, amount_per_share=amount, dividend_type=DividendType.FINAL)


class TestTtmDividendSum:
    def test_sums_events_within_window(self):
        as_of = date(2026, 7, 11)
        events = [
            make_event("TCS", date(2025, 8, 1), 10.0),
            make_event("TCS", date(2026, 2, 1), 8.0),
        ]
        assert ttm_dividend_sum(events, as_of) == 18.0

    def test_excludes_events_outside_window(self):
        as_of = date(2026, 7, 11)
        events = [
            make_event("TCS", date(2025, 6, 1), 10.0),  # > 365 days before as_of
            make_event("TCS", date(2026, 2, 1), 8.0),
        ]
        assert ttm_dividend_sum(events, as_of) == 8.0

    def test_empty_events_sums_to_zero(self):
        assert ttm_dividend_sum([], date(2026, 7, 11)) == 0.0

    def test_boundary_exactly_365_days_included(self):
        as_of = date(2026, 7, 11)
        window_start = as_of.replace(year=as_of.year - 1)  # ~365 days back
        events = [make_event("TCS", window_start, 5.0)]
        assert ttm_dividend_sum(events, as_of) == 5.0


class TestTtmDividendYield:
    def test_computes_percentage(self):
        as_of = date(2026, 7, 11)
        events = [make_event("TCS", date(2026, 2, 1), 30.0)]
        # 30 / 1500 * 100 = 2.0
        assert ttm_dividend_yield(events, as_of, 1500.0) == 2.0

    def test_no_dividends_is_confirmed_zero_not_none(self):
        as_of = date(2026, 7, 11)
        assert ttm_dividend_yield([], as_of, 1500.0) == 0.0

    def test_missing_price_returns_none(self):
        as_of = date(2026, 7, 11)
        events = [make_event("TCS", date(2026, 2, 1), 30.0)]
        assert ttm_dividend_yield(events, as_of, None) is None

    def test_zero_price_returns_none(self):
        as_of = date(2026, 7, 11)
        assert ttm_dividend_yield([], as_of, 0.0) is None

    def test_negative_price_returns_none(self):
        as_of = date(2026, 7, 11)
        assert ttm_dividend_yield([], as_of, -5.0) is None

    def test_yield_exactly_at_threshold_boundary(self):
        as_of = date(2026, 7, 11)
        events = [make_event("TCS", date(2026, 2, 1), 30.0)]
        # 30 / 1000 * 100 = 3.0 exactly
        assert ttm_dividend_yield(events, as_of, 1000.0) == pytest.approx(3.0)
