from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st
from postgrest.exceptions import APIError
from pydantic import ValidationError

from src.calculations.classification import criterion_b
from src.repositories import portfolio_repo, settings_repo
from src.services import portfolio_service
from src.utils.formatting import format_inr, pass_fail_icon
from src.utils.portfolio_page import (
    build_trade_legs,
    ensure_cache_bust,
    load_all_companies,
    load_holdings,
    load_latest_prices,
    load_position_meta,
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
    '"Analyse Trade", and rename its Trade Type to "CSP" to have it show up here. Set each leg\'s Trade Date '
    "below to unlock Target P&L; Stop Loss ratchets up automatically as P&L% improves and is saved on every visit. "
    "Target P&L changes every day to reflect whether there has been higher-than-average decay in the option "
    "premium, but it never crosses 95% of Max Credit."
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

try:
    saved_position_meta = load_position_meta(client, user_id, st.session_state["portfolio_cache_bust"])
except APIError:
    # portfolio_position_meta doesn't exist yet (migration 0025) -- degrade
    # to "no Trade Date entered / Stop Loss computed yet" for every leg.
    saved_position_meta = []


def _fmt_breakeven(breakeven_price: float | None, breakeven_pct: float | None) -> str:
    """"₹22,455.00 (-2.27%)" -- the CSP breakeven price (Strike - Avg
    Price) with how far it sits from the underlying's current price in
    parentheses, or an em dash when either half is missing (unresolved
    underlying, or no LTP for it yet)."""
    if breakeven_price is None or breakeven_pct is None:
        return "—"
    return f"{format_inr(breakeven_price)} ({breakeven_pct:+.2f}%)"


def _fmt_ltp(ltp: float | None, ltp_as_of) -> str:
    """"₹4.40" for a live LTP, or "₹3.30 (as of 17 Aug 2026)" when it
    came from this app's own end-of-day F&O data instead of a live
    broker quote -- e.g. a Dhan sync whose Market Quote call 401'd
    (commonly a missing "Data APIs" subscription), which otherwise
    silently shows a stale close with nothing distinguishing it from a
    live tick. Same "(as of <date>)" convention the Dashboard already
    uses for a stale screener price."""
    if ltp is None:
        return "—"
    if ltp_as_of is None:
        return format_inr(ltp)
    return f"{format_inr(ltp)} (as of {ltp_as_of.strftime('%d %b %Y')})"


def _render_csp_tab(
    portfolio_name: str,
    holdings_for_portfolio: list,
    positions_for_portfolio: list,
    trade_groups_for_portfolio: list,
    trade_meta_for_portfolio: list,
    position_meta_for_portfolio: list,
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
    position_meta_by_leg = {(m.broker, m.raw_name): m for m in position_meta_for_portfolio}

    table_rows = []
    for leg in csp_legs:
        rp = returns_by_symbol.get(leg["symbol"]) if leg["symbol"] else None
        return_1d = rp["return_1d"] if rp else None
        return_5d = rp["return_5d"] if rp else None
        return_20d = rp["return_20d"] if rp else None
        underlying_ltp = ltp_by_symbol.get(leg["symbol"]) if leg["symbol"] else None
        breakeven_price = portfolio_service.csp_breakeven_price(leg["strike_price"], leg["avg_price"])
        breakeven_pct = portfolio_service.csp_breakeven_pct(breakeven_price, underlying_ltp)

        leg_meta = position_meta_by_leg.get((leg["broker"], leg["raw_name"]))
        trade_date = leg_meta.trade_date if leg_meta else None
        existing_stop_loss = leg_meta.stop_loss if leg_meta else None

        max_credit = portfolio_service.csp_max_credit(leg["avg_price"], leg["qty"])
        target_pnl = portfolio_service.csp_target_pnl(max_credit, trade_date, leg["expiry_date"])
        new_stop_loss = portfolio_service.csp_stop_loss(existing_stop_loss, max_credit, leg["pnl_pct"])
        if new_stop_loss is not None and (existing_stop_loss is None or abs(new_stop_loss - existing_stop_loss) > 1e-9):
            portfolio_repo.set_position_stop_loss(client, user_id, portfolio_name, leg["broker"], leg["raw_name"], new_stop_loss)

        table_rows.append(
            {
                "Trade Date": trade_date.strftime("%d %b %Y") if trade_date else None,
                "Underlying": leg["symbol"],
                "Expiry": leg["expiry_date"].strftime("%d %b %Y") if leg["expiry_date"] else None,
                "Strike": leg["strike_price"],
                "Qty": leg["qty"],
                "Avg Price": leg["avg_price"],
                "LTP": _fmt_ltp(leg["ltp"], leg.get("ltp_as_of")),
                "P&L": leg["pnl"],
                "P&L %": leg["pnl_pct"],
                "Target P&L": target_pnl,
                "Stop Loss": new_stop_loss,
                "Breakeven": _fmt_breakeven(breakeven_price, breakeven_pct),
                "LTP Underlying": underlying_ltp,
                # Same "Momentum" criterion (B) the Dashboard's screener
                # classifies every stock on -- 1D, 5D, AND 20D returns all
                # positive -- reused as-is (src.calculations.classification.
                # criterion_b) so this column always agrees with what the
                # Dashboard would show for the same underlying.
                "Momentum": pass_fail_icon(criterion_b(return_1d, return_5d, return_20d)),
                "1D": return_1d,
                "5D": return_5d,
                "20D": return_20d,
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
            "P&L": st.column_config.NumberColumn(format="₹%,.2f"),
            "P&L %": st.column_config.NumberColumn(format="%+.2f%%"),
            "LTP Underlying": st.column_config.NumberColumn(format="₹%,.2f"),
            "1D": st.column_config.NumberColumn(format="%+.2f%%"),
            "5D": st.column_config.NumberColumn(format="%+.2f%%"),
            "20D": st.column_config.NumberColumn(format="%+.2f%%"),
            "Target P&L": st.column_config.NumberColumn(format="₹%,.2f"),
            "Stop Loss": st.column_config.NumberColumn(format="₹%,.2f"),
        },
    )

    st.markdown("**Set Trade Date**")
    st.caption(
        "Target P&L needs this (there's no reliable \"entry date\" in any broker export/API for an "
        "already-open position, so it's entered here manually)."
    )
    # The Instrument picker lives OUTSIDE the form, deliberately: widgets
    # inside an st.form only report their new value to the rest of the
    # script on Submit, not on every interaction -- so if the date_input
    # below were also inside the form, picking a different Instrument
    # would never actually update the date field to that leg's own saved
    # Trade Date (it'd keep showing whatever was on screen from the
    # previously-selected leg, or Streamlit's own default of "keep the
    # widget's last value" once a `key` has already been used once --
    # confirmed live: switching Instrument silently left the old date in
    # place, making it look like the form couldn't actually target a
    # different CSP at all). Outside the form, this selectbox reruns the
    # script immediately on change, so the date_input's `value=` -- and
    # its `key`, which also varies per instrument so Streamlit treats it
    # as a genuinely fresh widget rather than reusing stale state -- are
    # freshly recomputed for whichever leg is now selected.
    instrument_options = [leg["raw_name"] for leg in csp_legs]
    selected_instrument = st.selectbox(
        "Instrument", instrument_options, key=f"csp_trade_date_select_{slug(portfolio_name)}"
    )
    selected_leg = next(leg for leg in csp_legs if leg["raw_name"] == selected_instrument)
    existing_meta = position_meta_by_leg.get((selected_leg["broker"], selected_leg["raw_name"]))
    with st.form(f"csp_trade_date_form_{slug(portfolio_name)}_{slug(selected_instrument)}"):
        new_trade_date = st.date_input(
            "Trade Date",
            value=existing_meta.trade_date if existing_meta else date.today(),
            key=f"csp_trade_date_input_{slug(portfolio_name)}_{slug(selected_instrument)}",
        )
        save_submitted = st.form_submit_button("Save")
    if save_submitted:
        portfolio_repo.set_position_trade_date(
            client, user_id, portfolio_name, selected_leg["broker"], selected_leg["raw_name"], new_trade_date
        )
        st.session_state["portfolio_cache_bust"] += 1
        st.cache_data.clear()
        st.success(f'Trade Date saved for "{selected_instrument}".')
        st.rerun()


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
                [m for m in saved_position_meta if m.portfolio_name == name],
                company_type_by_symbol,
            )
