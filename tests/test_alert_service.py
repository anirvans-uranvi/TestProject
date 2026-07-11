from datetime import date, datetime, timedelta

import pytz

from src.models.alert import Alert
from src.models.enums import AlertType, ScreenerStatus
from src.models.screener import DailyScreenerSnapshot
from src.services.alert_service import evaluate_alert, evaluate_alerts

IST = pytz.timezone("Asia/Kolkata")
NOW = IST.localize(datetime(2026, 7, 11, 10, 0))


def snap(symbol="TCS", status=ScreenerStatus.GREEN, price=4100.0, r1=0.5, r5=1.0, r20=2.0, yld=4.0, peg=1.5, d=date(2026, 7, 11)):
    return DailyScreenerSnapshot(
        symbol=symbol, snapshot_date=d, latest_price=price, return_1d=r1, return_5d=r5, return_20d=r20,
        ttm_dividend_yield=yld, pe_ratio=25.0, peg_ratio=peg, criterion_a=True, criterion_b=True, criterion_c=True,
        status=status,
    )


def make_alert(alert_type: AlertType, config: dict | None = None, is_active=True, cooldown_minutes=60, last_triggered_at=None):
    return Alert(
        id="alert-1", user_id="user-1", symbol="TCS", alert_type=alert_type, config=config or {},
        is_active=is_active, cooldown_minutes=cooldown_minutes, last_triggered_at=last_triggered_at,
    )


class TestStatusChangeAlerts:
    def test_triggers_on_any_status_change(self):
        alert = make_alert(AlertType.STATUS_CHANGE)
        prev = snap(status=ScreenerStatus.AMBER)
        curr = snap(status=ScreenerStatus.GREEN)
        event = evaluate_alert(alert, curr, prev, "Tata Consultancy Services", NOW)
        assert event is not None
        assert "AMBER" in event.message and "GREEN" in event.message

    def test_no_trigger_when_status_unchanged(self):
        alert = make_alert(AlertType.STATUS_CHANGE)
        prev = snap(status=ScreenerStatus.GREEN)
        curr = snap(status=ScreenerStatus.GREEN)
        assert evaluate_alert(alert, curr, prev, "TCS", NOW) is None

    def test_no_trigger_without_previous_snapshot(self):
        alert = make_alert(AlertType.STATUS_CHANGE)
        curr = snap(status=ScreenerStatus.GREEN)
        assert evaluate_alert(alert, curr, None, "TCS", NOW) is None


class TestEntersLeavesGreen:
    def test_enters_green(self):
        alert = make_alert(AlertType.ENTERS_GREEN)
        prev = snap(status=ScreenerStatus.AMBER)
        curr = snap(status=ScreenerStatus.GREEN)
        assert evaluate_alert(alert, curr, prev, "TCS", NOW) is not None

    def test_does_not_trigger_staying_green(self):
        alert = make_alert(AlertType.ENTERS_GREEN)
        prev = snap(status=ScreenerStatus.GREEN)
        curr = snap(status=ScreenerStatus.GREEN)
        assert evaluate_alert(alert, curr, prev, "TCS", NOW) is None

    def test_leaves_green(self):
        alert = make_alert(AlertType.LEAVES_GREEN)
        prev = snap(status=ScreenerStatus.GREEN)
        curr = snap(status=ScreenerStatus.RED)
        assert evaluate_alert(alert, curr, prev, "TCS", NOW) is not None


class TestPriceCross:
    def test_crosses_above_threshold(self):
        alert = make_alert(AlertType.PRICE_CROSS, {"level": 4000, "direction": "above"})
        prev = snap(price=3950.0)
        curr = snap(price=4050.0)
        assert evaluate_alert(alert, curr, prev, "TCS", NOW) is not None

    def test_does_not_cross_when_staying_above(self):
        alert = make_alert(AlertType.PRICE_CROSS, {"level": 4000, "direction": "above"})
        prev = snap(price=4050.0)
        curr = snap(price=4100.0)
        assert evaluate_alert(alert, curr, prev, "TCS", NOW) is None

    def test_crosses_below_threshold(self):
        alert = make_alert(AlertType.PRICE_CROSS, {"level": 4000, "direction": "below"})
        prev = snap(price=4050.0)
        curr = snap(price=3950.0)
        assert evaluate_alert(alert, curr, prev, "TCS", NOW) is not None


class TestMomentumCross:
    def test_1d_crosses_above_zero(self):
        alert = make_alert(AlertType.MOMENTUM_CROSS, {"period": "1d", "direction": "above_zero"})
        prev = snap(r1=-0.5)
        curr = snap(r1=0.5)
        assert evaluate_alert(alert, curr, prev, "TCS", NOW) is not None

    def test_1d_crosses_below_zero(self):
        alert = make_alert(AlertType.MOMENTUM_CROSS, {"period": "1d", "direction": "below_zero"})
        prev = snap(r1=0.5)
        curr = snap(r1=-0.5)
        assert evaluate_alert(alert, curr, prev, "TCS", NOW) is not None

    def test_no_trigger_when_no_previous(self):
        alert = make_alert(AlertType.MOMENTUM_CROSS, {"period": "1d", "direction": "above_zero"})
        curr = snap(r1=0.5)
        assert evaluate_alert(alert, curr, None, "TCS", NOW) is None


class TestDividendYieldAndPegCross:
    def test_dividend_yield_crosses_above(self):
        alert = make_alert(AlertType.DIVIDEND_YIELD_CROSS, {"threshold": 3.0, "direction": "above"})
        prev = snap(yld=2.5)
        curr = snap(yld=3.5)
        assert evaluate_alert(alert, curr, prev, "TCS", NOW) is not None

    def test_peg_crosses_below(self):
        alert = make_alert(AlertType.PEG_CROSS, {"threshold": 1.0, "direction": "below"})
        prev = snap(peg=1.2)
        curr = snap(peg=0.8)
        assert evaluate_alert(alert, curr, prev, "TCS", NOW) is not None


