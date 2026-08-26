from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st
from postgrest.exceptions import APIError
from pydantic import ValidationError

from src.calculations.classification import criterion_b
from src.models.enums import OptionType
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
from src.utils.refresh_bar import render_stock_refresh_button
from src.utils.session import current_user_id, get_user_client_cached, require_login
from src.utils.ui import inject_global_styles, render_disclaimer

st.set_page_config(page_title="Analyse Trade | Nifty 50 Screener", page_icon="\U0001f50d", layout="wide")
require_login()  # already injects Tailwind + the light-theme CSS design system

client = get_user_client_cached()
user_id = current_user_id()
user_settings = settings_repo.get_user_settings(client, user_id)
inject_global_styles(user_settings.theme)  # re-inject with the user's actual theme

st.title("\U0001f50d Analyse Trade")
render_disclaimer()
render_stock_refresh_button(client, user_id, user_settings.data_provider)

ensure_cache_bust()

trade_id = st.session_state.get("analyse_trade_id")
portfolio_name = st.session_state.get("analyse_trade_portfolio")

if not trade_id or not portfolio_name:
    st.info("No trade selected. Go to My Trades, select a row, and click \"Analyse Trade\".")
    if st.button("Go to My Trades"):
        st.switch_page("pages/7_My_Trades.py")
    st.stop()


def _fmt_breakeven(breakeven_price: float | None, breakeven_pct: float | None) -> str:
    """"₹22,455.00 (-2.27%)" -- the CSP breakeven price (Strike - Avg
    Price) with how far it sits from the underlying's current price in
    parentheses, or an em dash when either half is missing (not a short
    put leg, unresolved underlying, or no LTP for it yet)."""
    if breakeven_price is None or breakeven_pct is None:
        return "—"
    return f"{format_inr(breakeven_price)} ({breakeven_pct:+.2f}%)"


def _fmt_ltp(ltp: float | None, ltp_as_of) -> str:
    """"₹4.40" for a live LTP, or "₹3.30 (as of 17 Aug 2026)" when it
    came from this app's own end-of-day F&O data instead of a live
    broker quote."""
    if ltp is None:
        return "—"
    if ltp_as_of is None:
        return format_inr(ltp)
    return f"{format_inr(ltp)} (as of {ltp_as_of.strftime('%d %b %Y')})"


def _fmt_target_pnl(target_pnl: float | None, credit: float | None) -> str:
    """"₹4,275.00 (85.00%)" -- Target P&L with what % of Credit it
    represents in parentheses, or an em dash when there's nothing to
    compute one against (not a short option leg, or no Trade Date set)."""
    if target_pnl is None:
        return "—"
    if not credit:
        return format_inr(target_pnl)
    return f"{format_inr(target_pnl)} ({target_pnl / credit * 100:+.2f}%)"


def _fmt_pnl(pnl: float | None, pnl_pct: float | None, target_pnl: float | None, stop_loss: float | None) -> str:
    """"✅ ₹1,234.56 (+12.34%)" once P&L has cleared Target P&L, "❌
    ₹1,234.56 (-8.00%)" once P&L has fallen through Stop Loss, or just
    the plain value with no marker when neither threshold applies (not a
    short option leg, or nothing to compare against yet)."""
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


def _load_portfolio_data():
    try:
        holdings = load_holdings(client, user_id, st.session_state["portfolio_cache_bust"])
    except (APIError, ValidationError):
        holdings = []
    try:
        positions = load_positions(client, user_id, st.session_state["portfolio_cache_bust"])
    except APIError:
        positions = []
    try:
        trade_groups = load_trade_groups(client, user_id, st.session_state["portfolio_cache_bust"])
    except APIError:
        trade_groups = []
    try:
        trade_meta = load_trade_meta(client, user_id, st.session_state["portfolio_cache_bust"])
    except APIError:
        trade_meta = []
    return holdings, positions, trade_groups, trade_meta


