"""Planner for CCs -- every stock/ETF holding with **no option leg at
all** (a plain, uncovered position, across every broker within a Trade),
laid out as one flat table (Stock/Avg Price/Qty/Invested Amt/LTP/
Dividend/PEG/Fundamentals/1D/5D/20D/Momentum, then a CC Strike/CC ROI
column pair per near/next/far monthly expiry) so a covered-call decision
never requires a separate visit to Options per symbol. Nested in the
"Wheel Strategy" journey right after Modified CSPs: Screener for CSP ->
My Current CSPs -> Modified CSPs -> **this page** -> My Portfolio Trades
(holdings *with* option legs).

**A full rebuild of the earlier "Other Stock Holdings" page**, per an
explicit user request -- same name, same journey slot conceptually
(the "plain holdings" step), same underlying shape-check filter
(`not any(leg["leg_type"] == "Position" for leg in t["legs"])`, not the
saved `trade_type` string, for the exact reason the old page's own
docstring already gave: a stale/custom label shouldn't be able to hide
an uncovered holding or wrongly include one that's actually covered
now), but with everything else about the page rewritten:
- **Renamed** "Other Stock Holdings" -> "Planner for CCs" and **moved**
  from after My Portfolio Trades to right after Modified CSPs.
- **One flat table instead of one small table per holding** -- the old
  page rendered a separate Term/Expiry/Strike/.../CC Assignment ROI
  table under each holding's own markdown line; this page instead
  renders every holding as one row, with a CC Strike/CC ROI column pair
  per expiry, the same "flatten near/next/far into paired columns"
  layout the Screener for CSP page's own CSP columns already use.
- **Dividend/PEG/Fundamentals/Momentum added**, sourced the same way
  the Screener does -- pre-classified `criterion_a`/`criterion_b`/
  `criterion_c` and `ttm_dividend_yield`/`peg_ratio` read directly from
  `daily_screener_snapshots` (`snapshot_repo.get_latest_fundamentals_and_returns`),
  not recomputed, so these cells always agree with what the Screener
  would show for the same stock. Momentum in particular is **not** the
  freshly-recomputed `criterion_b(return_1d, return_5d, return_20d)`
  every other portfolio page uses (My CSP, My Portfolio Trades, Modified
  CSPs) -- it's the stored flag, per an explicit request that this
  column match the Screener specifically.
- **A new, separate covered-call target formula**,
  `fo_service.planner_cc_for_holding` -- deliberately the *opposite*
  condition/base pairing from the old page's `covered_call_for_holding`
  (which is otherwise untouched, still live on the Options page's own
  "Portfolio CC" section): a **profit** (LTP above avg buy price) now
  targets 3% above **LTP**; a **loss or exact breakeven** targets 5%
  above **avg buy price**. Two genuinely different planning tools by
  design, confirmed as an explicit, deliberate instruction for this page
  -- not a correction of the old formula.
- **Row selection + "Open in Stock Detail"/"Open in Options" buttons**,
  reusing the exact same mechanism and session-state keys
  (`selected_symbol`/`fo_symbol`) the Screener for CSP page's own table
  uses, rather than a manual per-row button column."""
from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st
from postgrest.exceptions import APIError
from pydantic import ValidationError

from src.calculations.classification import criterion_fundamentals
from src.repositories import settings_repo
from src.services import fo_service, portfolio_service
from src.utils.formatting import format_pct, pass_fail_icon
from src.utils.portfolio_page import (
    build_trade_legs,
    ensure_cache_bust,
    load_all_companies,
    load_fundamentals_and_returns,
    load_holdings,
    load_option_chain,
    load_option_expiries,
    load_positions,
    load_trade_groups,
    load_trade_meta,
    slug,
)
from src.utils.refresh_bar import render_portfolio_refresh_button, render_stock_refresh_button
from src.utils.session import current_user_id, get_user_client_cached, require_login
from src.utils.ui import inject_global_styles, render_disclaimer

st.set_page_config(page_title="Planner for CCs | Nifty 50 Screener", page_icon="\U0001f4c8", layout="wide")
require_login()  # already injects Tailwind + the light-theme CSS design system

client = get_user_client_cached()
user_id = current_user_id()
user_settings = settings_repo.get_user_settings(client, user_id)
inject_global_styles(user_settings.theme)  # re-inject with the user's actual theme

