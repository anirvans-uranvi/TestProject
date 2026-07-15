"""Shared Streamlit UI fragments: status badges, disclaimer, chart theming."""
from __future__ import annotations

import streamlit as st

from src.models.enums import MarketState, ScreenerStatus, Theme

STATUS_STYLE = {
    ScreenerStatus.GREEN: ("#0f9d58", "🟢", "Green"),
    ScreenerStatus.AMBER: ("#f4a623", "🟠", "Amber"),
    ScreenerStatus.RED: ("#d93025", "🔴", "Red"),
    ScreenerStatus.UNAVAILABLE: ("#8a8f98", "⚪", "Unavailable"),
}

MARKET_STATE_LABEL = {
    MarketState.OPEN: "🟢 Open",
    MarketState.PRE_OPEN: "🟡 Pre-open",
    MarketState.CLOSED: "⚪ Closed",
    MarketState.DATA_DELAYED: "🟠 Data Delayed",
}

BUY_SELL_LABEL = {
    ScreenerStatus.GREEN: "Model Buy Watch",
    ScreenerStatus.AMBER: "Model Caution",
    ScreenerStatus.RED: "Model Exit / Review",
    ScreenerStatus.UNAVAILABLE: "Model Unavailable",
}

DISCLAIMER = (
    "This dashboard is an analytical tool, not investment advice. "
    "Verify data and consider your risk tolerance before trading."
)


def status_badge(status: ScreenerStatus) -> str:
    color, icon, label = STATUS_STYLE[ScreenerStatus(status)]
    return (
        f'<span style="background-color:{color}22;color:{color};border:1px solid {color};'
        f'border-radius:6px;padding:2px 8px;font-weight:600;white-space:nowrap;">'
        f"{icon} {label}</span>"
    )


# Custom shapes per status (no single emoji matches these precisely):
# green tick in a green square, blue "!" in an amber circle, white cross
# in a red triangle. Built as small inline SVGs for exact control over
# shape/color rather than relying on font-rendered emoji glyphs.
_STATUS_SVG = {
    ScreenerStatus.GREEN: (
        '<svg width="20" height="20" viewBox="0 0 20 20" xmlns="http://www.w3.org/2000/svg">'
        '<rect x="1" y="1" width="18" height="18" rx="2" fill="#0f9d58"/>'
        '<path d="M5 10.3 L8.3 13.6 L15 6.8" stroke="white" stroke-width="2.2" '
        'fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>'
    ),
    ScreenerStatus.AMBER: (
        '<svg width="20" height="20" viewBox="0 0 20 20" xmlns="http://www.w3.org/2000/svg">'
        '<circle cx="10" cy="10" r="9" fill="#f4a623"/>'
        '<rect x="9" y="4.5" width="2" height="7.5" rx="1" fill="#1a56db"/>'
        '<rect x="9" y="13.5" width="2" height="2" rx="1" fill="#1a56db"/></svg>'
    ),
    ScreenerStatus.RED: (
        '<svg width="20" height="20" viewBox="0 0 20 20" xmlns="http://www.w3.org/2000/svg">'
        '<path d="M10 1 L19 18 L1 18 Z" fill="#d93025" stroke="#d93025" stroke-linejoin="round"/>'
        '<path d="M7 9 L13 15 M13 9 L7 15" stroke="white" stroke-width="2" stroke-linecap="round"/></svg>'
    ),
}


def status_dot(status: ScreenerStatus) -> str:
    """Color-coded shape only, no text label -- for compact table cells
    (e.g. the Dashboard screener table) where the row already carries a
    Criteria column and other context. Use status_badge() instead
    wherever the status needs to stand alone (e.g. the Stock Detail
    header), since spelling it out matters more there for accessibility."""
    status = ScreenerStatus(status)
    _color, icon, label = STATUS_STYLE[status]
    svg = _STATUS_SVG.get(status)
    inner = svg if svg else f'<span style="font-size:1.3em;">{icon}</span>'
    return f'<span title="{label}">{inner}</span>'


def market_state_label(state: MarketState) -> str:
    return MARKET_STATE_LABEL[MarketState(state)]


def buy_sell_label(status: ScreenerStatus) -> str:
    return BUY_SELL_LABEL[ScreenerStatus(status)]


def render_disclaimer() -> None:
    st.warning(DISCLAIMER, icon="⚠️")


def plotly_template(theme: Theme | str = Theme.SYSTEM) -> str:
    theme = Theme(theme)
    if theme == Theme.DARK:
        return "plotly_dark"
    return "plotly_white"