saved_holdings, saved_positions, saved_trade_groups, saved_trade_meta = _load_portfolio_data()
holdings_for_portfolio = [h for h in saved_holdings if h.portfolio_name == portfolio_name]
positions_for_portfolio = [p for p in saved_positions if p.portfolio_name == portfolio_name]
trade_groups_for_portfolio = [g for g in saved_trade_groups if g.portfolio_name == portfolio_name]
trade_meta_for_portfolio = [m for m in saved_trade_meta if m.portfolio_name == portfolio_name]

all_companies = load_all_companies(client, st.session_state["portfolio_cache_bust"])
company_type_by_symbol = {c.symbol: c.company_type for c in all_companies}

legs = build_trade_legs(
    client, user_id, st.session_state["portfolio_cache_bust"], holdings_for_portfolio, positions_for_portfolio
)
overrides_by_leg = {(g.broker, g.raw_name): g.trade_id for g in trade_groups_for_portfolio}
trade_meta_by_id = {
    m.trade_id: {"underlying_label": m.underlying_label, "trade_type": m.trade_type, "bucket_override": m.bucket_override}
    for m in trade_meta_for_portfolio
}
trades = portfolio_service.group_into_trades(legs, overrides_by_leg, trade_meta_by_id, company_type_by_symbol)

trade = next((t for t in trades if t["trade_id"] == trade_id), None)

if trade is None:
    st.info(
        f'Trade "{trade_id}" in "{portfolio_name}" has no legs anymore -- it was probably fully split or '
        "the underlying data changed. Go back to My Trades and pick another one."
    )
    if st.button("Go to My Trades"):
        st.session_state.pop("analyse_trade_id", None)
        st.session_state.pop("analyse_trade_portfolio", None)
        st.switch_page("pages/7_My_Trades.py")
    st.stop()

st.subheader(f"{trade['underlying_label']} -- {portfolio_name}")
st.caption(f"Trade Type: {trade['trade_type']} | {trade['leg_count']} leg(s)")
st.caption(
    "Same columns as My CSP. Credit/Target P&L/Stop Loss only compute for a short option leg (any short "
    "CE/PE, not just a put); Breakeven is specifically the CSP breakeven (Strike - Avg Price), shown only "
    "for a short put leg -- blank for every other leg shape (a Holding, a future, a long option, a short "
    "call), same as this app's other CSP-formula reuses. LTP Underlying/Momentum/1D/5D/20D apply to every "
    "leg with a resolved symbol, holdings included."
)

# --- Legs table ---------------------------------------------------------
trade_legs = trade["legs"]

try:
    saved_position_meta = load_position_meta(client, user_id, st.session_state["portfolio_cache_bust"])
except APIError:
    # portfolio_position_meta doesn't exist yet (migration 0025) -- degrade
    # to "no Trade Date entered / Stop Loss computed yet" for every leg.
    saved_position_meta = []
position_meta_by_leg = {(m.broker, m.raw_name): m for m in saved_position_meta if m.portfolio_name == portfolio_name}

leg_symbols = tuple(sorted({leg["symbol"] for leg in trade_legs if leg["symbol"]}))
ltp_by_symbol = load_latest_prices(client, leg_symbols, st.session_state["portfolio_cache_bust"])
# Same broker-live-first, daily_screener_snapshots-fallback preference as
# My CSP/My CC's own "LTP Underlying" -- applies to every leg here
# (Holding or Position), since it's a fact about the underlying itself,
# not the leg's own instrument.
live_ltp_by_symbol = load_live_broker_prices(client, user_id, leg_symbols, st.session_state["portfolio_cache_bust"])
ltp_by_symbol = {**ltp_by_symbol, **live_ltp_by_symbol}
returns_by_symbol = load_returns_and_pe(client, leg_symbols, st.session_state["portfolio_cache_bust"])

