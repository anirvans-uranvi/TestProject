import pytest

from src.calculations.returns import (
    pct_return,
    return_1d,
    return_5d,
    return_20d,
    return_n_trading_days_ago,
    value_change_from_pct,
)


class TestPctReturn:
    def test_positive_return(self):
        assert pct_return(110, 100) == pytest.approx(10.0)

    def test_negative_return(self):
        assert pct_return(90, 100) == pytest.approx(-10.0)

    def test_zero_return_exactly(self):
        assert pct_return(100, 100) == 0.0

    def test_none_latest_returns_none(self):
        assert pct_return(None, 100) is None

    def test_none_base_returns_none(self):
        assert pct_return(100, None) is None

    def test_zero_base_returns_none(self):
        assert pct_return(100, 0) is None


class TestReturnNTradingDaysAgo:
    def test_exact_window(self):
        closes = [90, 91, 92, 93, 94]  # oldest -> newest
        assert return_n_trading_days_ago(100, closes, 1) == pct_return(100, 94)
        assert return_n_trading_days_ago(100, closes, 5) == pct_return(100, 90)

    def test_insufficient_history_returns_none(self):
        closes = [92, 93, 94]
        assert return_n_trading_days_ago(100, closes, 5) is None

    def test_missing_latest_price_returns_none(self):
        closes = [90, 91, 92, 93, 94]
        assert return_n_trading_days_ago(None, closes, 1) is None

    def test_base_value_none_in_series_returns_none(self):
        closes = [None, 91, 92, 93, 94]
        assert return_n_trading_days_ago(100, closes, 5) is None

    def test_n_zero_returns_none(self):
        assert return_n_trading_days_ago(100, [90, 91], 0) is None


class TestConvenienceWrappers:
    closes = list(range(80, 100))  # 20 values, oldest -> newest, last = 99

    def test_return_1d(self):
        assert self.closes[-1] == 99
        assert return_1d(100, self.closes) == pct_return(100, 99)

    def test_return_5d(self):
        assert return_5d(100, self.closes) == pct_return(100, self.closes[-5])

    def test_return_20d(self):
        assert return_20d(100, self.closes) == pct_return(100, self.closes[-20])

    def test_return_20d_insufficient_history(self):
        assert return_20d(100, self.closes[:19]) is None


class TestValueChangeFromPct:
    def test_inverts_pct_return_for_a_positive_move(self):
        # base 100 -> current 110 is a +10% return; the implied change
        # over current_value=110 must recover the same +10 gap.
        pct = pct_return(110, 100)
        assert value_change_from_pct(110, pct) == pytest.approx(10.0)

    def test_inverts_pct_return_for_a_negative_move(self):
        pct = pct_return(90, 100)
        assert value_change_from_pct(90, pct) == pytest.approx(-10.0)

    def test_zero_pct_is_zero_change(self):
        assert value_change_from_pct(100, 0.0) == pytest.approx(0.0)

    def test_scales_linearly_with_current_value(self):
        # Same pct return but current_value is a whole position's cur_val
        # (qty * ltp) rather than a per-share price -- the formula must
        # give the proportional total-value change, not just a per-share one.
        pct = pct_return(110, 100)
        assert value_change_from_pct(1100, pct) == pytest.approx(100.0)

    def test_none_current_value_returns_none(self):
        assert value_change_from_pct(None, 5.0) is None

    def test_none_pct_returns_none(self):
        assert value_change_from_pct(100, None) is None

    def test_pct_of_minus_100_returns_none_not_a_zero_division_crash(self):
        assert value_change_from_pct(100, -100.0) is None
