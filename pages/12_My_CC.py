from __future__ import annotations

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
    load_live_broker_prices,
    load_position_meta,
    load_positions,
    load_returns_and_pe,
    load_trade_groups,
    load_trade_meta,
    slug,
)
from src.utils.refresh_bar import render_portfolio_refresh_button, render_stock_refresh_button
from src.utils.session import current_user_id, get_user_client_cached, require_login
from src.utils.ui import inject_global_styles, render_disclaimer

st.set_page_config(page_title="My CC | Nifty 50 Screener", page_icon="\U0001f6e1", layout="wide")
require_login()  # already injects Tailwind + the light-theme CSS design system

client = get_user_client_cached()
user_id = current_user_id()
user_settings = settings_repo.get_user_settings(client, user_id)
inject_global_styles(user_settings.theme)  # re-inject with the user's actual theme

st.title("\U0001f6e1 My CC")
render_disclaimer()
render_stock_refresh_button(client, user_id, user_settings.data_provider)
render_portfolio_refresh_button(client, user_id, user_settings.data_provider)
st.caption(
    'Every short call leg from a Trade whose Trade Type is "Portfolio CC". Go to My Trades, select a trade, '
    'click "Analyse Trade", and rename its Trade Type to "Portfolio CC" to have it show up here. Set each '
    'leg\'s Trade Date on that same "Analyse Trade" page to unlock Target Option P&L; Stop Loss ratchets up '
    "automatically as Option P&L% improves and is saved on every visit. Target Option P&L changes every day "
    "to reflect whether there has been higher-than-average decay in the option premium, but it never crosses "
    "95% of Credit. Option P&L shows a ✅ once it clears Target Option P&L, or a ❌ once it falls through Stop "
    "Loss. Stock P&L is the covered stock's own P&L, with a Target Stock P&L of 5% of its own investment "
    "(Avg Stock Price × Holding) -- Stock P&L shows a ✅ once it clears that target. Combined P&L adds Stock "
    "P&L to Option P&L."
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
    # "no corrected underlying label / custom trade type saved yet", which
    # just means nothing is tagged "Portfolio CC".
    saved_trade_meta = []

try:
    saved_position_meta = load_position_meta(client, user_id, st.session_state["portfolio_cache_bust"])
except APIError:
    # portfolio_position_meta doesn't exist yet (migration 0025) -- degrade
    # to "no Trade Date entered / Stop Loss computed yet" for every leg.
    saved_position_meta = []


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


def _fmt_target_pnl(target_pnl: float | None, credit: float | None) -> str:
    """"₹4,275.00 (85.00%)" -- Target Option P&L with what % of Credit it
    represents in parentheses, or an em dash before a Trade Date is set
    (Target Option P&L itself is None then)."""
    if target_pnl is None:
        return "—"
    if not credit:
        return format_inr(target_pnl)
    return f"{format_inr(target_pnl)} ({target_pnl / credit * 100:+.2f}%)"


def _fmt_target_stock_pnl(target_stock_pnl: float | None) -> str:
    """"₹4,275.00" -- Target Stock P&L, 5% of the stock's own investment
    (Avg Stock Price × Holding). No percentage in parentheses, unlike the
    other Target columns -- it's always exactly 5% of that same
    investment by definition, so restating it would be redundant. An em
    dash if the trade has no Holding leg to compute a cost basis from."""
    if target_stock_pnl is None:
        return "—"
    return format_inr(target_stock_pnl)


def _fmt_pnl(pnl: float | None, pnl_pct: float | None, target_pnl: float | None, stop_loss: float | None) -> str:
    """"✅ ₹1,234.56 (+12.34%)" once P&L has cleared Target P&L, "❌
    ₹1,234.56 (-8.00%)" once P&L has fallen through Stop Loss, or just
    the plain value with no marker when neither threshold is crossed
    (or there's nothing to compare against yet, e.g. no Trade Date)."""
    if pnl is None:
        return "—"
    if target_pnl is not None and pnl > target_pnl:
        marker = "✅ "
    elif stop_loss is not None and pnl < stop_loss:
        marker = "❌ "
    else:
        marker = ""
    if pnl_pct is None:
        return f"{marker}{format_inr(pnl)}"
    return f"{marker}{format_inr(pnl)} ({pnl_pct:+.2f}%)"


def _render_cc_table(
    *,
    title: str,
    trades: list[dict],
    portfolio_name: str,
    key_suffix: str,
    position_meta_for_portfolio: list,
) -> None:
    st.markdown(f"**{title}**")
    if not trades:
        st.caption("None.")
        return

    # One row per short-call Position leg. The covered stock itself isn't
    # its own row -- its qty/avg_price/ltp (Holding, Avg Stock Price,
    # Stock LTP) are looked up per-trade from that trade's own Holding
    # leg(s) instead, same as My CSP silently skips a stray Holding leg.
    # Aggregated (summed qty, investment-weighted avg price) in case a
    # trade holds the same underlying across more than one lot/broker.
    call_legs = []
    for t in trades:
        holding_legs = [leg for leg in t["legs"] if leg["leg_type"] == "Holding"]
        if holding_legs:
            holding_qty = sum(leg["qty"] for leg in holding_legs)
            total_investment = sum(leg["investment"] for leg in holding_legs)
            stock_avg_price = total_investment / holding_qty if holding_qty else None
        else:
            holding_qty = None
            stock_avg_price = None
        for leg in t["legs"]:
            if leg["leg_type"] == "Position":
                call_legs.append({**leg, "holding_qty": holding_qty, "stock_avg_price": stock_avg_price})

    if not call_legs:
        st.caption("No call legs found for these trades.")
        return

    symbols = tuple(sorted({leg["symbol"] for leg in call_legs if leg["symbol"]}))
    ltp_by_symbol = load_latest_prices(client, symbols, st.session_state["portfolio_cache_bust"])
    # Prefer a live quote from whichever broker(s) this portfolio has
    # connected over the (possibly stale, yfinance-sourced) screener
    # snapshot above -- only overrides symbols a connected broker actually
    # returned a price for; every other symbol (no broker connected, or
    # connected but that symbol/session didn't come back) keeps its
    # snapshot value. Same resolution used for both Stock LTP and the
    # Momentum/1D/5D/20D columns below, so they always agree.
    live_ltp_by_symbol = load_live_broker_prices(client, user_id, symbols, st.session_state["portfolio_cache_bust"])
    ltp_by_symbol = {**ltp_by_symbol, **live_ltp_by_symbol}
    returns_by_symbol = load_returns_and_pe(client, symbols, st.session_state["portfolio_cache_bust"])
    position_meta_by_leg = {(m.broker, m.raw_name): m for m in position_meta_for_portfolio}

    table_rows = []
    for leg in call_legs:
        rp = returns_by_symbol.get(leg["symbol"]) if leg["symbol"] else None
        return_1d = rp["return_1d"] if rp else None
        return_5d = rp["return_5d"] if rp else None
        return_20d = rp["return_20d"] if rp else None
        stock_ltp = ltp_by_symbol.get(leg["symbol"]) if leg["symbol"] else None
        stock_pnl = (
            (stock_ltp - leg["stock_avg_price"]) * leg["holding_qty"]
            if stock_ltp is not None and leg["stock_avg_price"] is not None and leg["holding_qty"] is not None
            else None
        )

        leg_meta = position_meta_by_leg.get((leg["broker"], leg["raw_name"]))
        trade_date = leg_meta.trade_date if leg_meta else None
        existing_stop_loss = leg_meta.stop_loss if leg_meta else None

        credit = portfolio_service.csp_max_credit(leg["avg_price"], leg["qty"])
        target_pnl = portfolio_service.csp_target_pnl(credit, trade_date, leg["expiry_date"])
        new_stop_loss = portfolio_service.csp_stop_loss(existing_stop_loss, credit, leg["pnl_pct"])
        if new_stop_loss is not None and (existing_stop_loss is None or abs(new_stop_loss - existing_stop_loss) > 1e-9):
            portfolio_repo.set_position_stop_loss(client, user_id, portfolio_name, leg["broker"], leg["raw_name"], new_stop_loss)

        # Stock's own investment (Avg Stock Price × Holding) -- the
        # denominator for both Stock P&L% and Combined P&L% (Combined
        # P&L is measured against the stock's own capital outlay, not
        # stock+option combined, since the option collateral is the
        # stock itself, not extra capital).
        stock_investment = (
            leg["holding_qty"] * leg["stock_avg_price"]
            if leg["holding_qty"] is not None and leg["stock_avg_price"] is not None
            else None
        )
        stock_pnl_pct = stock_pnl / stock_investment * 100 if stock_pnl is not None and stock_investment else None
        target_stock_pnl = 0.05 * stock_investment if stock_investment is not None else None

        combined_pnl = stock_pnl + leg["pnl"] if stock_pnl is not None and leg["pnl"] is not None else None
        combined_pnl_pct = (
            combined_pnl / stock_investment * 100 if combined_pnl is not None and stock_investment else None
        )

        table_rows.append(
            {
                "Trade Date": trade_date.strftime("%d %b %Y") if trade_date else None,
                "Underlying": leg["symbol"],
                "Holding": leg["holding_qty"],
                "Avg Stock Price": leg["stock_avg_price"],
                "Stock LTP": stock_ltp,
                "Stock P&L": _fmt_pnl(stock_pnl, stock_pnl_pct, target_stock_pnl, None),
                "Target Stock P&L": _fmt_target_stock_pnl(target_stock_pnl),
                "CC Expiry": leg["expiry_date"].strftime("%d %b %Y") if leg["expiry_date"] else None,
                "Strike": leg["strike_price"],
                "Qty": leg["qty"],
                "Avg Price": leg["avg_price"],
                "Credit": credit,
                "LTP": _fmt_ltp(leg["ltp"], leg.get("ltp_as_of")),
                "Option P&L": _fmt_pnl(leg["pnl"], leg["pnl_pct"], target_pnl, new_stop_loss),
                "Target Option P&L": _fmt_target_pnl(target_pnl, credit),
                "Stop Loss": new_stop_loss,
                "Combined P&L": _fmt_pnl(combined_pnl, combined_pnl_pct, None, None),
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
        key=f"cc_table_{key_suffix}_{slug(portfolio_name)}",
        column_config={
            "Holding": st.column_config.NumberColumn(format="%,.0f"),
            "Avg Stock Price": st.column_config.NumberColumn(format="₹%,.2f"),
            "Stock LTP": st.column_config.NumberColumn(format="₹%,.2f"),
            "Qty": st.column_config.NumberColumn(format="%+,.0f"),
            "Avg Price": st.column_config.NumberColumn(format="₹%,.2f"),
            "Credit": st.column_config.NumberColumn(format="₹%,.2f"),
            "1D": st.column_config.NumberColumn(format="%+.2f%%"),
            "5D": st.column_config.NumberColumn(format="%+.2f%%"),
            "20D": st.column_config.NumberColumn(format="%+.2f%%"),
            "Stop Loss": st.column_config.NumberColumn(format="₹%,.2f"),
        },
    )


def _render_cc_tab(
    portfolio_name: str,
    holdings_for_portfolio: list,
    positions_for_portfolio: list,
    trade_groups_for_portfolio: list,
    trade_meta_for_portfolio: list,
    position_meta_for_portfolio: list,
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
    cc_trades = [t for t in trades if portfolio_service.is_covered_call_trade_type(t["trade_type"])]

    stock_trades = [t for t in cc_trades if t["bucket"] == "stock"]
    index_trades = [t for t in cc_trades if t["bucket"] == "index"]
    other_trades = [t for t in cc_trades if t["bucket"] == "other"]

    _render_cc_table(
        title="Stock Trades",
        trades=stock_trades,
        portfolio_name=portfolio_name,
        key_suffix="stock",
        position_meta_for_portfolio=position_meta_for_portfolio,
    )
    _render_cc_table(
        title="Index Trades",
        trades=index_trades,
        portfolio_name=portfolio_name,
        key_suffix="index",
        position_meta_for_portfolio=position_meta_for_portfolio,
    )
    _render_cc_table(
        title="Other Trades",
        trades=other_trades,
        portfolio_name=portfolio_name,
        key_suffix="other",
        position_meta_for_portfolio=position_meta_for_portfolio,
    )


portfolio_names = sorted({h.portfolio_name for h in saved_holdings} | {p.portfolio_name for p in saved_positions})

if not portfolio_names:
    st.info("No portfolios yet -- go to Settings > Data Provider to connect a Dhan account.")
else:
    all_companies = load_all_companies(client, st.session_state["portfolio_cache_bust"])
    company_type_by_symbol = {c.symbol: c.company_type for c in all_companies}

    tabs = st.tabs(portfolio_names)
    for name, tab in zip(portfolio_names, tabs):
        with tab:
            _render_cc_tab(
                name,
                [h for h in saved_holdings if h.portfolio_name == name],
                [p for p in saved_positions if p.portfolio_name == name],
                [g for g in saved_trade_groups if g.portfolio_name == name],
                [m for m in saved_trade_meta if m.portfolio_name == name],
                [m for m in saved_position_meta if m.portfolio_name == name],
                company_type_by_symbol,
            )