leg_table_rows = []
for leg in trade_legs:
    leg_meta = position_meta_by_leg.get((leg["broker"], leg["raw_name"]))
    trade_date_for_leg = leg_meta.trade_date if leg_meta else None
    existing_stop_loss = leg_meta.stop_loss if leg_meta else None

    # "Credit"/decay-target/ratcheting-stop math is generic to any SHORT
    # option leg (see My CC's own reuse of the same csp_* functions for a
    # short call) -- but meaningless for a Holding, a future (no
    # option_type), or a long option (a debit, not a credit received).
    is_short_option_leg = (
        leg["leg_type"] == "Position"
        and leg.get("option_type") is not None
        and leg.get("strike_price") is not None
        and leg["qty"] < 0
    )
    credit = target_pnl = new_stop_loss = None
    if is_short_option_leg:
        credit = portfolio_service.csp_max_credit(leg["avg_price"], leg["qty"])
        target_pnl = portfolio_service.csp_target_pnl(credit, trade_date_for_leg, leg["expiry_date"])
        new_stop_loss = portfolio_service.csp_stop_loss(existing_stop_loss, credit, leg.get("pnl_pct"))
        if new_stop_loss is not None and (existing_stop_loss is None or abs(new_stop_loss - existing_stop_loss) > 1e-9):
            portfolio_repo.set_position_stop_loss(client, user_id, portfolio_name, leg["broker"], leg["raw_name"], new_stop_loss)

    # Breakeven is specifically the *CSP* breakeven (Strike - Avg Price) --
    # only a valid concept for a short put, unlike Credit/Target/Stop Loss
    # above. A short call, long option, future, or Holding leg shows "—".
    breakeven_price = breakeven_pct = None
    if is_short_option_leg and leg["option_type"] == OptionType.PE:
        underlying_ltp_for_breakeven = ltp_by_symbol.get(leg["symbol"]) if leg["symbol"] else None
        breakeven_price = portfolio_service.csp_breakeven_price(leg["strike_price"], leg["avg_price"])
        breakeven_pct = portfolio_service.csp_breakeven_pct(breakeven_price, underlying_ltp_for_breakeven)

    rp = returns_by_symbol.get(leg["symbol"]) if leg["symbol"] else None
    return_1d = rp["return_1d"] if rp else None
    return_5d = rp["return_5d"] if rp else None
    return_20d = rp["return_20d"] if rp else None

    leg_table_rows.append(
        {
            "Trade Date": trade_date_for_leg.strftime("%d %b %Y") if trade_date_for_leg else None,
            "Underlying": leg["symbol"] or f'{leg["raw_name"]} (unresolved)',
            "Expiry": leg["expiry_date"].strftime("%d %b %Y") if leg.get("expiry_date") else None,
            "Strike": leg.get("strike_price"),
            "Qty": leg["qty"],
            "Avg Price": leg["avg_price"],
            "Credit": credit,
            "LTP": _fmt_ltp(leg.get("ltp"), leg.get("ltp_as_of")),
            "P&L": _fmt_pnl(leg.get("pnl"), leg.get("pnl_pct"), target_pnl, new_stop_loss),
            "Target P&L": _fmt_target_pnl(target_pnl, credit),
            "Stop Loss": new_stop_loss,
            "Breakeven": _fmt_breakeven(breakeven_price, breakeven_pct),
            "LTP Underlying": ltp_by_symbol.get(leg["symbol"]) if leg["symbol"] else None,
            "Momentum": pass_fail_icon(criterion_b(return_1d, return_5d, return_20d)),
            "1D": return_1d,
            "5D": return_5d,
            "20D": return_20d,
        }
    )

legs_table_key = f"analyse_trade_legs_{slug(portfolio_name)}_{slug(trade_id)}"
legs_event = st.dataframe(
    pd.DataFrame(leg_table_rows),
    use_container_width=True,
    hide_index=True,
    on_select="rerun",
    selection_mode="multi-row",
    key=legs_table_key,
    column_config={
        "Qty": st.column_config.NumberColumn(format="%+,.2f"),
        "Avg Price": st.column_config.NumberColumn(format="₹%,.2f"),
        "Credit": st.column_config.NumberColumn(format="₹%,.2f"),
        "Stop Loss": st.column_config.NumberColumn(format="₹%,.2f"),
        "LTP Underlying": st.column_config.NumberColumn(format="₹%,.2f"),
        "1D": st.column_config.NumberColumn(format="%+.2f%%"),
        "5D": st.column_config.NumberColumn(format="%+.2f%%"),
        "20D": st.column_config.NumberColumn(format="%+.2f%%"),
    },
)

