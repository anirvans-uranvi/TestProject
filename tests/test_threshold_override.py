from src.models.enums import ScreenerStatus
from src.models.screener import DataQuality, ScreenerRow
from src.models.user import UserSettings
from src.services.threshold_override import recompute_with_user_thresholds


def make_row(**overrides) -> ScreenerRow:
    defaults = dict(
        symbol="TCS",
        name="Tata Consultancy Services",
        latest_price=4100.0,
        return_1d=0.5,
        return_5d=1.0,
        return_20d=2.0,
        ttm_dividend_yield=4.0,
        pe_ratio=25.0,
        peg_ratio=0.8,
        data_quality=DataQuality(),
    )
    defaults.update(overrides)
    return ScreenerRow(**defaults)


class TestRecomputeWithUserThresholds:
    def test_default_thresholds_match_stored_green(self):
        row = make_row()
        settings = UserSettings(user_id="u1", dividend_yield_threshold=3.0, peg_threshold=1.0)
        result = recompute_with_user_thresholds(row, settings)
        assert result.status == ScreenerStatus.GREEN

    def test_stricter_dividend_threshold_downgrades_to_amber(self):
        row = make_row()  # yield 4.0
        settings = UserSettings(user_id="u1", dividend_yield_threshold=5.0, peg_threshold=1.0)
        result = recompute_with_user_thresholds(row, settings)
        assert result.criterion_a is False
        assert result.status == ScreenerStatus.AMBER

    def test_looser_peg_threshold_can_upgrade_to_green(self):
        row = make_row(peg_ratio=1.5)  # fails at the default 1.0 threshold
        settings = UserSettings(user_id="u1", dividend_yield_threshold=3.0, peg_threshold=2.0)
        result = recompute_with_user_thresholds(row, settings)
        assert result.criterion_c is True
        assert result.status == ScreenerStatus.GREEN

    def test_custom_stale_threshold_forces_unavailable(self):
        row = make_row(data_quality=DataQuality(stale_minutes=45.0))
        settings = UserSettings(user_id="u1", stale_data_threshold_minutes=30)
        result = recompute_with_user_thresholds(row, settings)
        assert result.status == ScreenerStatus.UNAVAILABLE
        assert result.data_quality.is_stale is True

    def test_custom_stale_threshold_can_undo_staleness(self):
        row = make_row(data_quality=DataQuality(stale_minutes=20.0))
        settings = UserSettings(user_id="u1", stale_data_threshold_minutes=30)
        result = recompute_with_user_thresholds(row, settings)
        assert result.data_quality.is_stale is False
        assert result.status == ScreenerStatus.GREEN

    def test_does_not_mutate_original_row(self):
        row = make_row()
        settings = UserSettings(user_id="u1", dividend_yield_threshold=99.0)
        recompute_with_user_thresholds(row, settings)
        assert row.criterion_a is None  # original untouched