class TestBuyWatch:
    def test_triggers_when_green_and_at_or_below_entry(self):
        alert = make_alert(AlertType.BUY_WATCH, {"entry_price": 4200.0})
        curr = snap(status=ScreenerStatus.GREEN, price=4150.0)
        event = evaluate_alert(alert, curr, None, "TCS", NOW)
        assert event is not None
        assert "Model Buy Watch" in event.message

    def test_no_trigger_when_above_entry(self):
        alert = make_alert(AlertType.BUY_WATCH, {"entry_price": 4000.0})
        curr = snap(status=ScreenerStatus.GREEN, price=4150.0)
        assert evaluate_alert(alert, curr, None, "TCS", NOW) is None

    def test_no_trigger_when_not_green(self):
        alert = make_alert(AlertType.BUY_WATCH, {"entry_price": 4200.0})
        curr = snap(status=ScreenerStatus.AMBER, price=4150.0)
        assert evaluate_alert(alert, curr, None, "TCS", NOW) is None


class TestSellWatch:
    def test_triggers_on_status_drop_from_green(self):
        alert = make_alert(AlertType.SELL_WATCH, {})
        prev = snap(status=ScreenerStatus.GREEN)
        curr = snap(status=ScreenerStatus.RED)
        event = evaluate_alert(alert, curr, prev, "TCS", NOW)
        assert event is not None
        assert "Model Exit / Review" in event.message

    def test_triggers_on_stop_loss(self):
        alert = make_alert(AlertType.SELL_WATCH, {"stop_loss": 4000.0})
        curr = snap(status=ScreenerStatus.GREEN, price=3950.0)
        event = evaluate_alert(alert, curr, None, "TCS", NOW)
        assert event is not None
        assert "stop-loss" in event.message

    def test_triggers_on_target_reached(self):
        alert = make_alert(AlertType.SELL_WATCH, {"target_price": 4500.0})
        curr = snap(status=ScreenerStatus.GREEN, price=4600.0)
        event = evaluate_alert(alert, curr, None, "TCS", NOW)
        assert event is not None
        assert "target" in event.message


class TestCooldownAndDedupe:
    def test_cooldown_suppresses_repeat_trigger(self):
        alert = make_alert(
            AlertType.ENTERS_GREEN, cooldown_minutes=60, last_triggered_at=NOW - timedelta(minutes=10)
        )
        prev = snap(status=ScreenerStatus.AMBER)
        curr = snap(status=ScreenerStatus.GREEN)
        assert evaluate_alert(alert, curr, prev, "TCS", NOW) is None

    def test_triggers_again_after_cooldown_elapses(self):
        alert = make_alert(
            AlertType.ENTERS_GREEN, cooldown_minutes=60, last_triggered_at=NOW - timedelta(minutes=90)
        )
        prev = snap(status=ScreenerStatus.AMBER)
        curr = snap(status=ScreenerStatus.GREEN)
        assert evaluate_alert(alert, curr, prev, "TCS", NOW) is not None

    def test_inactive_alert_never_triggers(self):
        alert = make_alert(AlertType.ENTERS_GREEN, is_active=False)
        prev = snap(status=ScreenerStatus.AMBER)
        curr = snap(status=ScreenerStatus.GREEN)
        assert evaluate_alert(alert, curr, prev, "TCS", NOW) is None

    def test_dedupe_key_stable_for_same_day(self):
        alert = make_alert(AlertType.ENTERS_GREEN)
        prev = snap(status=ScreenerStatus.AMBER)
        curr = snap(status=ScreenerStatus.GREEN)
        e1 = evaluate_alert(alert, curr, prev, "TCS", NOW)
        e2 = evaluate_alert(alert, curr, prev, "TCS", NOW + timedelta(hours=1))
        assert e1.dedupe_key == e2.dedupe_key

    def test_dedupe_key_differs_across_days(self):
        alert = make_alert(AlertType.ENTERS_GREEN)
        prev = snap(status=ScreenerStatus.AMBER)
        curr = snap(status=ScreenerStatus.GREEN)
        e1 = evaluate_alert(alert, curr, prev, "TCS", NOW)
        e2 = evaluate_alert(alert, curr, prev, "TCS", NOW + timedelta(days=1))
        assert e1.dedupe_key != e2.dedupe_key


class TestEvaluateAlertsBatch:
    def test_multiple_alerts_can_all_fire(self):
        alerts = [
            make_alert(AlertType.STATUS_CHANGE),
            make_alert(AlertType.ENTERS_GREEN),
        ]
        prev = snap(status=ScreenerStatus.AMBER)
        curr = snap(status=ScreenerStatus.GREEN)
        events = evaluate_alerts(alerts, curr, prev, "TCS", NOW)
        assert len(events) == 2

    def test_only_matching_conditions_fire(self):
        alerts = [
            make_alert(AlertType.STATUS_CHANGE),
            make_alert(AlertType.PRICE_CROSS, {"level": 100000, "direction": "above"}),
        ]
        prev = snap(status=ScreenerStatus.AMBER, price=4000.0)
        curr = snap(status=ScreenerStatus.GREEN, price=4050.0)
        events = evaluate_alerts(alerts, curr, prev, "TCS", NOW)
        assert len(events) == 1
        assert events[0].alert_type == AlertType.STATUS_CHANGE