st.divider()

if trade["trade_type_mismatch"]:
    detected_type = portfolio_service.classify_trade_type(trade_legs)
    st.warning(
        f"This trade is saved as \"{trade['trade_type']}\", but its current legs now look like a "
        f"**{detected_type}** -- update Trade Type below if that's no longer right. Your saved label is never "
        "changed automatically."
    )

# --- Edit underlying / trade date / trade type / table ---------------------
st.markdown("**Correct the underlying, rename the trade type, or pin the table**")
_BUCKET_LABELS = {None: "Auto (based on underlying)", "stock": "Stock Trades", "index": "Index Trades", "other": "Other Trades"}
_BUCKET_VALUES = {label: value for value, label in _BUCKET_LABELS.items()}
current_bucket_override = trade_meta_by_id.get(trade_id, {}).get("bucket_override")

position_legs = [leg for leg in trade_legs if leg["leg_type"] == "Position"]
# position_meta_by_leg already loaded above, for the legs table's own
# per-leg Trade Date/Stop Loss columns -- reused here as-is.
# One Trade Date for the whole Trade, applied to every Position leg in it
# on Save -- multi-leg Trades (e.g. a strangle) are near-always entered as
# one package on one day, so this is simpler than picking a leg first.
# Shows the first leg's already-saved date, if any (they're expected to
# agree; if they don't yet, Save reconciles them to one value).
existing_trade_date = next(
    (position_meta_by_leg[(leg["broker"], leg["raw_name"])].trade_date for leg in position_legs
     if (leg["broker"], leg["raw_name"]) in position_meta_by_leg and position_meta_by_leg[(leg["broker"], leg["raw_name"])].trade_date),
    None,
)

with st.form(f"analyse_trade_edit_form_{slug(portfolio_name)}_{slug(trade_id)}"):
    edited_underlying = st.text_input(
        "Underlying Instrument",
        value=trade["underlying_label"],
        help='Free text -- e.g. correct a resolved "Tata Motors" to the real post-demerger underlying.',
    )
    if position_legs:
        edited_trade_date = st.date_input(
            "Trade Date",
            value=existing_trade_date or date.today(),
            help="Feeds My CSP's Target P&L calculation (there's no reliable \"entry date\" in any broker "
            "export/API for an already-open position, so it's entered here manually). Applied to every "
            "Position leg in this Trade.",
        )
    edited_trade_type = st.text_input("Trade Type", value=trade["trade_type"])
    edited_bucket_label = st.selectbox(
        "Table (on My Trades)",
        list(_BUCKET_LABELS.values()),
        index=list(_BUCKET_LABELS.keys()).index(current_bucket_override),
        help="Which of My Trades' three tables this trade shows in. \"Auto\" follows the underlying's own "
        "classification (e.g. an ETF defaults to Stock Trades even if it tracks an index) -- pin it here to "
        "override that, e.g. to keep an index ETF alongside Index Trades.",
    )
    save_submitted = st.form_submit_button("Save")
if save_submitted:
    underlying_to_save = edited_underlying.strip() or None
    if underlying_to_save == trade["default_underlying_label"]:
        underlying_to_save = None  # matches the auto-computed default -- no override needed
    portfolio_repo.set_trade_meta(
        client,
        user_id,
        portfolio_name,
        trade_id,
        underlying_label=underlying_to_save,
        trade_type=edited_trade_type.strip() or "Trade",
        bucket_override=_BUCKET_VALUES[edited_bucket_label],
    )
    if position_legs:
        for leg in position_legs:
            portfolio_repo.set_position_trade_date(client, user_id, portfolio_name, leg["broker"], leg["raw_name"], edited_trade_date)
    st.session_state["portfolio_cache_bust"] += 1
    st.cache_data.clear()
    st.success("Saved.")
    st.rerun()

st.divider()

# --- Merge / split --------------------------------------------------------
other_trade_ids = sorted(t["trade_id"] for t in trades if t["trade_id"] != trade_id)
merge_col, split_col = st.columns(2)

