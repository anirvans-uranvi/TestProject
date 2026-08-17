from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st
from postgrest.exceptions import APIError
from pydantic import ValidationError

from src.repositories import settings_repo
from src.services import portfolio_service
from src.utils.formatting import format_inr
from src.utils.portfolio_page import ensure_cache_bust, load_all_companies, load_holdings, load_positions, slug
from src.utils.refresh_bar import render_global_refresh_bar
from src.utils.session import current_user_id, get_user_client_cached, require_login
from src.utils.ui import inject_global_styles, render_disclaimer, render_stat_grid

st.set_page_config(page_title="My Positions | Nifty 50 Screener", page_icon="\U0001f4bc", layout="wide")
require_login()  # already injects Tailwind + the light-theme CSS design system

client = get_user_client_cached()
user_id = current_user_id()
user_settings = settings_repo.get_user_settings(client, user_id)
inject_global_styles(user_settings.theme)  # re-inject with the user's actual theme

st.title("\U0001f4bc My Positions")
render_disclaimer()
render_global_refresh_bar(client)
st.caption("Individual F&O legs, one row each. To see them grouped into Trades, go to My Trades.")

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
    positions_available = True
except APIError:
    saved_positions = []
    positions_available = False


def _render_option_table(title: str, rows: list[dict], portfolio_name: str, key_suffix: str) -> None:
    """Renders one of the "Stock Options"/"Index Options" tables --
    identical columns, split only by classify_position_bucket. `Instrument`
    is the broker's raw contract string, `Underlying` the decoded symbol --
    every row here has both option_type and symbol set (see
    classify_position_bucket), so there's no "(undecoded)" fallback to
    handle, unlike the Others table below."""
    st.markdown(f"**{title}**")
    if not rows:
        st.caption("None.")
        return

    table_rows = [
        {
            "Instrument": p["raw_name"],
            "Underlying": p["symbol"],
            "Expiry": p["expiry_date"].strftime("%d %b %Y") if p["expiry_date"] else None,
            "Strike": p["strike_price"],
            "Type": p["option_type"].value,
            "Qty": p["qty"],
            "Avg Price": p["avg_price"],
            "P&L": p["pnl"],
            "P&L %": p["pnl_pct"],
        }
        for p in rows
    ]
    st.dataframe(
        pd.DataFrame(table_rows),
        use_container_width=True,
        hide_index=True,
        key=f"positions_table_{key_suffix}_{slug(portfolio_name)}",
        column_config={
            "Qty": st.column_config.NumberColumn(format="%+,.0f"),
            "Avg Price": st.column_config.NumberColumn(format="₹%,.2f"),
            "P&L": st.column_config.NumberColumn(format="₹%,.2f"),
            "P&L %": st.column_config.NumberColumn(format="%+.2f%%"),
        },
    )


def _render_others_table(rows: list[dict], portfolio_name: str) -> None:
    """Everything that isn't a decoded option contract -- an undecoded F&O
    row, a futures position, or a stock/ETF bought/sold as a position
    rather than a holding (see classify_position_bucket). `symbol` is
    always None for every row here (option_type and symbol are only ever
    set together), so `Instrument` is simply the broker's raw string --
    the exact NSE symbol already, for a plain equity/ETF position."""
    st.markdown("**Others**")
    if not rows:
        st.caption("None.")
        return

    table_rows = [
        {
            "Instrument": p["raw_name"],
            "Qty": p["qty"],
            "Avg Price": p["avg_price"],
            "P&L": p["pnl"],
            "P&L %": p["pnl_pct"],
        }
        for p in rows
    ]
    st.dataframe(
        pd.DataFrame(table_rows),
        use_container_width=True,
        hide_index=True,
        key=f"positions_table_other_{slug(portfolio_name)}",
        column_config={
            "Qty": st.column_config.NumberColumn(format="%+,.0f"),
            "Avg Price": st.column_config.NumberColumn(format="₹%,.2f"),
            "P&L": st.column_config.NumberColumn(format="₹%,.2f"),
            "P&L %": st.column_config.NumberColumn(format="%+.2f%%"),
        },
    )


def _render_positions_tab(portfolio_name: str, positions_for_portfolio: list) -> None:
    if not positions_available:
        st.info(
            "Positions isn't set up yet. Apply migration "
            "`supabase/migrations/0016_portfolio_positions.sql` in the Supabase SQL editor, "
            "then reload this page."
        )
        return
    if not positions_for_portfolio:
        st.caption("No positions saved yet for this portfolio -- upload one on My Broker.")
        return

    position_rows = [
        {
            "raw_name": p.raw_name,
            "broker": p.broker,
            "symbol": p.symbol,
            "expiry_date": p.expiry_date,
            "strike_price": p.strike_price,
            "option_type": p.option_type,
            "qty": p.qty,
            "avg_price": p.avg_price,
            "ltp": p.ltp,
        }
        for p in positions_for_portfolio
    ]
    computed_positions = portfolio_service.compute_positions_view(position_rows)
    computed_positions.sort(key=lambda p: (p["symbol"] or p["raw_name"], p["expiry_date"] or date.max))

    priced = [p for p in computed_positions if p["pnl"] is not None]
    st.markdown(
        render_stat_grid(
            [("Total P&L", format_inr(sum(p["pnl"] for p in priced)) if priced else "N/A", None)],
            user_settings.theme,
            cols=1,
        ),
        unsafe_allow_html=True,
    )
    if len(priced) < len(computed_positions):
        st.caption(
            f"Total excludes {len(computed_positions) - len(priced)} position(s) with no LTP available "
            "(shown as N/A below)."
        )
    st.caption(
        "P&L is (LTP - Avg Price) x Qty (qty is signed -- negative is short) against each position's "
        "last-known LTP, not live -- index F&O (NIFTY/BANKNIFTY/SENSEX) isn't tracked by this app's own "
        "market data."
    )

    all_companies = load_all_companies(client, st.session_state["portfolio_cache_bust"])
    company_type_by_symbol = {c.symbol: c.company_type for c in all_companies}

    stock_options, index_options, others = [], [], []
    for p in computed_positions:
        bucket = portfolio_service.classify_position_bucket(p["option_type"], p["symbol"], company_type_by_symbol)
        {"stock": stock_options, "index": index_options, "other": others}[bucket].append(p)

    _render_option_table("Stock Options", stock_options, portfolio_name, "stock")
    _render_option_table("Index Options", index_options, portfolio_name, "index")
    _render_others_table(others, portfolio_name)


portfolio_names = sorted({h.portfolio_name for h in saved_holdings} | {p.portfolio_name for p in saved_positions})

if not portfolio_names:
    st.info("No portfolios yet -- go to My Broker to upload or connect one.")
else:
    tabs = st.tabs(portfolio_names)
    for name, tab in zip(portfolio_names, tabs):
        with tab:
            _render_positions_tab(name, [p for p in saved_positions if p.portfolio_name == name])
