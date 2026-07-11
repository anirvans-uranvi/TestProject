"""Alert evaluation: pure function over two consecutive snapshots + a
user's active alert configs. No Supabase I/O here -- callers persist the
returned events (notification_repo.insert_notification, which enforces
dedupe via a unique constraint on dedupe_key) and update
alerts.last_triggered_at.
"""
from __future__ import annotations

import hashlib
from datetime import datetime

from src.models.alert import Alert, NotificationEvent
from src.models.enums import AlertType, ScreenerStatus
from src.models.screener import DailyScreenerSnapshot


def _dedupe_key(alert_id: str | None, symbol: str | None, alert_type: AlertType, as_of: datetime) -> str:
    raw = f"{alert_id}|{symbol}|{alert_type}|{as_of.date().isoformat()}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _cooled_down(alert: Alert, now: datetime) -> bool:
    if alert.last_triggered_at is None:
        return True
    elapsed_minutes = (now - alert.last_triggered_at).total_seconds() / 60
    return elapsed_minutes >= alert.cooldown_minutes


def _crossed_above(prev: float | None, curr: float | None, threshold: float) -> bool:
    return prev is not None and curr is not None and prev <= threshold < curr


def _crossed_below(prev: float | None, curr: float | None, threshold: float) -> bool:
    return prev is not None and curr is not None and prev >= threshold > curr


def _momentum_value(snapshot: DailyScreenerSnapshot, period: str) -> float | None:
    return {"1d": snapshot.return_1d, "5d": snapshot.return_5d, "20d": snapshot.return_20d}.get(period)


def _make_event(
    alert: Alert,
    symbol: str | None,
    stock_name: str | None,
    message: str,
    current: DailyScreenerSnapshot | None,
    relevant_values: dict,
    status_change: str | None,
    now: datetime,
) -> NotificationEvent:
    return NotificationEvent(
        alert_id=alert.id,
        user_id=alert.user_id,
        symbol=symbol,
        stock_name=stock_name,
        alert_type=alert.alert_type,
        message=message,
        current_price=current.latest_price if current else None,
        relevant_values=relevant_values,
        status_change=status_change,
        triggered_at=now,
        dedupe_key=_dedupe_key(alert.id, symbol, alert.alert_type, now),
    )


