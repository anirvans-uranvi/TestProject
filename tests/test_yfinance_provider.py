import math

from src.data_providers.yfinance_provider import _finite


class TestFinite:
    def test_passes_through_normal_values(self):
        assert _finite(12.5) == 12.5
        assert _finite(0) == 0.0

    def test_rejects_infinity(self):
        # yfinance derives some ratios itself (e.g. trailingPE = price /
        # trailingEps), so a near-zero denominator can surface as
        # Infinity -- seen for a newly listed stock with trailingEps==0.0.
        assert _finite(float("inf")) is None
        assert _finite(float("-inf")) is None

    def test_rejects_nan(self):
        assert _finite(float("nan")) is None

    def test_rejects_none_and_non_numeric(self):
        assert _finite(None) is None
        assert _finite("n/a") is None

    def test_result_is_never_non_finite(self):
        for value in (float("inf"), float("-inf"), float("nan"), None, 5):
            result = _finite(value)
            assert result is None or math.isfinite(result)
