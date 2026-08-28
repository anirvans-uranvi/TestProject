"""Realized P&L (FIFO-matched closed lots) and a raw trade journal, built
from every trade fill synced via Settings' "Sync Trade History from Dhan"
(src/utils/data_provider_settings.py's _render_dhan_trade_history_sync,
writing portfolio_trade_fills -- migration 0038). Dhan only for now, same
as that sync itself.

Unlike every other portfolio page (My Trades/My Holdings/My Positions/
My CSP/My CC/Analyse Trade), this page's data does NOT come from the
Stock & Option Data Refresh / Portfolio Refresh buttons -- those sync
current-state holdings/positions snapshots, not historical fills -- so
neither refresh bar is rendered here. See src/services/portfolio_service.py's
compute_realized_pnl for the FIFO lot-matching this page's Realized P&L
section is built on.
"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from postgrest.exceptions import APIError

from src.repositories import settings_repo
from src.services import portfolio_service
from src.utils.formatting import format_inr
from src.utils.portfolio_page import ensure_cache_bust, load_trade_fills
from src.utils.session import current_user_id, get_user_client_cached, require_login
from src.utils.ui import inject_global_styles, plotly_template, render_disclaimer, render_stat_grid

st.set_page_config(page_title="Trade History | Nifty 50 Screener", page_icon="\U0001f4d1", layout="wide")
require_login()  # already injects Tailwind + the light-theme CSS design system

client = get_user_client_cached()
user_id = current_user_id()
user_settings = settings_repo.get_user_settings(client, user_id)
inject_global_styles(user_settings.theme)  # re-inject with the user's actual theme

st.title("\U0001f4d1 Trade History")
render_disclaimer()
st.caption(
    "Every trade fill Dhan has ever executed for this account. Realized P&L below is FIFO-matched against "
    "actual closed lots -- unlike every other portfolio page here, which only shows unrealized P&L on "
    "currently open positions."
)

ensure_cache_bust()

try:
    fills = load_trade_fills(client, user_id, st.session_state["portfolio_cache_bust"])
except APIError:
    st.info(
        "Trade history isn't set up yet. Apply migration "
        "`supabase/migrations/0038_portfolio_trade_fills.sql` in the Supabase SQL editor, then reload this page."
    )
    st.stop()

if not fills:
    st.info('No trade fills synced yet -- go to Settings > Data Provider > "Sync Trade History from Dhan".')
    if st.button("Go to Settings"):
        st.switch_page("pages/4_Settings.py")
    st.stop()

st.divider()
st.subheader("Realized P&L")
closed_lots = portfolio_service.compute_realized_pnl(fills)
if not closed_lots:
    st.caption("No closed trades yet -- every synced fill is still part of a currently open position.")
else:
    total_gross = sum(lot["gross_pnl"] for lot in closed_lots)
    total_charges = sum(lot["charges"] for lot in closed_lots)
    total_net = sum(lot["net_pnl"] for lot in closed_lots)
    stats = [
        ("Gross P&L", format_inr(total_gross), None),
        ("Charges", format_inr(total_charges), None),
        ("Net P&L", format_inr(total_net), f"{len(closed_lots)} closed lot(s)"),
    ]
    st.markdown(render_stat_grid(stats, user_settings.theme, cols=3), unsafe_allow_html=True)

    pnl_rows = [
        {
            "Symbol": lot["symbol"] or "—",
            "Expiry": lot["expiry_date"].strftime("%d-%b-%y") if lot["expiry_date"] else "—",
            "Strike": lot["strike_price"] if lot["strike_price"] is not None else "—",
            "Type": lot["option_type"].value if lot["option_type"] else "—",
            "Entry": lot["entry_time"],
            "Exit": lot["exit_time"],
            "Qty": lot["qty_closed"],
            "Entry Price": lot["entry_price"],
            "Exit Price": lot["exit_price"],
            "Gross P&L": lot["gross_pnl"],
            "Charges": lot["charges"],
            "Net P&L": lot["net_pnl"],
        }
        for lot in sorted(closed_lots, key=lambda lot: lot["exit_time"], reverse=True)
    ]
    st.dataframe(
        pd.DataFrame(pnl_rows),
        use_container_width=True,
        hide_index=True,
        column_config={
            "Entry Price": st.column_config.NumberColumn(format="₹%.2f"),
            "Exit Price": st.column_config.NumberColumn(format="₹%.2f"),
            "Gross P&L": st.column_config.NumberColumn(format="₹%,.2f"),
            "Charges": st.column_config.NumberColumn(format="₹%,.2f"),
            "Net P&L": st.column_config.NumberColumn(format="₹%,.2f"),
            "Qty": st.column_config.NumberColumn(format="%g"),
        },
    )

    by_symbol = (
        pd.DataFrame(pnl_rows).groupby("Symbol", as_index=False)["Net P&L"].sum().sort_values("Net P&L")
    )
    fig = go.Figure()
    fig.add_trace(go.Bar(x=by_symbol["Symbol"], y=by_symbol["Net P&L"]))
    fig.update_layout(
        template=plotly_template(user_settings.theme),
        height=320,
        margin=dict(l=10, r=10, t=30, b=10),
        title="Net realized P&L by symbol",
    )
    st.plotly_chart(fig, use_container_width=True)

st.divider()
st.subheader("Trade Journal")
all_symbols = sorted({f.symbol for f in fills if f.symbol})
selected_symbols = st.multiselect("Filter by symbol", all_symbols)
journal_fills = [f for f in fills if not selected_symbols or f.symbol in selected_symbols]
journal_rows = [
    {
        "Traded At": f.traded_at,
        "Symbol": f.symbol or f.raw_name,
        "Expiry": f.expiry_date.strftime("%d-%b-%y") if f.expiry_date else "—",
        "Strike": f.strike_price if f.strike_price is not None else "—",
        "Type": f.option_type.value if f.option_type else "—",
        "Side": f.transaction_type,
        "Qty": f.qty,
        "Price": f.price,
        "Brokerage": f.brokerage,
        "Charges": f.taxes_and_charges,
    }
    for f in sorted(journal_fills, key=lambda f: f.traded_at, reverse=True)
]
st.dataframe(
    pd.DataFrame(journal_rows),
    use_container_width=True,
    hide_index=True,
    column_config={
        "Price": st.column_config.NumberColumn(format="₹%.2f"),
        "Brokerage": st.column_config.NumberColumn(format="₹%.2f"),
        "Charges": st.column_config.NumberColumn(format="₹%.2f"),
        "Qty": st.column_config.NumberColumn(format="%g"),
    },
)
