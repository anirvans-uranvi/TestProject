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
    load_latest_prices,
    load_positions,
    load_returns_and_pe,
    load_trade_groups,
    load_trade_meta,
    slug,
)
from src.utils.refresh_bar import render_global_refresh_bar
from src.utils.session import current_user_id, get_user_client_cached, require_login
from src.utils.ui import inject_global_styles, render_disclaimer

st.set_page_config(page_title="My CSP | Nifty 50 Screener", page_icon="\U0001f4b0", layout="wide")
require_login()  # already injects Tailwind + the light-theme CSS design system

client = get_user_client_cached()
user_id = current_user_id()
user_settings = settings_repo.get_user_settings(client, user_id)
inject_global_styles(user_settings.theme)  # re-inject with the user's actual theme

st.title("\U0001f4b0 My CSP")
render_disclaimer()
render_global_refresh_bar(client)
st.caption(
    'Every position leg from a Trade whose Trade Type is "CSP". Go to My Trades, select a trade, click '
    '"Analyse Trade", and rename its Trade Type to "CSP" to have it show up here.'
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
    # "no Trade Type saved yet", which just means nothing is tagged "CSP".
    saved_trade_meta = []


def _render_csp_tab(
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
    trade_meta_by_id = {
        m.trade_id: {"underlying_label": m.underlying_label, "trade_type": m.trade_type, "bucket_override": m.bucket_override}
        for m in trade_meta_for_portfolio
    }
    trades = portfolio_service.group_into_trades(legs, overrides_by_leg, trade_meta_by_id, company_type_by_symbol)

    # Only Position-type legs -- a CSP is an option position; a Holding
    # leg (e.g. accidentally merged into a "CSP"-renamed Trade) has no
    # expiry/strike to show and is silently skipped rather than shown
    # with blanks.
    csp_legs = [
        leg
        for t in trades
        if portfolio_service.is_csp_trade_type(t["trade_type"])
        for leg in t["legs"]
        if leg["leg_type"] == "Position"
    ]
    if not csp_legs:
        st.caption('No trades tagged "CSP" yet for this portfolio.')
        return

    symbols = tuple(sorted({leg["symbol"] for leg in csp_legs if leg["symbol"]}))
    ltp_by_symbol = load_latest_prices(client, symbols, st.session_state["portfolio_cache_bust"])
    returns_by_symbol = load_returns_and_pe(client, symbols, st.session_state["portfolio_cache_bust"])

    table_rows = []
    for leg in csp_legs:
        rp = returns_by_symbol.get(leg["symbol"]) if leg["symbol"] else None
        table_rows.append(
            {
                "Instrument": leg["raw_name"],
                "Underlying": leg["symbol"],
                "Expiry": leg["expiry_date"].strftime("%d %b %Y") if leg["expiry_date"] else None,
                "Strike": leg["strike_price"],
                "Qty": leg["qty"],
                "Avg Price": leg["avg_price"],
                "LTP": leg["ltp"],
                "P&L": leg["pnl"],
                "P&L %": leg["pnl_pct"],
                "LTP Underlying": ltp_by_symbol.get(leg["symbol"]) if leg["symbol"] else None,
                "1D Underlying": rp["return_1d"] if rp else None,
                "5D Underlying": rp["return_5d"] if rp else None,
                "20D Underlying": rp["return_20d"] if rp else None,
            }
        )

    st.dataframe(
        pd.DataFrame(table_rows),
        use_container_width=True,
        hide_index=True,
        key=f"csp_table_{slug(portfolio_name)}",
        column_config={
            "Qty": st.column_config.NumberColumn(format="%+,.0f"),
            "Avg Price": st.column_config.NumberColumn(format="₹%,.2f"),
            "LTP": st.column_config.NumberColumn(format="₹%,.2f"),
            "P&L": st.column_config.NumberColumn(format="₹%,.2f"),
            "P&L %": st.column_config.NumberColumn(format="%+.2f%%"),
            "LTP Underlying": st.column_config.NumberColumn(format="₹%,.2f"),
            "1D Underlying": st.column_config.NumberColumn(format="%+.2f%%"),
            "5D Underlying": st.column_config.NumberColumn(format="%+.2f%%"),
            "20D Underlying": st.column_config.NumberColumn(format="%+.2f%%"),
        },
    )


portfolio_names = sorted({h.portfolio_name for h in saved_holdings} | {p.portfolio_name for p in saved_positions})

if not portfolio_names:
    st.info("No portfolios yet -- go to My Broker to upload or connect one.")
else:
    all_companies = load_all_companies(client, st.session_state["portfolio_cache_bust"])
    company_type_by_symbol = {c.symbol: c.company_type for c in all_companies}

    tabs = st.tabs(portfolio_names)
    for name, tab in zip(portfolio_names, tabs):
        with tab:
            _render_csp_tab(
                name,
                [h for h in saved_holdings if h.portfolio_name == name],
                [p for p in saved_positions if p.portfolio_name == name],
                [g for g in saved_trade_groups if g.portfolio_name == name],
                [m for m in saved_trade_meta if m.portfolio_name == name],
                company_type_by_symbol,
            )
