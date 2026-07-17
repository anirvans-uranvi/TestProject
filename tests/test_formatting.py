from src.models.enums import AlertType
from src.utils.formatting import alert_type_label, summarize_alert_config


class TestAlertTypeLabel:
    def test_every_alert_type_has_a_label(self):
        for alert_type in AlertType:
            label = alert_type_label(alert_type)
            assert label
            assert label != alert_type.value

    def test_unknown_value_falls_back_to_str(self):
        assert alert_type_label("status_change") == "Status change"


class TestSummarizeAlertConfig:
    def test_price_cross(self):
        summary = summarize_alert_config(AlertType.PRICE_CROSS, {"level": 1000.0, "direction": "above"})
        assert "above" in summary
        assert "1,000.00" in summary

    def test_momentum_cross(self):
        summary = summarize_alert_config(AlertType.MOMENTUM_CROSS, {"period": "5d", "direction": "above_zero"})
        assert "5D" in summary
        assert "above zero" in summary

    def test_dividend_yield_cross(self):
        summary = summarize_alert_config(AlertType.DIVIDEND_YIELD_CROSS, {"threshold": 3.0, "direction": "above"})
        assert "above" in summary
        assert "3.00%" in summary

    def test_peg_cross(self):
        summary = summarize_alert_config(AlertType.PEG_CROSS, {"threshold": 1.0, "direction": "below"})
        assert "below" in summary
        assert "1.0" in summary

    def test_buy_watch(self):
        summary = summarize_alert_config(AlertType.BUY_WATCH, {"entry_price": 500.0})
        assert "500.00" in summary

    def test_sell_watch_with_both_targets(self):
        summary = summarize_alert_config(AlertType.SELL_WATCH, {"target_price": 600.0, "stop_loss": 450.0})
        assert "600.00" in summary
        assert "450.00" in summary

    def test_sell_watch_with_missing_optional_targets(self):
        summary = summarize_alert_config(AlertType.SELL_WATCH, {"target_price": None, "stop_loss": None})
        assert "—" in summary

    def test_status_change_has_no_extra_config(self):
        assert summarize_alert_config(AlertType.STATUS_CHANGE, {}) == "No extra configuration"

    def test_enters_green_has_no_extra_config(self):
        assert summarize_alert_config(AlertType.ENTERS_GREEN, {}) == "No extra configuration"

    def test_leaves_green_has_no_extra_config(self):
        assert summarize_alert_config(AlertType.LEAVES_GREEN, {}) == "No extra configuration"

    def test_refresh_failure_has_no_extra_config(self):
        assert summarize_alert_config(AlertType.REFRESH_FAILURE, {}) == "No extra configuration"
