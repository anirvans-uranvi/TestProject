from __future__ import annotations

import pandas as pd
import streamlit as st
from postgrest.exceptions import APIError
from pydantic import ValidationError

from src.calculations.returns import value_change_from_pct
from src.models.enums import CompanyType
from src.repositories import settings_repo
from src.services import portfolio_service
from src.utils.formatting import format_inr, format_pct
from src.utils.portfolio_page import (
    ensure_cache_bust,
    load_all_companies,
    load_holdings,
    load_latest_prices,
    load_live_broker_prices,
    load_positions,
    load_returns_and_pe,
    slug,
)
from src.utils.refresh_bar import render_global_refresh_bar
from src.utils.session import current_user_id, get_user_client_cached, require_login
from src.utils.ui import inject_global_styles, render_disclaimer, render_stat_grid

st.set_page_config(page_title="My Holdings | Nifty 50 Screener", page_icon="\U0001f4bc", layout="wide")
require_login()  # already injects Tailwind + the light-theme CSS design system

client = get_user_client_cached()
user_id = current_user_id()
user_settings = settings_repo.get_user_settings(client, user_id)
inject_global_styles(user_settings.theme)  # re-inject with the user's actual theme

st.title("\U0001f4bc My Holdings")
render_disclaimer()
render_global_refresh_bar(client)

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


def _fmt_value_change(change: float | None, pct: float | None) -> str:
    """"₹+12,345.67 (+2.34%)" -- absolute rupee change for the period with
    its percentage in parentheses, or an em dash when either half is
    missing (no snapshot far enough back yet, e.g. a fund newly added to
    the portfolio)."""
    if change is None or pct is None:
        return "—"
    return f"₹{change:+,.2f} ({pct:+.2f}%)"


def _render_holdings_table(
    *, title: str, rows: list[dict], portfolio_name: str, key_suffix: str, returns_pe_by_symbol: dict
) -> None:
    """Renders one Holdings table -- either the "ETFs & Mutual Funds" or
    "Stocks" split (see _render_holdings_tab) -- plus the single-row-
    selection "Open in Stock Detail"/"Open in Options" buttons underneath.
    `key_suffix` keeps the two tables' widget keys distinct within the
    same portfolio tab. Both tables share the exact same columns.

    A plain st.dataframe with on_select="rerun" (not the hand-rendered
    render_screener_table, and not a per-row st.button column) -- native
    header-click sort stays correct regardless of how the table is
    currently sorted, which a *pre-sort*-indexed button column can't
    guarantee. Columns are kept as real numbers, formatted for display via
    column_config, so a header-click sort compares the underlying value
    (not a "₹10,81,452.10"-style string, which would sort alphabetically).

    `returns_pe_by_symbol` (keyed by symbol, each value a dict with
    return_1d/return_5d/return_20d/pe_ratio from
    snapshot_repo.get_latest_returns_and_pe) drives the 1D/5D/20D Change
    columns. The period returns are percentages (daily_screener_snapshots
    has no stored historical price), so the rupee change is derived via
    value_change_from_pct(cur_val, return_pct) rather than read directly."""
    st.markdown(f"**{title}**")
    if not rows:
        st.caption("None.")
        return

    table_rows = []
    for r in rows:
        stock = r["symbol"] or f'{r["raw_name"]} (unmatched)'
        rp = returns_pe_by_symbol.get(r["symbol"]) if r["symbol"] else None
        cur_val = r["cur_val"]
        return_1d = rp["return_1d"] if rp else None
        return_5d = rp["return_5d"] if rp else None
        return_20d = rp["return_20d"] if rp else None
        table_rows.append(
            {
                "Stock": stock,
                "Qty": r["qty"],
                "Avg Price": r["avg_price"],
                "LTP": r["ltp"],
                "Investment": r["investment"],
                "Cur Val": cur_val,
                "P&L": r["pnl"],
                "P&L %": r["pnl_pct"],
                "1D Change": _fmt_value_change(value_change_from_pct(cur_val, return_1d), return_1d),
                "5D Change": _fmt_value_change(value_change_from_pct(cur_val, return_5d), return_5d),
                "20D Change": _fmt_value_change(value_change_from_pct(cur_val, return_20d), return_20d),
            }
        )

    column_config = {
        "Qty": st.column_config.NumberColumn(format="%,.0f"),
        "Avg Price": st.column_config.NumberColumn(format="₹%,.2f"),
        "LTP": st.column_config.NumberColumn(format="₹%,.2f"),
        "Investment": st.column_config.NumberColumn(format="₹%,.2f"),
        "Cur Val": st.column_config.NumberColumn(format="₹%,.2f"),
        "P&L": st.column_config.NumberColumn(format="₹%,.2f"),
        "P&L %": st.column_config.NumberColumn(format="%+.2f%%"),
    }

    event = st.dataframe(
        pd.DataFrame(table_rows),
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key=f"holdings_table_{key_suffix}_{slug(portfolio_name)}",
        column_config=column_config,
    )
    selected_rows = event.selection.rows if event and event.selection else []
    if selected_rows:
        selected_symbol = rows[selected_rows[0]]["symbol"]
        if selected_symbol:
            # Every resolved symbol here is, by definition, one of this
            # signed-in user's own portfolio symbols -- and both Stock
            # Detail's and Options' own symbol pickers now union in
            # exactly that set (pages/2_Stock_Detail.py, pages/5_Options.py),
            # so any resolved row is always viewable on either page.
            detail_col, options_col = st.columns(2)
            with detail_col:
                if st.button(
                    f"Open {selected_symbol} in Stock Detail",
                    key=f"holdings_open_detail_{key_suffix}_{portfolio_name}",
                ):
                    st.session_state["selected_symbol"] = selected_symbol
                    st.switch_page("pages/2_Stock_Detail.py")
            with options_col:
                if st.button(
                    f"Open {selected_symbol} in Options", key=f"holdings_open_options_{key_suffix}_{portfolio_name}"
                ):
                    st.session_state["fo_symbol"] = selected_symbol
                    st.switch_page("pages/5_Options.py")
        else:
            st.caption("This holding has no resolved symbol yet, so there's no Stock Detail or Options page for it.")


