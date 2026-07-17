from datetime import date

from src.models.enums import DividendType, ScreenerStatus
from src.models.market_data import DividendEvent, PricePoint
from src.services.screener_service import compute_screener_row, valid_closes


def make_event(ex_date: date, amount: float) -> DividendEvent:
    return DividendEvent(symbol="TCS", ex_date=ex_date, amount_per_share=amount, dividend_type=DividendType.FINAL)


def make_point(trade_date: date, close: float | None) -> PricePoint:
    return PricePoint(symbol="TCS", trade_date=trade_date, close=close, adjusted_close=close)


class TestValidCloses:
    def test_drops_gap_days_preserving_order(self):
        history = [
            make_point(date(2026, 7, 13), 100.0),
            make_point(date(2026, 7, 14), 101.0),
            make_point(date(2026, 7, 16), None),  # e.g. an NSE holiday Yahoo still timestamps
            make_point(date(2026, 7, 17), 103.0),
        ]
        assert valid_closes(history) == [100.0, 101.0, 103.0]

    def test_no_gaps_returns_all_closes(self):
        history = [make_point(date(2026, 7, d), float(d)) for d in range(13, 18)]
        assert valid_closes(history) == [13.0, 14.0, 15.0, 16.0, 17.0]

    def test_empty_history_returns_empty_list(self):
        assert valid_closes([]) == []

    def test_gap_at_the_most_recent_position_no_longer_breaks_1d_lookup(self):
        # This is the exact real-world scenario: a phantom holiday row
        # lands right before the latest price, so a NAIVE historical_closes
        # (without gap-filtering) would put None at [-1] and make return_1d
        # Unavailable even though a real previous close exists further back.
        history = [
            make_point(date(2026, 7, 13), 3177.5),
            make_point(date(2026, 7, 14), 3188.8999),
            make_point(date(2026, 7, 15), 3150.6001),
            make_point(date(2026, 7, 16), None),  # phantom holiday row
        ]
        closes = valid_closes(history)
        row = compute_screener_row(
            symbol="TCS", latest_price=3153.8999, historical_closes=closes, dividend_events=[],
            pe_ratio=25.0, peg_ratio=0.8, as_of_date=date(2026, 7, 17),
        )
        assert row.return_1d is not None
        assert row.data_quality.missing_return_1d is False


class TestComputeScreenerRow:
    def test_green_row(self):
        closes = [3900.0] * 19 + [3950.0]  # 20 values; latest 4100 > all -> positive returns
        row = compute_screener_row(
            symbol="TCS",
            latest_price=4100.0,
            historical_closes=closes,
            dividend_events=[make_event(date(2026, 2, 1), 150.0)],
            pe_ratio=25.0,
            peg_ratio=0.8,
            as_of_date=date(2026, 7, 11),
        )
        assert row.status == ScreenerStatus.GREEN
        assert row.criterion_a is True
        assert row.criterion_b is True
        assert row.criterion_c is True

    def test_unavailable_when_history_insufficient(self):
        row = compute_screener_row(
            symbol="TCS",
            latest_price=4100.0,
            historical_closes=[3900.0, 3950.0],  # not enough for 20d return
            dividend_events=[],
            pe_ratio=25.0,
            peg_ratio=1.8,
            as_of_date=date(2026, 7, 11),
        )
        assert row.status == ScreenerStatus.UNAVAILABLE
        assert row.data_quality.missing_return_20d is True

    def test_unavailable_when_peg_missing(self):
        closes = [3900.0] * 20
        row = compute_screener_row(
            symbol="TCS",
            latest_price=4100.0,
            historical_closes=closes,
            dividend_events=[],
            pe_ratio=25.0,
            peg_ratio=None,
            as_of_date=date(2026, 7, 11),
        )
        assert row.status == ScreenerStatus.UNAVAILABLE
        assert row.data_quality.missing_peg is True

    def test_red_row_no_dividends_negative_momentum_high_peg(self):
        closes = [4200.0] * 20  # latest lower than base -> negative returns
        row = compute_screener_row(
            symbol="TCS",
            latest_price=4000.0,
            historical_closes=closes,
            dividend_events=[],  # confirmed zero dividends -> criterion A fails, not unavailable
            pe_ratio=25.0,
            peg_ratio=1.5,
            as_of_date=date(2026, 7, 11),
        )
        assert row.status == ScreenerStatus.RED
        assert row.criterion_a is False
        assert row.criterion_b is False
        assert row.criterion_c is False

    def test_stale_forces_unavailable(self):
        closes = [3900.0] * 20
        row = compute_screener_row(
            symbol="TCS",
            latest_price=4100.0,
            historical_closes=closes,
            dividend_events=[make_event(date(2026, 2, 1), 150.0)],
            pe_ratio=25.0,
            peg_ratio=1.8,
            as_of_date=date(2026, 7, 11),
            is_stale=True,
        )
        assert row.status == ScreenerStatus.UNAVAILABLE
        assert row.data_quality.is_stale is True

    def test_52w_high_low_criteria_computed(self):
        closes = [3900.0] * 20
        row = compute_screener_row(
            symbol="TCS",
            latest_price=4100.0,
            historical_closes=closes,
            dividend_events=[],
            pe_ratio=25.0,
            peg_ratio=1.8,
            as_of_date=date(2026, 7, 11),
            week_52_high=5000.0,  # 4100 < 0.9*5000=4500 -> pass
            week_52_low=3000.0,  # 4100 > 1.1*3000=3300 -> pass
        )
        assert row.week_52_high == 5000.0
        assert row.week_52_low == 3000.0
        assert row.criterion_52w_high is True
        assert row.criterion_52w_low is True

    def test_52w_high_low_criteria_fail_near_high_and_low(self):
        closes = [3900.0] * 20
        row = compute_screener_row(
            symbol="TCS",
            latest_price=4100.0,
            historical_closes=closes,
            dividend_events=[],
            pe_ratio=25.0,
            peg_ratio=1.8,
            as_of_date=date(2026, 7, 11),
            week_52_high=4300.0,  # 4100 >= 0.9*4300=3870 -> fail
            week_52_low=4000.0,  # 4100 <= 1.1*4000=4400 -> fail
        )
        assert row.criterion_52w_high is False
        assert row.criterion_52w_low is False

    def test_52w_high_low_missing_returns_none_not_fail(self):
        closes = [3900.0] * 20
        row = compute_screener_row(
            symbol="TCS",
            latest_price=4100.0,
            historical_closes=closes,
            dividend_events=[],
            pe_ratio=25.0,
            peg_ratio=1.8,
            as_of_date=date(2026, 7, 11),
        )
        assert row.week_52_high is None
        assert row.criterion_52w_high is None
        assert row.criterion_52w_low is None

    def test_custom_thresholds(self):
        closes = [3900.0] * 20
        row = compute_screener_row(
            symbol="TCS",
            latest_price=4100.0,
            historical_closes=closes,
            dividend_events=[make_event(date(2026, 2, 1), 400.0)],  # big yield
            pe_ratio=25.0,
            peg_ratio=1.8,
            as_of_date=date(2026, 7, 11),
            dividend_yield_threshold=20.0,  # very high bar, should fail now
            peg_threshold=5.0,  # 1.8 <= 5.0 now passes (default threshold of 1.0 would have failed it)
        )
        assert row.criterion_a is False
        assert row.criterion_c is True
        assert row.status == ScreenerStatus.AMBER  # B and C pass, A fails