with merge_col:
    st.markdown("**Merge other trades into this one**")
    trades_to_merge = st.multiselect(
        "Other trades in this portfolio",
        other_trade_ids,
        key=f"analyse_trade_merge_select_{slug(portfolio_name)}_{slug(trade_id)}",
    )
    if st.button(
        "Merge into this trade", disabled=not trades_to_merge, key=f"analyse_trade_merge_btn_{slug(portfolio_name)}_{slug(trade_id)}"
    ):
        legs_to_move = [
            (leg["broker"], leg["raw_name"])
            for other in trades
            if other["trade_id"] in trades_to_merge
            for leg in other["legs"]
        ]
        portfolio_repo.set_trade_group(client, user_id, portfolio_name, legs_to_move, trade_id)
        st.session_state["portfolio_cache_bust"] += 1
        st.cache_data.clear()
        # The legs table's row-selection state is keyed by widget key, not
        # by the data it was computed against -- it survives st.rerun()
        # unchanged even though trade_legs is about to be a different
        # (here, larger) list. Clear it so a stale selection can't
        # silently point at the wrong leg -- or, after a split shrinks
        # trade_legs, past its end entirely (confirmed live: IndexError
        # on trade_legs[i] right after a successful split).
        st.session_state.pop(legs_table_key, None)
        st.success(f"Merged {len(trades_to_merge)} trade(s) into this one.")
        st.rerun()

with split_col:
    st.markdown("**Split selected legs out of this trade**")
    all_selected_rows = legs_event.selection.rows if legs_event and legs_event.selection else []
    # Defensive: the selection state can outlive the row it pointed to
    # (see the merge-button comment above) if some other path ever
    # reruns this page without clearing legs_table_key -- silently drop
    # any now-out-of-range index rather than crashing on trade_legs[i].
    selected_leg_rows = [i for i in all_selected_rows if i < len(trade_legs)]
    if not selected_leg_rows:
        st.caption("Select one or more legs above first.")
    else:
        selected_legs = [trade_legs[i] for i in selected_leg_rows]
        selected_leg_keys = [(leg["broker"], leg["raw_name"]) for leg in selected_legs]
        st.caption(f"{len(selected_legs)} leg(s) selected.")
        split_mode = st.radio(
            "Split to",
            ["Default grouping (by underlying)", "A new Trade ID"],
            key=f"analyse_trade_split_mode_{slug(portfolio_name)}_{slug(trade_id)}",
            horizontal=True,
        )
        if split_mode == "A new Trade ID":
            new_trade_id = st.text_input(
                "New Trade ID", key=f"analyse_trade_split_new_id_{slug(portfolio_name)}_{slug(trade_id)}"
            ).strip()
            if st.button(
                "Split into new trade",
                disabled=not new_trade_id,
                key=f"analyse_trade_split_new_btn_{slug(portfolio_name)}_{slug(trade_id)}",
            ):
                portfolio_repo.set_trade_group(client, user_id, portfolio_name, selected_leg_keys, new_trade_id)
                st.session_state["portfolio_cache_bust"] += 1
                st.cache_data.clear()
                st.session_state.pop(legs_table_key, None)  # trade_legs is about to shrink -- see merge button's comment
                st.success(f'Split {len(selected_legs)} leg(s) into "{new_trade_id}".')
                st.rerun()
        else:
            if st.button("Split to default grouping", key=f"analyse_trade_split_default_btn_{slug(portfolio_name)}_{slug(trade_id)}"):
                portfolio_repo.clear_trade_group_overrides(client, user_id, portfolio_name, selected_leg_keys)
                st.session_state["portfolio_cache_bust"] += 1
                st.cache_data.clear()
                st.session_state.pop(legs_table_key, None)  # trade_legs is about to shrink -- see merge button's comment
                st.success(f"Split {len(selected_legs)} leg(s) back to their default per-underlying Trade.")
                st.rerun()

st.divider()
if st.button("← Back to My Trades"):
    st.session_state.pop("analyse_trade_id", None)
    st.session_state.pop("analyse_trade_portfolio", None)
    st.switch_page("pages/7_My_Trades.py")
