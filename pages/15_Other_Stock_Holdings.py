"""Other Stock Holdings -- every stock/ETF holding with **no option leg at
all** (a plain, uncovered position, across every broker within a Trade),
with a covered-call entry trigger per holding so writing a call against it
doesn't require a separate visit to Options per symbol. New page, added per
an explicit user request as part of the "Wheel Strategy" journey: Screener
for CSP -> My Current CSPs -> My Portfolio Trades (holdings *with* option
legs) -> **this page** (holdings with none).

**Stock bucket only** -- the page originally also showed Index Holdings
and Other Holdings tables (the same shape check's `index`/`other`
buckets), removed by explicit request; `_merged`/`holding_only_trades`
still compute across every bucket (unchanged, shared shape-check logic),
only the render call for the non-stock buckets was dropped.

A Trade lands here purely by *shape* -- `not any(leg["leg_type"] ==
"Position" for leg in t["legs"])` -- not by trusting the stored
`trade_type` string (which could be stale, e.g. still says "Portfolio CC"
after the option leg was actually closed, or a custom label like "Hedged"
that doesn't reflect a genuine option overlay). This is the same
build_trade_legs/group_into_trades pipeline every other Trades page uses,
just filtered differently.

The covered-call trigger reuses `fo_service.covered_call_for_holding`
unchanged -- the exact same per-holding formula already live on
`pages/5_Options.py`'s "Portfolio CC" section (avg-buy-price-vs-LTP-
dependent target, nearest strike, not a fixed 5%-OTM floor filter) --
confirmed with the user rather than inventing a new formula. This page
just renders it for every qualifying holding across the whole portfolio
at once, instead of requiring a per-symbol visit to Options.
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st
from postgrest.exceptions import APIError
from pydantic import ValidationError

from src.repositories import settings_repo
from src.services import fo_service, portfolio_service
from src.utils.formatting import format_inr, format_pct
from src.utils.portfolio_page import (
    build_trade_legs,
    ensure_cache_bust,
    fmt_qty,
    load_all_companies,
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

st.set_page_config(page_title="Other Stock Holdings | Nifty 50 Screener", page_icon="\U0001f4c8", layout="wide")
require_login()  # already injects Tailwind + the light-theme CSS design system

client = get_user_client_cached()
user_id = current_user_id()
user_settings = settings_repo.get_user_settings(client, user_id)
inject_global_styles(user_settings.theme)  # re-inject with the user's actual theme

st.title("\U0001f4c8 Other Stock Holdings")
render_disclaimer()
render_stock_refresh_button(client, user_id, user_settings.data_provider)
render_portfolio_refresh_button(client, user_id, user_settings.data_provider)
st.caption(
    "Every stock/ETF holding with no option trade against it at all -- i.e. a Trade whose current legs are "
    "entirely Holding legs, regardless of what its Trade Type is saved as. For each one, a covered-call "
    "suggestion broken out by near/next/far monthly expiry: if avg buy price is above the last traded price, "
    "the strike targeted is ~3% above avg buy price; otherwise it's ~5% above the last traded price -- picking "
    "whichever listed strike is nearest that target. The same calculation already shown per-symbol on the "
    "Options page's \"Portfolio CC\" section, gathered here for every holding at once."
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

_TERM_LABELS = ["Near month", "Next month", "Far month"]


def _render_holdings_cc_table(*, title: str, holdings: list[dict], key_suffix: str, portfolio_name: str) -> None:
    st.markdown(f"**{title}**")
    if not holdings:
        st.caption("None.")
        return

    symbols = tuple(sorted({h["symbol"] for h in holdings if h["symbol"]}))
    expiries_by_symbol = load_option_expiries(client, symbols, st.session_state["portfolio_cache_bust"])

    for holding in holdings:
        symbol = holding["symbol"]
        st.markdown(
            f"**{holding['underlying_label']}** -- {fmt_qty(holding['qty'])} shares @ "
            f"{format_inr(holding['avg_price'])} avg"
            + (f" · LTP {format_inr(holding['ltp'])}" if holding["ltp"] is not None else "")
        )
        expiries = [date.fromisoformat(d) for d in (expiries_by_symbol.get(symbol) or [])] if symbol else []
        if not symbol or holding["ltp"] is None or not expiries:
            st.caption("Not enough option data to compute a covered-call suggestion for this holding.")
            continue

        rows = []
        for label, exp in zip(_TERM_LABELS, expiries[:3]):
            chain_rows = load_option_chain(client, symbol, exp.isoformat(), st.session_state["portfolio_cache_bust"])
            cc = fo_service.covered_call_for_holding(chain_rows, holding["avg_price"], holding["ltp"], holding["qty"], exp)
            rows.append(
                {
                    "Term": label,
                    "Expiry": exp.strftime("%d %b %Y"),
                    "Strike": format_inr(cc["strike"], decimals=0) if cc else "N/A",
                    "Premium": format_inr(cc["premium_per_share"]) if cc and cc["premium_per_share"] is not None else "N/A",
                    "Trade Date": (cc["trade_date"] or "—") if cc else "N/A",
                    "Invested Amount": format_inr(cc["invested_amount"]) if cc else "N/A",
                    "CC ROI": format_pct(cc["cc_roi_pct"], signed=False) if cc and cc["cc_roi_pct"] is not None else "N/A",
                    "CC Assignment ROI": format_pct(cc["assignment_roi_pct"])
                    if cc and cc["assignment_roi_pct"] is not None
                    else "N/A",
                }
            )
        st.dataframe(
            pd.DataFrame(rows),
            use_container_width=True,
            hide_index=True,
            key=f"other_holdings_cc_{key_suffix}_{slug(portfolio_name)}_{slug(symbol or holding['underlying_label'])}",
        )


def _render_other_holdings_tab(
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
            "ltp": holding_legs[0]["ltp"],
        }

    # Only the Stock bucket is shown on this page (Index/Other Holdings
    # tables removed by request).
    stock_holdings = [_merged(t) for t in holding_only_trades if t["bucket"] == "stock"]

    _render_holdings_cc_table(title="Stock Holdings", holdings=stock_holdings, key_suffix="stock", portfolio_name=portfolio_name)


portfolio_names = sorted({h.portfolio_name for h in saved_holdings} | {p.portfolio_name for p in saved_positions})

if not portfolio_names:
    st.info("No portfolios yet -- go to Settings > Data Provider to connect a Dhan account.")
else:
    all_companies = load_all_companies(client, st.session_state["portfolio_cache_bust"])
    company_type_by_symbol = {c.symbol: c.company_type for c in all_companies}

    tabs = st.tabs(portfolio_names)
    for name, tab in zip(portfolio_names, tabs):
        with tab:
            _render_other_holdings_tab(
                name,
                [h for h in saved_holdings if h.portfolio_name == name],
                [p for p in saved_positions if p.portfolio_name == name],
                [g for g in saved_trade_groups if g.portfolio_name == name],
                [m for m in saved_trade_meta if m.portfolio_name == name],
                company_type_by_symbol,
            )
