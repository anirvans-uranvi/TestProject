"""My Portfolio Trades (formerly "My CC") -- every Trade whose Trade Type
carries the "Portfolio " prefix (portfolio_service.is_portfolio_trade_type):
Portfolio CC, Portfolio Strangle, Portfolio Jade Lizard, Portfolio Twisted
Sister, Portfolio IC, or any hand-typed label starting with "Portfolio ".
Renamed and rebuilt per an explicit user request, generalizing from
"Covered Call only" to every strategy that pairs option legs with a stock
holding -- the common thread across all of them is that the holding
changes the position's real risk profile, which is exactly what
classify_trade_type's "Portfolio " prefix already flags (see
src/services/portfolio_service.py).

One row per Trade (not one row per leg, unlike the old My CC table --
a Trade here can carry up to 4 option legs at once, not just a single
short call), with six column blocks:
1. Trade Details -- Underlying, Trade Type, Total P&L, Option P&L.
2. Stock Holding -- Avg Price/Qty/Invested/LTP/Momentum, aggregated
   across the trade's own Holding leg(s) same as the old My CC did.
3-6. PE Sell / PE Buy / CE Sell / CE Buy Leg -- Strike/Expiry/Avg
   Price/Qty/LTP for whichever single Position leg matches that slot
   (short PE / long PE / short CE / long CE respectively); blank if the
   trade has no leg of that shape.

Dropped entirely from the old My CC page: Trade Date/Target Option P&L/
Stop Loss/Credit -- that per-leg premium-decay-ratchet math is CSP/CC-
specific (Analyse Trade's own per-leg version still computes it
independently for any short option leg) and doesn't generalize cleanly
to a 4-leg spread (whose "target" or "stop" would it be?); the explicit
spec for this rebuild only asked for the six blocks above, so nothing
beyond them was invented here."""
from __future__ import annotations

import pandas as pd
import streamlit as st
from postgrest.exceptions import APIError
from pydantic import ValidationError

from src.calculations.classification import criterion_b
from src.models.enums import OptionType
from src.repositories import settings_repo
from src.services import portfolio_service
from src.utils.formatting import format_inr, pass_fail_icon
from src.utils.portfolio_page import (
    build_trade_legs,
    ensure_cache_bust,
    load_all_companies,
    load_holdings,
    load_latest_prices,
    load_live_broker_prices,
    load_positions,
    load_returns_and_pe,
    load_trade_groups,
    load_trade_meta,
    slug,
)
from src.utils.refresh_bar import render_portfolio_refresh_button, render_stock_refresh_button
from src.utils.session import current_user_id, get_user_client_cached, require_login
from src.utils.ui import inject_global_styles, render_disclaimer

st.set_page_config(page_title="My Portfolio Trades | Nifty 50 Screener", page_icon="\U0001f6e1", layout="wide")
require_login()  # already injects Tailwind + the light-theme CSS design system

client = get_user_client_cached()
user_id = current_user_id()
user_settings = settings_repo.get_user_settings(client, user_id)
inject_global_styles(user_settings.theme)  # re-inject with the user's actual theme

