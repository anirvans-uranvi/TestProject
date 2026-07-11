from datetime import date, datetime, timedelta

import pytz

from src.models.enums import MarketState
from src.services.market_calendar import (
    IST,
    get_market_state,
    is_trading_day,
    previous_trading_day,
    trading_days_between,
)


class TestIsTradingDay:
    def test_weekday_non_holiday_is_trading_day(self):
        assert is_trading_day(date(2026, 7, 13)) is True  # Monday

    def test_saturday_is_not_trading_day(self):
        assert is_trading_day(date(2026, 7, 11)) is False  # Saturday

    def test_sunday_is_not_trading_day(self):
        assert is_trading_day(date(2026, 7, 12)) is False

    def test_republic_day_holiday(self):
        assert is_trading_day(date(2026, 1, 26)) is False

    def test_christmas_holiday(self):
        assert is_trading_day(date(2026, 12, 25)) is False

    def test_unlisted_year_falls_back_to_weekday_only(self):
        assert is_trading_day(date(2030, 6, 3)) is True  # Monday, no data for 2030


class TestPreviousTradingDay:
    def test_skips_weekend(self):
        # Monday 2026-07-13 -> previous trading day is Friday 2026-07-10
        assert previous_trading_day(date(2026, 7, 13)) == date(2026, 7, 10)

    def test_skips_holiday_and_weekend(self):
        # Tuesday 2026-01-27 -> Monday 2026-01-26 is Republic Day, so Friday 2026-01-23
        assert previous_trading_day(date(2026, 1, 27)) == date(2026, 1, 23)


class TestTradingDaysBetween:
    def test_excludes_weekends_and_holidays(self):
        days = trading_days_between(date(2026, 1, 23), date(2026, 1, 27))
        assert date(2026, 1, 24) not in days  # Saturday
        assert date(2026, 1, 25) not in days  # Sunday
        assert date(2026, 1, 26) not in days  # Republic Day
        assert date(2026, 1, 23) in days
        assert date(2026, 1, 27) in days


class TestGetMarketState:
    def test_weekend_is_closed(self):
        now = IST.localize(datetime(2026, 7, 11, 11, 0))  # Saturday
        assert get_market_state(now=now) == MarketState.CLOSED

    def test_before_pre_open_is_closed(self):
        now = IST.localize(datetime(2026, 7, 13, 8, 30))
        assert get_market_state(now=now) == MarketState.CLOSED

    def test_pre_open_window(self):
        now = IST.localize(datetime(2026, 7, 13, 9, 5))
        assert get_market_state(now=now) == MarketState.PRE_OPEN

    def test_open_with_fresh_data(self):
        now = IST.localize(datetime(2026, 7, 13, 11, 0))
        last_fetch = now - timedelta(minutes=5)
        assert get_market_state(now=now, last_successful_fetch_at=last_fetch, stale_threshold_minutes=30) == MarketState.OPEN

    def test_open_but_stale_data_is_delayed(self):
        now = IST.localize(datetime(2026, 7, 13, 11, 0))
        last_fetch = now - timedelta(minutes=45)
        assert (
            get_market_state(now=now, last_successful_fetch_at=last_fetch, stale_threshold_minutes=30)
            == MarketState.DATA_DELAYED
        )

    def test_open_no_fetch_ever_is_delayed(self):
        now = IST.localize(datetime(2026, 7, 13, 11, 0))
        assert get_market_state(now=now, last_successful_fetch_at=None) == MarketState.DATA_DELAYED

    def test_after_close_is_closed(self):
        now = IST.localize(datetime(2026, 7, 13, 16, 0))
        assert get_market_state(now=now) == MarketState.CLOSED

    def test_holiday_during_market_hours_is_closed(self):
        now = IST.localize(datetime(2026, 12, 25, 11, 0))
        assert get_market_state(now=now) == MarketState.CLOSED
