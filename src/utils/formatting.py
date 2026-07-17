from __future__ import annotations

import math

from src.models.enums import AlertType


def _is_missing(value: float | None) -> bool:
    """True for None AND for float('nan'). Pydantic models correctly use
    None for missing data, but building a pandas DataFrame from a list of
    those models (pages/1_Dashboard.py: pd.DataFrame([r.model_dump() for
    r in rows])) silently converts None to NaN for any column that also
    has real float values elsewhere in the same column -- a real bug this
    caused: rows[i]['return_1d'] is None upstream (correctly missing) but
    reads back as float('nan') from the DataFrame, which `value is None`
    doesn't catch, so it fell through to numeric formatting and rendered
    as the literal string "nan%" instead of a missing-data placeholder.
    Every formatter below checks this instead of a bare `is None`."""
    return value is None or (isinstance(value, float) and math.isnan(value))


def _group_indian(int_str: str) -> str:
    if len(int_str) <= 3:
        return int_str
    last3, rest = int_str[-3:], int_str[:-3]
    parts = []
    while len(rest) > 2:
        parts.insert(0, rest[-2:])
        rest = rest[:-2]
    if rest:
        parts.insert(0, rest)
    return ",".join(parts) + "," + last3


def format_inr(value: float | None, decimals: int = 2) -> str:
    if _is_missing(value):
        return "—"
    sign = "-" if value < 0 else ""
    value = abs(value)
    int_part = int(value)
    int_str = _group_indian(str(int_part))
    if decimals <= 0:
        return f"{sign}₹{int_str}"
    frac_str = f"{value:.{decimals}f}".split(".")[1]
    return f"{sign}₹{int_str}.{frac_str}"


def format_crores(value_in_rupees: float | None) -> str:
    if _is_missing(value_in_rupees):
        return "—"
    crores = value_in_rupees / 1e7
    return f"₹{crores:,.0f} Cr"


def format_pct(value: float | None, decimals: int = 2, signed: bool = True) -> str:
    if _is_missing(value):
        return "—"
    sign = "+" if signed and value > 0 else ""
    return f"{sign}{value:.{decimals}f}%"


def direction_arrow(value: float | None) -> str:
    if _is_missing(value):
        return "—"
    if value > 0:
        return "▲"
    if value < 0:
        return "▼"
    return "▬"


def pass_fail_badge(value: bool | None) -> str:
    if value is None:
        return "N/A"
    return "✅ Pass" if value else "❌ Fail"


def pass_fail_icon(value: bool | None) -> str:
    """Same pass/fail signal as pass_fail_badge() but symbol-only (no
    'Pass'/'Fail' text), for compact table cells."""
    if value is None:
        return "—"
    return "✅" if value else "❌"


_ALERT_TYPE_LABELS = {
    AlertType.STATUS_CHANGE: "Status change",
    AlertType.ENTERS_GREEN: "Enters Green",
    AlertType.LEAVES_GREEN: "Leaves Green",
    AlertType.PRICE_CROSS: "Price cross",
    AlertType.MOMENTUM_CROSS: "Momentum cross",
    AlertType.DIVIDEND_YIELD_CROSS: "Dividend yield cross",
    AlertType.PEG_CROSS: "PEG cross",
    AlertType.BUY_WATCH: "Buy watch",
    AlertType.SELL_WATCH: "Sell watch",
    AlertType.REFRESH_FAILURE: "Refresh failure",
}


def alert_type_label(alert_type: AlertType | str) -> str:
    """Human-readable label for an AlertType, for display (badges/pills)
    rather than the raw enum value string."""
    return _ALERT_TYPE_LABELS.get(AlertType(alert_type), str(alert_type))


def summarize_alert_config(alert_type: AlertType | str, config: dict) -> str:
    """One-line human-readable summary of an alert's config dict --
    replaces literally printing the raw Python dict (f"config={a.config}")
    that both Stock Detail and Alerts previously showed. Matches the
    exact config keys both pages' alert-creation UIs actually write:
    level/direction (price cross), period/direction (momentum cross),
    threshold/direction (dividend yield / PEG cross), entry_price (buy
    watch), target_price/stop_loss (sell watch)."""
    t = AlertType(alert_type)
    if t == AlertType.PRICE_CROSS:
        return f"Price crosses {config.get('direction', '?')} {format_inr(config.get('level'))}"
    if t == AlertType.MOMENTUM_CROSS:
        period = str(config.get("period", "?")).upper()
        direction = str(config.get("direction", "?")).replace("_", " ")
        return f"{period} momentum crosses {direction}"
    if t == AlertType.DIVIDEND_YIELD_CROSS:
        return f"Dividend yield crosses {config.get('direction', '?')} {format_pct(config.get('threshold'), signed=False)}"
    if t == AlertType.PEG_CROSS:
        return f"PEG crosses {config.get('direction', '?')} {config.get('threshold', '?')}"
    if t == AlertType.BUY_WATCH:
        return f"Buy watch at entry {format_inr(config.get('entry_price'))}"
    if t == AlertType.SELL_WATCH:
        target = format_inr(config.get("target_price")) if config.get("target_price") else "—"
        stop = format_inr(config.get("stop_loss")) if config.get("stop_loss") else "—"
        return f"Sell watch — target {target}, stop-loss {stop}"
    return "No extra configuration"