st.title("\U0001f6e1 My Portfolio Trades")
render_disclaimer()
render_stock_refresh_button(client, user_id, user_settings.data_provider)
render_portfolio_refresh_button(client, user_id, user_settings.data_provider)
st.caption(
    'Every Trade whose Trade Type starts with "Portfolio " -- Portfolio CC, Portfolio Strangle, Portfolio Jade '
    'Lizard, Portfolio Twisted Sister, Portfolio IC, or any custom label you type starting with "Portfolio " on '
    'Analyse Trade. These are exactly the Trades that pair a stock holding with option legs, so the holding\'s '
    "own numbers are shown alongside up to 4 option legs (PE Sell/PE Buy/CE Sell/CE Buy) -- blank where a trade "
    "has no leg of that shape. Go to My Trades, select a trade, click \"Analyse Trade\", and rename its Trade "
    "Type to start with \"Portfolio \" to have it show up here."
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
    # just means nothing is tagged "Portfolio ...".
    saved_trade_meta = []


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


# One slot per (option_type, side) combination this page shows a leg for --
# order here is the column order in the table (PE Sell/PE Buy/CE Sell/CE
# Buy, matching the spec). A leg matches a slot when its option_type and
# the sign of its qty (negative = short/sell, positive = long/buy) agree;
# a leg with option_type None (undecoded contract, or a futures leg)
# matches none of them and is simply left out of these 4 blocks (it still
# counts toward the trade's Total P&L/Option P&L above).
_LEG_SLOTS: list[tuple[str, OptionType, bool]] = [
    ("PE Sell", OptionType.PE, False),
    ("PE Buy", OptionType.PE, True),
    ("CE Sell", OptionType.CE, False),
    ("CE Buy", OptionType.CE, True),
]


def _slot_legs(position_legs: list[dict]) -> dict[str, list[dict]]:
    slots: dict[str, list[dict]] = {name: [] for name, _, _ in _LEG_SLOTS}
    for leg in position_legs:
        for name, option_type, is_long in _LEG_SLOTS:
            if leg.get("option_type") == option_type and (leg["qty"] > 0) == is_long:
                slots[name].append(leg)
                break
    return slots


def _render_portfolio_trades_table(*, title: str, trades: list[dict], key_suffix: str, portfolio_name: str) -> None:
    st.markdown(f"**{title}**")
    if not trades:
        st.caption("None.")
        return

    # Stock Holding block, aggregated per trade exactly like the old My CC
    # page did (summed qty, investment-weighted avg price, in case the
    # same underlying is split across more than one lot/broker within one
    # trade). `stock_symbol` is the first resolved symbol among the
    # trade's own Holding leg(s) -- used both for the live LTP/momentum
    # lookup below and as this row's "Underlying" identity.
    per_trade = []
    has_unshown_legs = False
    for t in trades:
        holding_legs = [leg for leg in t["legs"] if leg["leg_type"] == "Holding"]
        if holding_legs:
            holding_qty = sum(leg["qty"] for leg in holding_legs)
            total_investment = sum(leg["investment"] for leg in holding_legs)
            stock_avg_price = total_investment / holding_qty if holding_qty else None
            stock_symbol = next((leg["symbol"] for leg in holding_legs if leg["symbol"]), None)
        else:
            # A "Portfolio ..."-labeled trade with no actual Holding leg --
            # a hand-typed label that doesn't match its own legs (same
            # class of caveat the old My CC page had for a naked short
            # call mislabeled "Covered Call"). Stock Holding block stays
            # blank rather than guessing.
            holding_qty = total_investment = stock_avg_price = stock_symbol = None

        position_legs = [leg for leg in t["legs"] if leg["leg_type"] == "Position"]
        slots = _slot_legs(position_legs)
        if any(len(legs) > 1 for legs in slots.values()):
            has_unshown_legs = True

        per_trade.append(
            {
                "trade": t,
                "holding_qty": holding_qty,
                "total_investment": total_investment,
                "stock_avg_price": stock_avg_price,
                "stock_symbol": stock_symbol,
                "slots": slots,
            }
        )

    symbols = tuple(sorted({row["stock_symbol"] for row in per_trade if row["stock_symbol"]}))
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

    table_rows = []
    for row in per_trade:
        t = row["trade"]
        stock_symbol = row["stock_symbol"]
        rp = returns_by_symbol.get(stock_symbol) if stock_symbol else None
        return_1d = rp["return_1d"] if rp else None
        return_5d = rp["return_5d"] if rp else None
        return_20d = rp["return_20d"] if rp else None
        stock_ltp = ltp_by_symbol.get(stock_symbol) if stock_symbol else None

        table_row = {
            "Underlying": t["underlying_label"],
            "Trade Type": t["trade_type"] + (" ⚠️" if t["trade_type_mismatch"] else ""),
            "Total P&L": t["total_pnl"],
            "Option P&L": t["option_pnl"],
            "Stock Avg Price": row["stock_avg_price"],
            "Stock Qty": row["holding_qty"],
            "Stock Invested": row["total_investment"],
            "Stock LTP": stock_ltp,
            "Momentum": pass_fail_icon(criterion_b(return_1d, return_5d, return_20d)) if stock_symbol else "—",
            "1D": return_1d,
            "5D": return_5d,
            "20D": return_20d,
        }
        for name, _option_type, _is_long in _LEG_SLOTS:
            legs_here = row["slots"][name]
            leg = legs_here[0] if legs_here else None
            table_row[f"{name} Strike"] = leg["strike_price"] if leg else None
            table_row[f"{name} Expiry"] = leg["expiry_date"].strftime("%d %b %Y") if leg and leg["expiry_date"] else None
            table_row[f"{name} Avg Price"] = leg["avg_price"] if leg else None
            table_row[f"{name} Qty"] = leg["qty"] if leg else None
            table_row[f"{name} LTP"] = _fmt_ltp(leg["ltp"], leg.get("ltp_as_of")) if leg else "—"
        table_rows.append(table_row)

    if has_unshown_legs:
        st.caption(
            "⚠️ At least one trade below has more than one leg of the same shape (e.g. two short puts at "
            "different strikes) -- only the first is shown in that slot here. See My Trades/Analyse Trade for "
            "the full leg list."
        )

    column_config = {
        "Total P&L": st.column_config.NumberColumn(format="₹%,.2f"),
        "Option P&L": st.column_config.NumberColumn(format="₹%,.2f"),
        "Stock Avg Price": st.column_config.NumberColumn(format="₹%,.2f"),
        "Stock Qty": st.column_config.NumberColumn(format="%,.0f"),
        "Stock Invested": st.column_config.NumberColumn(format="₹%,.2f"),
        "Stock LTP": st.column_config.NumberColumn(format="₹%,.2f"),
        "1D": st.column_config.NumberColumn(format="%+.2f%%"),
        "5D": st.column_config.NumberColumn(format="%+.2f%%"),
        "20D": st.column_config.NumberColumn(format="%+.2f%%"),
    }
    for name, _option_type, _is_long in _LEG_SLOTS:
        column_config[f"{name} Avg Price"] = st.column_config.NumberColumn(format="₹%,.2f")
        column_config[f"{name} Qty"] = st.column_config.NumberColumn(format="%+,.0f")

    st.dataframe(
        pd.DataFrame(table_rows),
        use_container_width=True,
        hide_index=True,
        key=f"portfolio_trades_table_{key_suffix}_{slug(portfolio_name)}",
        column_config=column_config,
    )


def _render_portfolio_trades_tab(
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
    portfolio_trades = [t for t in trades if portfolio_service.is_portfolio_trade_type(t["trade_type"])]

    stock_trades = [t for t in portfolio_trades if t["bucket"] == "stock"]
    index_trades = [t for t in portfolio_trades if t["bucket"] == "index"]
    other_trades = [t for t in portfolio_trades if t["bucket"] == "other"]

    _render_portfolio_trades_table(title="Stock Trades", trades=stock_trades, key_suffix="stock", portfolio_name=portfolio_name)
    _render_portfolio_trades_table(title="Index Trades", trades=index_trades, key_suffix="index", portfolio_name=portfolio_name)
    _render_portfolio_trades_table(title="Other Trades", trades=other_trades, key_suffix="other", portfolio_name=portfolio_name)


portfolio_names = sorted({h.portfolio_name for h in saved_holdings} | {p.portfolio_name for p in saved_positions})

if not portfolio_names:
    st.info("No portfolios yet -- go to Settings > Data Provider to connect a Dhan account.")
else:
    all_companies = load_all_companies(client, st.session_state["portfolio_cache_bust"])
    company_type_by_symbol = {c.symbol: c.company_type for c in all_companies}

    tabs = st.tabs(portfolio_names)
    for name, tab in zip(portfolio_names, tabs):
        with tab:
            _render_portfolio_trades_tab(
                name,
                [h for h in saved_holdings if h.portfolio_name == name],
                [p for p in saved_positions if p.portfolio_name == name],
                [g for g in saved_trade_groups if g.portfolio_name == name],
                [m for m in saved_trade_meta if m.portfolio_name == name],
                company_type_by_symbol,
            )
