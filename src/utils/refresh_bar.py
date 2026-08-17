"""Shared "refresh data" bar shown at a consistent location -- right
after the page title/disclaimer -- on every page (Dashboard, Stock
Detail, Options, My Broker, My Trades, My Holdings, My Positions,
Settings), so a user never has to navigate back to one specific page just
to trigger a data refresh. Three buttons:
Stock Data Refresh (Yahoo Finance, via the manual-refresh Edge Function),
NSE F&O Data Refresh, and BSE F&O Data Refresh (both via the same
fo-refresh Edge Function, parameterized by exchange -- see
src/services/edge_refresh.py and supabase/functions/fo-refresh/index.ts's
own docstring for why one function serves both).

A click always calls `st.cache_data.clear()` -- the *entire* app-wide
cache, not just the current page's -- before `st.rerun()`-ing, so every
page's own `@st.cache_data` loaders re-fetch fresh data regardless of
which page the click happened on. That's what makes "available on all
pages" actually mean something, rather than just refreshing data the
current page happens to read.
"""
from __future__ import annotations

import streamlit as st

from src.models.enums import CompanyType
from src.repositories import companies_repo, fetch_log_repo
from src.services import edge_refresh
from src.utils.timezones import format_ist

_NSE_FO_PROVIDER = "fo_edge_nse"
_BSE_FO_PROVIDER = "fo_edge_bse"


def _last_fetch_caption(client, label: str, fetch_type: str | list[str], provider_name: str | None = None) -> str:
    entry = fetch_log_repo.get_last_successful_fetch(client, fetch_type, provider_name)
    when = format_ist(entry.finished_at) if entry else "never"
    return f"{label}: {when}"


def _universe_breakdown(client) -> str:
    """Same "(X stocks, Y ETFs/funds)" breakdown the stock-refresh message
    has always shown (see git history of pages/1_Dashboard.py) -- moved
    here so it stays identical regardless of which page triggered the
    refresh."""
    companies = companies_repo.list_all_companies(client)
    stock_count = sum(1 for c in companies if c.company_type == CompanyType.EQUITY)
    etf_count = sum(1 for c in companies if c.company_type == CompanyType.ETF)
    return f" ({stock_count} stocks, {etf_count} ETFs/funds)" if etf_count else ""


def _run(key: str, spinner_text: str, action) -> None:
    with st.spinner(spinner_text):
        try:
            summary = action()
        except edge_refresh.ManualRefreshError as exc:
            st.session_state[f"_refresh_bar_{key}"] = {"error": str(exc)}
        else:
            st.session_state[f"_refresh_bar_{key}"] = summary
    st.cache_data.clear()
    st.rerun()


def _render_stock_summary(client) -> None:
    # Shown once, right after the rerun _run() triggers -- a message set
    # and then immediately st.rerun()-ed away would never actually
    # render, so this is stashed in session_state and displayed on the
    # next script run instead (same pattern for all three summaries below).
    summary = st.session_state.pop("_refresh_bar_stock", None)
    if not summary:
        return
    if summary.get("error"):
        st.error(summary["error"])
        return
    breakdown = _universe_breakdown(client)
    if summary["failed"] == 0:
        st.success(f"✅ Refreshed all {summary['succeeded']} symbols{breakdown}.")
    else:
        failed_symbols = ", ".join(f["symbol"] for f in summary["symbolsFailed"])
        st.warning(
            f"Refreshed {summary['succeeded']} of {summary['total']} symbols{breakdown} -- "
            f"{summary['failed']} failed: {failed_symbols}"
        )


def _render_fo_summary(key: str, exchange_label: str) -> None:
    summary = st.session_state.pop(f"_refresh_bar_{key}", None)
    if not summary:
        return
    if summary.get("error"):
        st.error(summary["error"])
    elif summary.get("updated"):
        st.success(
            f"✅ Loaded {exchange_label} F&O bhavcopy for {summary['tradeDate']}: "
            f"{summary['futuresRows']} futures + {summary['optionRows']} option rows."
        )
    else:
        st.info(summary.get("message", f"{exchange_label} F&O data is already up to date."))


def render_global_refresh_bar(client) -> None:
    """Renders the 3-button bar plus each button's own "last refreshed"
    caption and (once, right after a click) its result message. Reads the
    signed-in user's access token from `st.session_state["sb_access_token"]`
    -- every page that calls this has already gone through `require_login()`,
    which sets it."""
    access_token = st.session_state["sb_access_token"]
    cols = st.columns(3)
    with cols[0]:
        st.caption(_last_fetch_caption(client, "Last stock refresh", ["intraday_price", "all"]))
        if st.button("🔄 Stock Data Refresh", use_container_width=True, key="refresh_bar_stock_btn"):
            _run(
                "stock",
                "Refreshing live data from Yahoo Finance -- this can take up to a minute...",
                lambda: edge_refresh.trigger_manual_refresh(access_token),
            )
    with cols[1]:
        st.caption(_last_fetch_caption(client, "Last NSE F&O refresh", "fo", _NSE_FO_PROVIDER))
        if st.button("📊 NSE F&O Data Refresh", use_container_width=True, key="refresh_bar_nse_fo_btn"):
            _run(
                "nse_fo",
                "Checking NSE for a newer F&O bhavcopy -- this can take up to a few minutes...",
                lambda: edge_refresh.trigger_fo_refresh(access_token, "NSE"),
            )
    with cols[2]:
        st.caption(_last_fetch_caption(client, "Last BSE F&O refresh", "fo", _BSE_FO_PROVIDER))
        if st.button("📊 BSE F&O Data Refresh", use_container_width=True, key="refresh_bar_bse_fo_btn"):
            _run(
                "bse_fo",
                "Checking BSE for a newer F&O bhavcopy -- this can take up to a few minutes...",
                lambda: edge_refresh.trigger_fo_refresh(access_token, "BSE"),
            )

    _render_stock_summary(client)
    _render_fo_summary("nse_fo", "NSE")
    _render_fo_summary("bse_fo", "BSE")