def _render_holdings_tab(portfolio_name: str, holdings_for_portfolio: list) -> None:
    raw_rows = [
        {
            "raw_name": h.raw_name,
            "symbol": h.symbol,
            "qty": h.qty,
            "avg_price": h.avg_price,
            "investment": h.investment,
        }
        for h in holdings_for_portfolio
    ]
    if not raw_rows:
        st.caption("No holdings saved yet for this portfolio -- upload one on My Broker.")
        return

    merged = portfolio_service.merge_holdings(raw_rows)
    symbols = tuple(sorted({r["symbol"] for r in merged if r["symbol"]}))
    ltp_by_symbol = load_latest_prices(client, symbols, st.session_state["portfolio_cache_bust"])
    # Prefer a live quote from whichever broker(s) this portfolio has
    # connected over the (possibly stale, yfinance-sourced) screener
    # snapshot above -- same preference My CSP's LTP Underlying uses.
    live_ltp_by_symbol = load_live_broker_prices(
        client, user_id, portfolio_name, symbols, st.session_state["portfolio_cache_bust"]
    )
    ltp_by_symbol = {**ltp_by_symbol, **live_ltp_by_symbol}
    rows, totals = portfolio_service.compute_portfolio_view(merged, ltp_by_symbol)
    rows.sort(key=lambda r: r["investment"], reverse=True)

    stats = [
        ("Total Investment", format_inr(totals["total_investment"]), None),
        ("Total Current Value", format_inr(totals["total_cur_val"]), None),
        ("Total P&L", format_inr(totals["total_pnl"]), None),
        ("Total P&L %", format_pct(totals["total_pnl_pct"]), None),
    ]
    st.markdown(render_stat_grid(stats, user_settings.theme, cols=4), unsafe_allow_html=True)
    if totals["unpriced_count"]:
        st.caption(
            f"Totals exclude {totals['unpriced_count']} holding(s) with no market data yet "
            "(shown as N/A below -- they'll be picked up by the next data refresh)."
        )

    # Split into two tables by companies.company_type (migration 0018) --
    # ETF/Fund vs everything else. A holding with no resolved symbol has no
    # company_type to check, so it defaults into "Stocks" (there's no
    # better signal for it -- see _render_holdings_table's "(unmatched)"
    # label for that case).
    all_companies = load_all_companies(client, st.session_state["portfolio_cache_bust"])
    company_type_by_symbol = {c.symbol: c.company_type for c in all_companies}

    def _is_etf_or_fund(symbol: str | None) -> bool:
        return symbol is not None and company_type_by_symbol.get(symbol) in (CompanyType.ETF, CompanyType.FUND)

    etf_fund_rows = [r for r in rows if _is_etf_or_fund(r["symbol"])]
    stock_rows = [r for r in rows if not _is_etf_or_fund(r["symbol"])]

    returns_pe_by_symbol = load_returns_and_pe(client, symbols, st.session_state["portfolio_cache_bust"])

    _render_holdings_table(
        title="ETFs & Mutual Funds",
        rows=etf_fund_rows,
        portfolio_name=portfolio_name,
        key_suffix="etf",
        returns_pe_by_symbol=returns_pe_by_symbol,
    )
    _render_holdings_table(
        title="Stocks",
        rows=stock_rows,
        portfolio_name=portfolio_name,
        key_suffix="stock",
        returns_pe_by_symbol=returns_pe_by_symbol,
    )


portfolio_names = sorted({h.portfolio_name for h in saved_holdings} | {p.portfolio_name for p in saved_positions})

if not portfolio_names:
    st.info("No portfolios yet -- go to My Broker to upload or connect one.")
else:
    tabs = st.tabs(portfolio_names)
    for name, tab in zip(portfolio_names, tabs):
        with tab:
            _render_holdings_tab(name, [h for h in saved_holdings if h.portfolio_name == name])
