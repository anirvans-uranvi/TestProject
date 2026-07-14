import pytest

from src.calculations.classification import build_classification, classify, criterion_a, criterion_b, criterion_c
from src.models.enums import ScreenerStatus


class TestCriterionA:
    def test_above_threshold_passes(self):
        assert criterion_a(3.01, 3.0) is True

    def test_exactly_at_threshold_fails(self):
        assert criterion_a(3.0, 3.0) is False

    def test_below_threshold_fails(self):
        assert criterion_a(2.99, 3.0) is False

    def test_missing_returns_none(self):
        assert criterion_a(None, 3.0) is None

    def test_zero_yield_fails_not_missing(self):
        assert criterion_a(0.0, 3.0) is False


class TestCriterionB:
    def test_all_positive_passes(self):
        assert criterion_b(0.1, 0.1, 0.1) is True

    def test_one_zero_fails(self):
        assert criterion_b(0.0, 1.0, 1.0) is False
        assert criterion_b(1.0, 0.0, 1.0) is False
        assert criterion_b(1.0, 1.0, 0.0) is False

    def test_one_negative_fails(self):
        assert criterion_b(-0.01, 1.0, 1.0) is False

    def test_all_zero_fails(self):
        assert criterion_b(0.0, 0.0, 0.0) is False

    def test_any_missing_returns_none(self):
        assert criterion_b(None, 1.0, 1.0) is None
        assert criterion_b(1.0, None, 1.0) is None
        assert criterion_b(1.0, 1.0, None) is None
        assert criterion_b(None, None, None) is None


class TestCriterionC:
    def test_above_threshold_fails(self):
        assert criterion_c(1.01, 1.0) is False

    def test_exactly_at_threshold_passes(self):
        assert criterion_c(1.0, 1.0) is True

    def test_below_threshold_passes(self):
        assert criterion_c(0.99, 1.0) is True

    def test_missing_returns_none(self):
        assert criterion_c(None, 1.0) is None

    def test_negative_peg_passes(self):
        assert criterion_c(-0.5, 1.0) is True


class TestClassify:
    def test_all_pass_is_green(self):
        assert classify(True, True, True) == ScreenerStatus.GREEN

    def test_none_pass_is_red(self):
        assert classify(False, False, False) == ScreenerStatus.RED

    def test_one_pass_is_amber(self):
        assert classify(True, False, False) == ScreenerStatus.AMBER
        assert classify(False, True, False) == ScreenerStatus.AMBER
        assert classify(False, False, True) == ScreenerStatus.AMBER

    def test_two_pass_is_amber(self):
        assert classify(True, True, False) == ScreenerStatus.AMBER
        assert classify(True, False, True) == ScreenerStatus.AMBER
        assert classify(False, True, True) == ScreenerStatus.AMBER

    def test_any_missing_is_unavailable_not_red(self):
        assert classify(None, True, True) == ScreenerStatus.UNAVAILABLE
        assert classify(True, None, True) == ScreenerStatus.UNAVAILABLE
        assert classify(True, True, None) == ScreenerStatus.UNAVAILABLE
        assert classify(None, None, None) == ScreenerStatus.UNAVAILABLE

    def test_missing_does_not_count_as_fail_even_with_other_fails(self):
        # Two explicit fails plus one missing must still be Unavailable,
        # not Red -- missing is never conflated with failed.
        assert classify(False, False, None) == ScreenerStatus.UNAVAILABLE

    def test_stale_forces_unavailable_even_if_all_pass(self):
        assert classify(True, True, True, is_stale=True) == ScreenerStatus.UNAVAILABLE

    def test_all_pass_all_fail_boundary_still_correct_when_not_stale(self):
        assert classify(True, True, True, is_stale=False) == ScreenerStatus.GREEN
        assert classify(False, False, False, is_stale=False) == ScreenerStatus.RED


class TestBuildClassification:
    def test_full_green_row(self):
        result = build_classification(
            ttm_dividend_yield=4.0,
            return_1d=0.5,
            return_5d=1.2,
            return_20d=3.0,
            peg_ratio=0.8,
            latest_price=1000.0,
            pe_ratio=20.0,
        )
        assert result.status == ScreenerStatus.GREEN
        assert result.criterion_a is True
        assert result.criterion_b is True
        assert result.criterion_c is True
        assert result.passed_count == 3
        assert result.data_quality.missing_price is False

    def test_full_red_row(self):
        result = build_classification(
            ttm_dividend_yield=1.0,
            return_1d=-0.5,
            return_5d=-1.2,
            return_20d=-3.0,
            peg_ratio=1.5,
            latest_price=1000.0,
        )
        assert result.status == ScreenerStatus.RED
        assert result.passed_count == 0

    def test_missing_peg_yields_unavailable_with_data_quality_flag(self):
        result = build_classification(
            ttm_dividend_yield=4.0,
            return_1d=0.5,
            return_5d=1.2,
            return_20d=3.0,
            peg_ratio=None,
            latest_price=1000.0,
        )
        assert result.status == ScreenerStatus.UNAVAILABLE
        assert result.data_quality.missing_peg is True

    def test_stale_data_yields_unavailable(self):
        result = build_classification(
            ttm_dividend_yield=4.0,
            return_1d=0.5,
            return_5d=1.2,
            return_20d=3.0,
            peg_ratio=1.5,
            latest_price=1000.0,
            is_stale=True,
            stale_minutes=90.0,
        )
        assert result.status == ScreenerStatus.UNAVAILABLE
        assert result.data_quality.is_stale is True
        assert result.data_quality.stale_minutes == 90.0

    def test_custom_thresholds_applied(self):
        result = build_classification(
            ttm_dividend_yield=5.0,
            return_1d=0.1,
            return_5d=0.1,
            return_20d=0.1,
            peg_ratio=2.0,
            latest_price=1000.0,
            dividend_yield_threshold=6.0,  # 5.0 now fails
            peg_threshold=3.0,  # 2.0 <= 3.0 now passes (default threshold of 1.0 would have failed it)
        )
        assert result.criterion_a is False
        assert result.criterion_c is True
        assert result.status == ScreenerStatus.AMBER  # B and C pass, A fails
