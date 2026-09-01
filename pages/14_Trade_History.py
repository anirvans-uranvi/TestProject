"""Realized P&L (FIFO-matched closed lots) and Unrealised P&L (today's
live P&L on whatever's still currently held/open, plus the trade fills
that built each one up), built from every trade fill synced via
Settings' "Sync Trade History from Dhan"
(src/utils/data_provider_settings.py's _render_dhan_trade_history_sync,
writing portfolio_trade_fills -- migration 0038). Dhan only for now, same
as that sync itself.

Unlike every other portfolio page (My Trades/My Holdings/My Positions/
My CSP/My CC/Analyse Trade), Realized P&L's data does NOT come from the
Stock & Option Data Refresh / Portfolio Refresh buttons -- those sync
current-state holdings/positions snapshots, not historical fills -- so
neither refresh bar is rendered here. Unrealised P&L, by contrast,
deliberately DOES reuse My Holdings/My Positions' own already-synced
holdings/positions + live-LTP data (portfolio_page.py's
load_holdings/load_positions/load_latest_prices/load_live_broker_prices,
portfolio_service.compute_portfolio_view/compute_positions_view) rather
than trying to derive "what's currently held" purely from trade fills --
see this file's own "trades leading to this holding" section for why
(some real, currently-held quantity has no matching trade fill at all:
shares transferred in from another broker never execute on an exchange,
so Dhan's own /v2/trades never receives them -- confirmed live, and not
fixable by any change to how fills are parsed).

See src/services/portfolio_service.py's compute_realized_pnl/
compute_open_lots for the FIFO lot-matching both sections are built on.
"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from postgrest.exceptions import APIError
from pydantic import ValidationError

from src.repositories import settings_repo
from src.services import portfolio_service
from src.utils.formatting import format_inr
from src.utils.portfolio_page import (
    ensure_cache_bust,
    load_holdings,
    load_latest_prices,
    load_live_broker_prices,
    load_positions,
    load_trade_fills,
)
from src.utils.session import current_user_id, get_user_client_cached, require_login
from src.utils.ui import inject_global_styles, plotly_template, render_disclaimer, render_stat_grid

_EPS = 1e-6  # float-tolerance for "does trade history account for the full current quantity"

st.set_page_config(page_title="Trade History | Nifty 50 Screener", page_icon="\U0001f4d1", layout="wide")
require_login()  # already injects Tailwind + the light-theme CSS design system

client = get_user_client_cached()
user_id = current_user_id()
user_settings = settings_repo.get_user_settings(client, user_id)
inject_global_styles(user_settings.theme)  # re-inject with the user's actual theme

st.title("\U0001f4d1 Trade History")
render_disclaimer()
st.caption(
    "Realized P&L is FIFO-matched against actual closed lots from every synced trade fill. Unrealised P&L "
    "shows today's live P&L on whatever's still currently held/open, using the same data My Holdings/My "
    "Positions already trust, plus the trade fills that built each one up."
)

ensure_cache_bust()
cache_bust = st.session_state["portfolio_cache_bust"]

try:
    fills = load_trade_fills(client, user_id, cache_bust)
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

open_lots_by_symbol: dict[str, list[dict]] = {}
for lot in portfolio_service.compute_open_lots(fills):
    open_lots_by_symbol.setdefault(lot["symbol"] or lot["raw_name"], []).append(lot)

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
            "Instrument": lot["raw_name"],
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
        for lot in closed_lots
    ]
    pnl_column_config = {
        "Entry Price": st.column_config.NumberColumn(format="₹%.2f"),
        "Exit Price": st.column_config.NumberColumn(format="₹%.2f"),
        "Gross P&L": st.column_config.NumberColumn(format="₹%,.2f"),
        "Charges": st.column_config.NumberColumn(format="₹%,.2f"),
        "Net P&L": st.column_config.NumberColumn(format="₹%,.2f"),
        "Qty": st.column_config.NumberColumn(format="%g"),
    }

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

    # Grouped by underlying -- one expander per symbol, rows within it
    # still newest-exit-first, same as the previous flat table's ordering.
    pnl_rows_by_symbol: dict[str, list[dict]] = {}
    for row in pnl_rows:
        pnl_rows_by_symbol.setdefault(row["Symbol"], []).append(row)
    for symbol in sorted(pnl_rows_by_symbol):
        symbol_rows = sorted(pnl_rows_by_symbol[symbol], key=lambda r: r["Exit"], reverse=True)
        symbol_net = sum(r["Net P&L"] for r in symbol_rows)
        with st.expander(f"{symbol} — {format_inr(symbol_net)} ({len(symbol_rows)} closed lot(s))"):
            # "Symbol" is redundant here -- it's the expander header itself.
            st.dataframe(
                pd.DataFrame(symbol_rows).drop(columns=["Symbol"]),
                use_container_width=True,
                hide_index=True,
                column_config=pnl_column_config,
            )

st.divider()
st.subheader("Unrealised P&L")
st.caption(
    "Today's live P&L on whatever's still currently held/open -- from the same holdings/positions data "
    "My Holdings/My Positions show, not derived from trade history (some currently-held quantity may have "
    "no matching trade fill at all -- see the note inside a symbol's section below if so)."
)

try:
    saved_holdings = load_holdings(client, user_id, cache_bust)
except (APIError, ValidationError):
    saved_holdings = []
try:
    saved_positions = load_positions(client, user_id, cache_bust)
except APIError:
    saved_positions = []

holding_dicts = [
    {"raw_name": h.raw_name, "symbol": h.symbol, "qty": h.qty, "avg_price": h.avg_price, "investment": h.investment}
    for h in saved_holdings
]
merged_holdings = portfolio_service.merge_holdings(holding_dicts)
holding_symbols = tuple(sorted({r["symbol"] for r in merged_holdings if r["symbol"]}))
ltp_by_symbol = {
    **load_latest_prices(client, holding_symbols, cache_bust),
    **load_live_broker_prices(client, user_id, holding_symbols, cache_bust),
}
holding_rows, _ = portfolio_service.compute_portfolio_view(merged_holdings, ltp_by_symbol)

position_dicts = [
    {
        "raw_name": p.raw_name,
        "symbol": p.symbol,
        "expiry_date": p.expiry_date,
        "strike_price": p.strike_price,
        "option_type": p.option_type,
        "qty": p.qty,
        "avg_price": p.avg_price,
        "ltp": p.ltp,
    }
    for p in saved_positions
]
position_rows = portfolio_service.compute_positions_view(position_dicts)

open_rows = [
    {
        "Kind": "Holding",
        "Symbol": r["symbol"] or r["raw_name"],
        "Expiry": "—",
        "Strike": "—",
        "Type": "—",
        "Qty": r["qty"],
        "Avg Price": r["avg_price"],
        "LTP": r["ltp"],
        "P&L": r["pnl"],
        "P&L %": r["pnl_pct"],
    }
    for r in holding_rows
] + [
    {
        "Kind": "Position",
        "Symbol": r["symbol"] or r["raw_name"],
        "Expiry": r["expiry_date"].strftime("%d-%b-%y") if r["expiry_date"] else "—",
        "Strike": r["strike_price"] if r["strike_price"] is not None else "—",
        "Type": r["option_type"].value if r["option_type"] else "—",
        "Qty": r["qty"],
        "Avg Price": r["avg_price"],
        "LTP": r["ltp"],
        "P&L": r["pnl"],
        "P&L %": r["pnl_pct"],
    }
    for r in position_rows
]

if not open_rows:
    st.caption("Nothing currently held or open.")
else:
    priced_rows = [r for r in open_rows if r["P&L"] is not None]
    total_unrealised = sum(r["P&L"] for r in priced_rows)
    stats = [
        ("Unrealised P&L", format_inr(total_unrealised), f"{len(open_rows)} holding(s)/position(s)"),
    ]
    st.markdown(render_stat_grid(stats, user_settings.theme, cols=1), unsafe_allow_html=True)
    if len(priced_rows) < len(open_rows):
        st.caption(f"Excludes {len(open_rows) - len(priced_rows)} row(s) with no live price yet.")

    by_symbol = (
        pd.DataFrame(priced_rows).groupby("Symbol", as_index=False)["P&L"].sum().sort_values("P&L")
        if priced_rows
        else pd.DataFrame(columns=["Symbol", "P&L"])
    )
    if not by_symbol.empty:
        fig = go.Figure()
        fig.add_trace(go.Bar(x=by_symbol["Symbol"], y=by_symbol["P&L"]))
        fig.update_layout(
            template=plotly_template(user_settings.theme),
            height=320,
            margin=dict(l=10, r=10, t=30, b=10),
            title="Unrealised P&L by symbol",
        )
        st.plotly_chart(fig, use_container_width=True)

    open_column_config = {
        "Avg Price": st.column_config.NumberColumn(format="₹%.2f"),
        "LTP": st.column_config.NumberColumn(format="₹%.2f"),
        "P&L": st.column_config.NumberColumn(format="₹%,.2f"),
        "P&L %": st.column_config.NumberColumn(format="%+.2f%%"),
        "Qty": st.column_config.NumberColumn(format="%g"),
    }
    lot_column_config = {
        "Price": st.column_config.NumberColumn(format="₹%.2f"),
        "Qty": st.column_config.NumberColumn(format="%g"),
    }

    open_rows_by_symbol: dict[str, list[dict]] = {}
    for row in open_rows:
        open_rows_by_symbol.setdefault(row["Symbol"], []).append(row)

    for symbol in sorted(open_rows_by_symbol):
        symbol_rows = open_rows_by_symbol[symbol]
        symbol_pnl = sum(r["P&L"] for r in symbol_rows if r["P&L"] is not None)
        with st.expander(f"{symbol} — {format_inr(symbol_pnl)}"):
            # "Symbol" is redundant here -- it's the expander header itself.
            st.dataframe(
                pd.DataFrame(symbol_rows).drop(columns=["Symbol"]),
                use_container_width=True,
                hide_index=True,
                column_config=open_column_config,
            )

            st.markdown("**Trades leading to this holding**")
            symbol_open_lots = sorted(open_lots_by_symbol.get(symbol, []), key=lambda lot: lot["traded_at"])
            if not symbol_open_lots:
                st.caption(
                    "No matching trade fills at all -- likely transferred in from another broker (Dhan's "
                    "trade history never receives an off-market transfer), or predates the synced date range."
                )
            else:
                actual_qty = sum(r["Qty"] for r in symbol_rows)
                fills_qty = sum(lot["qty"] for lot in symbol_open_lots)
                if abs(actual_qty - fills_qty) > _EPS:
                    st.caption(
                        f"Trade history accounts for {fills_qty:g} of {actual_qty:g} units -- the difference "
                        "likely reflects shares transferred from another broker (Dhan's trade history never "
                        "receives those), or fills outside the synced date range."
                    )
                lot_rows = [
                    {
                        "Entry": lot["traded_at"],
                        "Instrument": lot["raw_name"],
                        "Qty": lot["qty"],
                        "Price": lot["price"],
                    }
                    for lot in symbol_open_lots
                ]
                st.dataframe(
                    pd.DataFrame(lot_rows),
                    use_container_width=True,
                    hide_index=True,
                    column_config=lot_column_config,
                )
