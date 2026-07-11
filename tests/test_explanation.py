from src.models.enums import ScreenerStatus
from src.models.screener import DataQuality, ScreenerRow
from src.services.explanation import explain_classification


def make_row(**overrides):
    defaults = dict(
        symbol="TCS", name="Tata Consultancy Services", latest_price=4100.0,
        return_1d=0.5, return_5d=1.0, return_20d=2.0, ttm_dividend_yield=4.0,
        pe_ratio=25.0, peg_ratio=1.5, criterion_a=True, criterion_b=True, criterion_c=True,
        status=ScreenerStatus.GREEN, data_quality=DataQuality(),
    )
    defaults.update(overrides)
    return ScreenerRow(**defaults)


def test_green_explanation_mentions_all_pass():
    row = make_row()
    text = explain_classification(row)
    assert "Green" in text
    assert "Tata Consultancy Services" in text


def test_unavailable_explanation_cites_missing_peg():
    row = make_row(
        status=ScreenerStatus.UNAVAILABLE, criterion_c=None, peg_ratio=None,
        data_quality=DataQuality(missing_peg=True),
    )
    text = explain_classification(row)
    assert "Unavailable" in text
    assert "PEG" in text


def test_red_explanation_mentions_no_criteria_pass():
    row = make_row(status=ScreenerStatus.RED, criterion_a=False, criterion_b=False, criterion_c=False)
    text = explain_classification(row)
    assert "Red" in text
    assert "none" in text.lower()
