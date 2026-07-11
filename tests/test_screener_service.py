from datetime import date

from src.models.enums import DividendType, ScreenerStatus
from src.models.market_data import DividendEvent
from src.services.screener_service import compute_screener_row


def make_event(ex_date: date, amount: float) -> DividendEvent:
    return DividendEvent(symbol="TCS", ex_date=ex_date, amount_per_share=amount, dividend_type=DividendType.FINAL)


class TestComputeScreenerRow:
    def test_green_row(self):
        closes = [3900.0] * 19 + [3950.0]  # 20 values; latest 4100 > all -> positive returns
        row = compute_screener_row(
            symbol="TCS",
            latest_price=4100.0,
            historical_closes=closes,
            dividend_events=[make_event(date(2026, 2, 1), 150.0)],
            pe_ratio=25.0,
            peg_ratio=1.8,
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

    def test_red_row_no_dividends_negative_momentum_low_peg(self):
        closes = [4200.0] * 20  # latest lower than base -> negative returns
        row = compute_screener_row(
            symbol="TCS",
            latest_price=4000.0,
            historical_closes=closes,
            dividend_events=[],  # confirmed zero dividends -> criterion A fails, not unavailable
            pe_ratio=25.0,
            peg_ratio=0.5,
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
            peg_threshold=5.0,  # 1.8 now fails too
        )
        assert row.criterion_a is False
        assert row.criterion_c is False
        assert row.status == ScreenerStatus.AMBER  # only B passes
