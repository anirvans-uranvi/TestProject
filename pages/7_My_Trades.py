from __future__ import annotations

import pandas as pd
import streamlit as st
from postgrest.exceptions import APIError
from pydantic import ValidationError

from src.repositories import settings_repo
from src.services import portfolio_service
from src.utils.portfolio_page import (
    build_trade_legs,
    ensure_cache_bust,
    load_all_companies,
    load_holdings,
    load_positions,
    load_trade_groups,
    load_trade_meta,
    slug,
)
from src.utils.refresh_bar import render_global_refresh_bar
from src.utils.session import current_user_id, get_user_client_cached, require_login
from src.utils.ui import inject_global_styles, render_disclaimer

st.set_page_config(page_title="My Trades | Nifty 50 Screener", page_icon="\U0001f4bc", layout="wide")
require_login()  # already injects Tailwind + the light-theme CSS design system

client = get_user_client_cached()
user_id = current_user_id()
user_settings = settings_repo.get_user_settings(client, user_id)
inject_global_styles(user_settings.theme)  # re-inject with the user's actual theme

st.title("\U0001f4bc My Trades")
render_disclaimer()
render_global_refresh_bar(client)
st.caption(
    "Holdings and F&O positions sharing an underlying, grouped into one Trade. Select a row and click "
    "\"Analyse Trade\" to see its legs, correct the underlying, rename the trade type, or merge/split trades."
)

ensure_cache_bust()

try:
    saved_holdings = load_holdings(client, user_id, st.session_state["portfolio_cache_bust"])
except (APIError, ValidationError):
    st.info(
        "Portfolio isn't set up yet. Apply migrations "
        "`supabase/migrations/0012_portfolio_holdings.sql` and "
        "`supabase/migrations/0014_portfolio_holdings_multi_portfolio.sql` "
        "(in that order) in the Supabase SQL editor, then reload this page."
    )
    st.stop()

try:
    saved_positions = load_positions(client, user_id, st.session_state["portfolio_cache_bust"])
except APIError:
    saved_positions = []

try:
    saved_trade_groups = load_trade_groups(client, user_id, st.session_state["portfolio_cache_bust"])
except APIError:
    # portfolio_trade_groups doesn't exist yet (migration 0020) -- degrade
    # to "no manual Trade groupings saved yet" (every leg falls back to its
    # default per-underlying Trade) rather than st.stop().
    saved_trade_groups = []

try:
    saved_trade_meta = load_trade_meta(client, user_id, st.session_state["portfolio_cache_bust"])
except APIError:
    # portfolio_trade_meta doesn't exist yet (migration 0021) -- degrade to
    # "no corrected underlying label / custom trade type saved yet".
    saved_trade_meta = []


def _render_trades_table(*, title: str, trades: list[dict], portfolio_name: str, key_suffix: str) -> None:
    st.markdown(f"**{title}**")
    if not trades:
        st.caption("None.")
        return

    trades_sorted = sorted(trades, key=lambda t: t["underlying_label"])
    table_rows = [
        {
            "Underlying Instrument": t["underlying_label"],
            "Trade Type": t["trade_type"],
            "Legs": t["leg_count"],
            "Total P&L": t["total_pnl"],
        }
        for t in trades_sorted
    ]
    event = st.dataframe(
        pd.DataFrame(table_rows),
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key=f"trades_table_{key_suffix}_{slug(portfolio_name)}",
        column_config={
            "Legs": st.column_config.NumberColumn(format="%d"),
            "Total P&L": st.column_config.NumberColumn(format="₹%,.2f"),
        },
    )
    selected_rows = event.selection.rows if event and event.selection else []
    if selected_rows:
        selected_trade = trades_sorted[selected_rows[0]]
        if st.button(
            f"Analyse Trade: {selected_trade['underlying_label']}",
            key=f"trades_analyse_{key_suffix}_{slug(portfolio_name)}",
        ):
            st.session_state["analyse_trade_id"] = selected_trade["trade_id"]
            st.session_state["analyse_trade_portfolio"] = portfolio_name
            st.switch_page("pages/10_Analyse_Trade.py")


def _render_trades_tab(
    portfolio_name: str,
    holdings_for_portfolio: list,
    positions_for_portfolio: list,
    trade_groups_for_portfolio: list,
    trade_meta_for_portfolio: list,
    company_type_by_symbol: dict,
) -> None:
    legs = build_trade_legs(client, st.session_state["portfolio_cache_bust"], holdings_for_portfolio, positions_for_portfolio)
    if not legs:
        st.caption("No holdings or positions saved yet for this portfolio -- upload one on My Broker.")
        return

    overrides_by_leg = {(g.broker, g.raw_name): g.trade_id for g in trade_groups_for_portfolio}
    trade_meta_by_id = {m.trade_id: {"underlying_label": m.underlying_label, "trade_type": m.trade_type} for m in trade_meta_for_portfolio}
    trades = portfolio_service.group_into_trades(legs, overrides_by_leg, trade_meta_by_id, company_type_by_symbol)

    stock_trades = [t for t in trades if t["bucket"] == "stock"]
    index_trades = [t for t in trades if t["bucket"] == "index"]
    other_trades = [t for t in trades if t["bucket"] == "other"]

    _render_trades_table(title="Stock Trades", trades=stock_trades, portfolio_name=portfolio_name, key_suffix="stock")
    _render_trades_table(title="Index Trades", trades=index_trades, portfolio_name=portfolio_name, key_suffix="index")
    _render_trades_table(title="Other Trades", trades=other_trades, portfolio_name=portfolio_name, key_suffix="other")


portfolio_names = sorted({h.portfolio_name for h in saved_holdings} | {p.portfolio_name for p in saved_positions})

if not portfolio_names:
    st.info("No portfolios yet -- go to My Broker to upload or connect one.")
else:
    all_companies = load_all_companies(client, st.session_state["portfolio_cache_bust"])
    company_type_by_symbol = {c.symbol: c.company_type for c in all_companies}

    tabs = st.tabs(portfolio_names)
    for name, tab in zip(portfolio_names, tabs):
        with tab:
            _render_trades_tab(
                name,
                [h for h in saved_holdings if h.portfolio_name == name],
                [p for p in saved_positions if p.portfolio_name == name],
                [g for g in saved_trade_groups if g.portfolio_name == name],
                [m for m in saved_trade_meta if m.portfolio_name == name],
                company_type_by_symbol,
            )
