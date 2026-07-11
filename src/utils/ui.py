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
