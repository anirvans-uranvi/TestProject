from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st
from postgrest.exceptions import APIError
from pydantic import ValidationError

from src.calculations.returns import value_change_from_pct
from src.models.enums import CompanyType
from src.repositories import fo_repo, settings_repo
from src.services import fo_service, portfolio_service
from src.utils.formatting import format_inr, format_pct
from src.utils.portfolio_page import (
    ensure_cache_bust,
    load_all_companies,
    load_holdings,
    load_latest_prices,
    load_option_chain,
    load_option_expiries,
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


def _load_covered_calls(symbols: tuple[str, ...], expiry_iso: str | None) -> dict[str, dict | None]:
    """Best-effort per-symbol covered-call chain lookup for the selected
    expiry -- returns {} entirely if the F&O tables aren't migrated yet
    (or no expiry is selected), and skips (rather than errors on) any
    individual symbol with no F&O data at all (ETFs, ex-Nifty50 stocks) or
    that doesn't have a contract for this exact expiry date (matched by
    actual date, not position, since NSE monthly expiries occasionally
    drift out of alignment across symbols)."""
    if expiry_iso is None:
        return {}
    try:
        expiries_by_symbol = load_option_expiries(client, symbols, st.session_state["portfolio_cache_bust"])
    except APIError:
        return {}

    chains: dict[str, dict | None] = {}
    for symbol in symbols:
        if expiry_iso not in (expiries_by_symbol.get(symbol) or []):
            continue
        try:
            chains[symbol] = {
                "rows": load_option_chain(client, symbol, expiry_iso, st.session_state["portfolio_cache_bust"]),
                "expiry_iso": expiry_iso,
            }
        except APIError:
            continue
    return chains


def _render_holdings_table(
    *,
    title: str,
    rows: list[dict],
    portfolio_name: str,
    key_suffix: str,
    cc_by_symbol: dict,
    include_cc: bool = True,
    returns_pe_by_symbol: dict | None = None,
) -> None:
    """Renders one Holdings table -- either the "ETFs & Mutual Funds" or
    "Stocks" split (see _render_holdings_tab) -- plus the single-row-
    selection "Open in Stock Detail"/"Open in Options" buttons underneath.
    `key_suffix` keeps the two tables' widget keys distinct within the
    same portfolio tab.

    A plain st.dataframe with on_select="rerun" (not the hand-rendered
    render_screener_table, and not a per-row st.button column) -- native
    header-click sort stays correct regardless of how the table is
    currently sorted, which a *pre-sort*-indexed button column can't
    guarantee. Columns are kept as real numbers, formatted for display via
    column_config, so a header-click sort compares the underlying value
    (not a "₹10,81,452.10"-style string, which would sort alphabetically).

    `include_cc` gates the CC ROI/CC Assignment ROI columns -- the ETFs &
    Mutual Funds call site turns them off (covered-call suggestions there
    weren't useful enough to keep). `returns_pe_by_symbol` (keyed by
    symbol, each value a dict with return_1d/return_5d/return_20d/pe_ratio
    from snapshot_repo.get_latest_returns_and_pe) is optional -- only the
    ETFs & Mutual Funds call site passes it, adding the 1D/5D/20D value-
    change columns; the Stocks table stays as before. The period returns
    are percentages (daily_screener_snapshots has no stored historical
    price), so the rupee change is derived via value_change_from_pct
    (cur_val, return_pct) rather than read directly."""
    st.markdown(f"**{title}**")
    if not rows:
        st.caption("None.")
        return

    table_rows = []
    for r in rows:
        stock = r["symbol"] or f'{r["raw_name"]} (unmatched)'
        row_out = {
            "Stock": stock,
            "Qty": r["qty"],
            "Avg Price": r["avg_price"],
            "LTP": r["ltp"],
            "Investment": r["investment"],
            "Cur Val": r["cur_val"],
            "P&L": r["pnl"],
            "P&L %": r["pnl_pct"],
        }
        if include_cc:
            cc = cc_by_symbol.get(r["symbol"]) if r["symbol"] else None
            row_out["CC ROI"] = cc["cc_roi_pct"] if cc and cc["cc_roi_pct"] is not None else None
            row_out["CC Assignment ROI"] = cc["assignment_roi_pct"] if cc and cc["assignment_roi_pct"] is not None else None
        if returns_pe_by_symbol is not None:
            rp = returns_pe_by_symbol.get(r["symbol"]) if r["symbol"] else None
            cur_val = r["cur_val"]
            return_1d = rp["return_1d"] if rp else None
            return_5d = rp["return_5d"] if rp else None
            return_20d = rp["return_20d"] if rp else None
            row_out["1D Change"] = _fmt_value_change(value_change_from_pct(cur_val, return_1d), return_1d)
            row_out["5D Change"] = _fmt_value_change(value_change_from_pct(cur_val, return_5d), return_5d)
            row_out["20D Change"] = _fmt_value_change(value_change_from_pct(cur_val, return_20d), return_20d)
        table_rows.append(row_out)

    column_config = {
        "Qty": st.column_config.NumberColumn(format="%,.0f"),
        "Avg Price": st.column_config.NumberColumn(format="₹%,.2f"),
        "LTP": st.column_config.NumberColumn(format="₹%,.2f"),
        "Investment": st.column_config.NumberColumn(format="₹%,.2f"),
        "Cur Val": st.column_config.NumberColumn(format="₹%,.2f"),
        "P&L": st.column_config.NumberColumn(format="₹%,.2f"),
        "P&L %": st.column_config.NumberColumn(format="%+.2f%%"),
    }
    if include_cc:
        column_config["CC ROI"] = st.column_config.NumberColumn(format="%.2f%%")
        column_config["CC Assignment ROI"] = st.column_config.NumberColumn(format="%+.2f%%")

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


def _render_holdings_tab(portfolio_name: str, holdings_for_portfolio: list, cc_expiry_iso: str | None) -> None:
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
    rows, totals = portfolio_service.compute_portfolio_view(merged, ltp_by_symbol)
    rows.sort(key=lambda r: r["investment"], reverse=True)

    cc_chains = _load_covered_calls(symbols, cc_expiry_iso)
    cc_by_symbol: dict[str, dict | None] = {}
    for r in rows:
        symbol = r["symbol"]
        chain = cc_chains.get(symbol) if symbol else None
        cc_by_symbol[symbol] = (
            fo_service.covered_call_for_holding(
                chain["rows"], avg_price=r["avg_price"], ltp=r["ltp"], qty=r["qty"], expiry_date=chain["expiry_iso"]
            )
            if chain
            else None
        )

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

    st.caption(
        "CC ROI is the ROI when the call expires OTM. CC Assignment ROI is the ROI if the stock gets "
        "called away. In both cases the ROI is considered against the total invested amount of the "
        "stock, not just the margin. It is assumed that the stock is pledged and the pledge is used "
        "as margin."
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

    etf_symbols = tuple(sorted({r["symbol"] for r in etf_fund_rows if r["symbol"]}))
    returns_pe_by_symbol = load_returns_and_pe(client, etf_symbols, st.session_state["portfolio_cache_bust"])

    _render_holdings_table(
        title="ETFs & Mutual Funds",
        rows=etf_fund_rows,
        portfolio_name=portfolio_name,
        key_suffix="etf",
        cc_by_symbol=cc_by_symbol,
        include_cc=False,
        returns_pe_by_symbol=returns_pe_by_symbol,
    )
    _render_holdings_table(
        title="Stocks", rows=stock_rows, portfolio_name=portfolio_name, key_suffix="stock", cc_by_symbol=cc_by_symbol
    )


portfolio_names = sorted({h.portfolio_name for h in saved_holdings} | {p.portfolio_name for p in saved_positions})

if not portfolio_names:
    st.info("No portfolios yet -- go to My Broker to upload or connect one.")
else:
    # Options for the dropdown are the actual expiry dates present, taken
    # live from the system (option_contracts) rather than a fixed "Near
    # month" style label -- these shift every month as monthly contracts
    # expire and new ones are listed, so a hardcoded label would drift out
    # of sync with what's actually being priced. Union across every
    # symbol held anywhere in this account (not just the active tab) so
    # switching tabs never resets the choice.
    all_portfolio_symbols = tuple(sorted({h.symbol for h in saved_holdings if h.symbol}))
    try:
        all_expiries_by_symbol = load_option_expiries(client, all_portfolio_symbols, st.session_state["portfolio_cache_bust"])
    except APIError:
        all_expiries_by_symbol = {}
    cc_expiry_options = sorted({d for expiries in all_expiries_by_symbol.values() for d in expiries})[:3]

    if cc_expiry_options:
        if st.session_state.get("holdings_cc_expiry") not in cc_expiry_options:
            st.session_state["holdings_cc_expiry"] = cc_expiry_options[0]
        cc_expiry_iso = st.selectbox(
            "Covered call expiry",
            cc_expiry_options,
            key="holdings_cc_expiry",
            format_func=lambda d: date.fromisoformat(d).strftime("%b %Y"),
            help=(
                "Which monthly expiry the CC ROI / CC Assignment ROI columns below use. "
                "If avg buy price is above LTP, the strike targeted is ~3% above avg buy price; "
                "otherwise it's ~5% above LTP."
            ),
        )
    else:
        st.selectbox("Covered call expiry", ["N/A"], disabled=True)
        cc_expiry_iso = None

    tabs = st.tabs(portfolio_names)
    for name, tab in zip(portfolio_names, tabs):
        with tab:
            _render_holdings_tab(name, [h for h in saved_holdings if h.portfolio_name == name], cc_expiry_iso)