def evaluate_alert(
    alert: Alert,
    current: DailyScreenerSnapshot,
    previous: DailyScreenerSnapshot | None,
    stock_name: str,
    now: datetime,
) -> NotificationEvent | None:
    """Checks one active alert against one symbol's latest snapshot pair."""
    if not alert.is_active or not _cooled_down(alert, now):
        return None

    symbol = current.symbol
    cfg = alert.config or {}

    if alert.alert_type == AlertType.STATUS_CHANGE:
        if previous is not None and current.status != previous.status:
            return _make_event(
                alert, symbol, stock_name,
                f"{stock_name} ({symbol}) status changed from {previous.status.upper()} to {current.status.upper()}",
                current, {"previous_status": previous.status, "current_status": current.status},
                f"{previous.status} -> {current.status}", now,
            )

    elif alert.alert_type == AlertType.ENTERS_GREEN:
        if previous is not None and previous.status != ScreenerStatus.GREEN and current.status == ScreenerStatus.GREEN:
            return _make_event(
                alert, symbol, stock_name, f"{stock_name} ({symbol}) entered GREEN status",
                current, {"previous_status": previous.status}, f"{previous.status} -> green", now,
            )

    elif alert.alert_type == AlertType.LEAVES_GREEN:
        if previous is not None and previous.status == ScreenerStatus.GREEN and current.status != ScreenerStatus.GREEN:
            return _make_event(
                alert, symbol, stock_name, f"{stock_name} ({symbol}) left GREEN status (now {current.status.upper()})",
                current, {"previous_status": previous.status}, f"green -> {current.status}", now,
            )

    elif alert.alert_type == AlertType.PRICE_CROSS:
        level = cfg.get("level")
        direction = cfg.get("direction", "above")
        prev_price = previous.latest_price if previous else None
        if level is not None:
            crossed = (
                _crossed_above(prev_price, current.latest_price, level)
                if direction == "above"
                else _crossed_below(prev_price, current.latest_price, level)
            )
            if crossed:
                return _make_event(
                    alert, symbol, stock_name,
                    f"{stock_name} ({symbol}) price crossed {direction} INR {level}",
                    current, {"level": level, "direction": direction}, None, now,
                )

    elif alert.alert_type == AlertType.MOMENTUM_CROSS:
        period = cfg.get("period", "1d")
        direction = cfg.get("direction", "above_zero")
        prev_val = _momentum_value(previous, period) if previous else None
        curr_val = _momentum_value(current, period)
        crossed = (
            _crossed_above(prev_val, curr_val, 0.0)
            if direction == "above_zero"
            else _crossed_below(prev_val, curr_val, 0.0)
        )
        if crossed:
            return _make_event(
                alert, symbol, stock_name,
                f"{stock_name} ({symbol}) {period} return crossed {direction.replace('_', ' ')} ({curr_val:.2f}%)",
                current, {"period": period, "value": curr_val}, None, now,
            )

    elif alert.alert_type == AlertType.DIVIDEND_YIELD_CROSS:
        threshold = cfg.get("threshold")
        direction = cfg.get("direction", "above")
        prev_val = previous.ttm_dividend_yield if previous else None
        if threshold is not None:
            crossed = (
                _crossed_above(prev_val, current.ttm_dividend_yield, threshold)
                if direction == "above"
                else _crossed_below(prev_val, current.ttm_dividend_yield, threshold)
            )
            if crossed:
                return _make_event(
                    alert, symbol, stock_name,
                    f"{stock_name} ({symbol}) TTM dividend yield crossed {direction} {threshold}%",
                    current, {"threshold": threshold, "value": current.ttm_dividend_yield}, None, now,
                )

    elif alert.alert_type == AlertType.PEG_CROSS:
        threshold = cfg.get("threshold")
        direction = cfg.get("direction", "above")
        prev_val = previous.peg_ratio if previous else None
        if threshold is not None:
            crossed = (
                _crossed_above(prev_val, current.peg_ratio, threshold)
                if direction == "above"
                else _crossed_below(prev_val, current.peg_ratio, threshold)
            )
            if crossed:
                return _make_event(
                    alert, symbol, stock_name,
                    f"{stock_name} ({symbol}) PEG crossed {direction} {threshold}",
                    current, {"threshold": threshold, "value": current.peg_ratio}, None, now,
                )

    elif alert.alert_type == AlertType.BUY_WATCH:
        entry_price = cfg.get("entry_price")
        if (
            entry_price is not None
            and current.status == ScreenerStatus.GREEN
            and current.latest_price is not None
            and current.latest_price <= entry_price
        ):
            return _make_event(
                alert, symbol, stock_name,
                f"Model Buy Watch: {stock_name} ({symbol}) is GREEN and at/below your entry price INR {entry_price}",
                current, {"entry_price": entry_price}, None, now,
            )

    elif alert.alert_type == AlertType.SELL_WATCH:
        target_price = cfg.get("target_price")
        stop_loss = cfg.get("stop_loss")
        price = current.latest_price
        reasons = []
        if current.status in (ScreenerStatus.AMBER, ScreenerStatus.RED) and (
            previous is None or previous.status == ScreenerStatus.GREEN
        ):
            reasons.append(f"status is now {current.status.upper()}")
        if target_price is not None and price is not None and price >= target_price:
            reasons.append(f"price reached target INR {target_price}")
        if stop_loss is not None and price is not None and price <= stop_loss:
            reasons.append(f"price crossed stop-loss INR {stop_loss}")
        if reasons:
            return _make_event(
                alert, symbol, stock_name,
                f"Model Exit / Review: {stock_name} ({symbol}) -- " + "; ".join(reasons),
                current, {"target_price": target_price, "stop_loss": stop_loss}, None, now,
            )

    return None


def evaluate_alerts(
    alerts: list[Alert],
    current: DailyScreenerSnapshot,
    previous: DailyScreenerSnapshot | None,
    stock_name: str,
    now: datetime,
) -> list[NotificationEvent]:
    events = []
    for alert in alerts:
        event = evaluate_alert(alert, current, previous, stock_name, now)
        if event is not None:
            events.append(event)
    return events


def build_refresh_failure_event(alert: Alert, error_message: str, now: datetime) -> NotificationEvent | None:
    if not alert.is_active or not _cooled_down(alert, now):
        return None
    return _make_event(
        alert, None, None, f"Data refresh failed: {error_message}", None, {"error": error_message}, None, now
    )