st.title("\U0001f4c8 Planner for CCs")
render_disclaimer()
render_stock_refresh_button(client, user_id, user_settings.data_provider)
render_portfolio_refresh_button(client, user_id, user_settings.data_provider)
st.caption(
    "Every stock holding with no option trade against it at all -- i.e. a Trade whose current legs are "
    "entirely Holding legs, regardless of what its Trade Type is saved as. Dividend/PEG/Fundamentals/Momentum "
    "are the same pre-classified flags the Screener for CSP page shows. For each of the near/next/far monthly "
    "expiries: if LTP is above avg buy price, CC Strike targets 3% above LTP; otherwise it targets 5% above "
    "avg buy price -- picking whichever listed strike is nearest that target. Select a row to open that stock "
    "in Stock Detail or Options."
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
    # "no corrected underlying label / custom trade type saved yet" --
    # irrelevant here anyway, since this page filters by leg shape, not
    # the saved trade_type string.
    saved_trade_meta = []


def _render_planner_table(*, holdings: list[dict], portfolio_name: str) -> None:
    if not holdings:
        st.caption("No plain stock holdings (with no option leg) for this portfolio.")
        return

    symbols = tuple(sorted({h["symbol"] for h in holdings if h["symbol"]}))
    fundamentals_by_symbol = load_fundamentals_and_returns(client, symbols, st.session_state["portfolio_cache_bust"])

    # Every NSE stock option shares one monthly expiry calendar (confirmed
    # with the user during the Screener for CSP redesign), so the union of
    # every holding's own near/next/far expiries is, in practice, the same
    # 3 dates regardless of which symbol they came from -- computed once
    # here rather than per-holding, the same "shared 3 expiries" approach
    # the Screener's own CSP columns use.
    expiries_by_symbol = load_option_expiries(client, symbols, st.session_state["portfolio_cache_bust"])
    all_expiries = sorted({e for exps in expiries_by_symbol.values() for e in exps})[:3]
    month_labels = [date.fromisoformat(e).strftime("%b") for e in all_expiries]

    table_rows = []
    for holding in holdings:
        symbol = holding["symbol"]
        avg_price = holding["avg_price"]
        qty = holding["qty"]
        ltp = holding["ltp"]
        fund = fundamentals_by_symbol.get(symbol) or {}

        row = {
            "Stock": holding["underlying_label"],
            "Avg Price": avg_price,
            "Qty": qty,
            "Invested Amt": holding["investment"],
            "LTP": ltp,
            "Dividend": f"{format_pct(fund.get('ttm_dividend_yield'), signed=False)} {pass_fail_icon(fund.get('criterion_a'))}",
            "PEG": f"{fund['peg_ratio']:.2f} {pass_fail_icon(fund.get('criterion_c'))}" if fund.get("peg_ratio") is not None else "N/A",
            "Fundamentals": pass_fail_icon(criterion_fundamentals(fund.get("criterion_a"), fund.get("criterion_c"))),
            "1D": fund.get("return_1d"),
            "5D": fund.get("return_5d"),
            "20D": fund.get("return_20d"),
            "Momentum": pass_fail_icon(fund.get("criterion_b")),
        }
        for label, expiry_iso in zip(month_labels, all_expiries):
            cc = None
            if symbol and avg_price is not None and ltp is not None:
                chain_rows = load_option_chain(client, symbol, expiry_iso, st.session_state["portfolio_cache_bust"])
                cc = fo_service.planner_cc_for_holding(chain_rows, avg_price, ltp, qty, date.fromisoformat(expiry_iso))
            row[f"{label} CC Strike"] = cc["strike"] if cc else None
            row[f"{label} CC ROI"] = cc["cc_roi_pct"] if cc else None
        table_rows.append(row)

    column_config = {
        "Avg Price": st.column_config.NumberColumn(format="₹%,.2f"),
        "Qty": st.column_config.NumberColumn(format="%,.0f"),
        "Invested Amt": st.column_config.NumberColumn(format="₹%,.2f"),
        "LTP": st.column_config.NumberColumn(format="₹%,.2f"),
        "1D": st.column_config.NumberColumn(format="%+.2f%%"),
        "5D": st.column_config.NumberColumn(format="%+.2f%%"),
        "20D": st.column_config.NumberColumn(format="%+.2f%%"),
    }
    for label in month_labels:
        column_config[f"{label} CC Strike"] = st.column_config.NumberColumn(format="₹%,.0f")
        column_config[f"{label} CC ROI"] = st.column_config.NumberColumn(format="%.2f%%")

    # Row selection (same mechanism the Screener for CSP page's own table
    # uses) replaces a manual per-row button column -- Streamlit maps a
    # click back to the correct row regardless of how the table is
    # currently sorted in the browser.
    event = st.dataframe(
        pd.DataFrame(table_rows),
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key=f"planner_cc_table_{slug(portfolio_name)}",
        column_config=column_config,
    )
    selected_rows = event.selection.rows if event and event.selection else []
    if selected_rows:
        selected_symbol = table_rows[selected_rows[0]]["Stock"]
        detail_col, options_col = st.columns(2)
        with detail_col:
            if st.button(f"Open {selected_symbol} in Stock Detail", key=f"planner_cc_open_detail_{slug(portfolio_name)}"):
                st.session_state["selected_symbol"] = selected_symbol
                st.switch_page("pages/2_Stock_Detail.py")
        with options_col:
            if st.button(f"Open {selected_symbol} in Options", key=f"planner_cc_open_options_{slug(portfolio_name)}"):
                st.session_state["fo_symbol"] = selected_symbol
                st.switch_page("pages/5_Options.py")


def _render_planner_tab(
    portfolio_name: str,
    holdings_for_portfolio: list,
    positions_for_portfolio: list,
    trade_groups_for_portfolio: list,
    trade_meta_for_portfolio: list,
    company_type_by_symbol: dict,
) -> None:
    legs = build_trade_legs(
        client, user_id, st.session_state["portfolio_cache_bust"], holdings_for_portfolio, positions_for_portfolio
    )
    if not legs:
        st.caption("No holdings or positions synced yet for this portfolio -- connect a broker in Settings > Data Provider.")
        return

    overrides_by_leg = {(g.broker, g.raw_name): g.trade_id for g in trade_groups_for_portfolio}
    trade_meta_by_id = {
        m.trade_id: {"underlying_label": m.underlying_label, "trade_type": m.trade_type, "bucket_override": m.bucket_override}
        for m in trade_meta_for_portfolio
    }
    trades = portfolio_service.group_into_trades(legs, overrides_by_leg, trade_meta_by_id, company_type_by_symbol)
    # Shape check, not a trust in the saved trade_type string -- a Trade
    # belongs here iff it currently has zero Position legs at all.
    holding_only_trades = [t for t in trades if not any(leg["leg_type"] == "Position" for leg in t["legs"])]

    def _merged(t: dict) -> dict:
        holding_legs = t["legs"]  # every leg is a Holding leg, per the filter above
        qty = sum(leg["qty"] for leg in holding_legs)
        investment = sum(leg["investment"] for leg in holding_legs)
        avg_price = investment / qty if qty else None
        # Every Holding leg for one Trade shares the same underlying, so
        # any leg's own already-resolved ltp/symbol apply to the merged row.
        return {
            "underlying_label": t["underlying_label"],
            "symbol": holding_legs[0]["symbol"],
            "qty": qty,
            "avg_price": avg_price,
            "investment": investment,
            "ltp": holding_legs[0]["ltp"],
        }

    # Stock bucket only, matching the old page's own scope.
    stock_holdings = [_merged(t) for t in holding_only_trades if t["bucket"] == "stock"]

    _render_planner_table(holdings=stock_holdings, portfolio_name=portfolio_name)


portfolio_names = sorted({h.portfolio_name for h in saved_holdings} | {p.portfolio_name for p in saved_positions})

if not portfolio_names:
    st.info("No portfolios yet -- go to Settings > Data Provider to connect a Dhan account.")
else:
    all_companies = load_all_companies(client, st.session_state["portfolio_cache_bust"])
    company_type_by_symbol = {c.symbol: c.company_type for c in all_companies}

    tabs = st.tabs(portfolio_names)
    for name, tab in zip(portfolio_names, tabs):
        with tab:
            _render_planner_tab(
                name,
                [h for h in saved_holdings if h.portfolio_name == name],
                [p for p in saved_positions if p.portfolio_name == name],
                [g for g in saved_trade_groups if g.portfolio_name == name],
                [m for m in saved_trade_meta if m.portfolio_name == name],
                company_type_by_symbol,
            )
