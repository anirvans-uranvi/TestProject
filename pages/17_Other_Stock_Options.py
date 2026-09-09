"""Other Stock Options -- every stock-bucket Trade with **at least one
option leg** whose Trade Type is **neither "CSP" nor
"Portfolio "-prefixed** (`portfolio_service.is_other_trade_type`): a
bare Strangle/Jade Lizard/Twisted Sister/IC with no stock holding, a
custom label on a mixed holding+options Trade, or the default "Trade"
label on a naked option position. Added per an explicit user request as
a step in the "Wheel Strategy" journey: Screener for CSP -> My Current
CSPs -> My Portfolio Trades (holdings *with* option legs,
Portfolio-prefixed) -> **this page** (every other stock-options trade)
-> Other Stock Holdings (holdings with none). Originally named "Other
Stock Trades" and placed after Other Stock Holdings; renamed and moved
ahead of it per an explicit follow-up request -- purely a display-label
and `app.py` ordering change, the underlying filter/rendering are
unaffected.

**Plain holdings are excluded**, per an explicit follow-up request --
`any(leg["leg_type"] == "Position" for leg in t["legs"])`, a shape
check mirroring Other Stock Holdings' own signal, not a `trade_type`
match (the auto-classified default "Holding" label is itself neither
CSP nor Portfolio-prefixed, and would otherwise land here too). This
page and Other Stock Holdings now partition stock-bucket Trades
cleanly instead of a holding-only Trade showing on both.

There used to be a page just like this one, `pages/13_My_Other_Trades.py`
("Other Trades") -- a flat per-trade summary table (Underlying/Trade
Type/Legs/Total P&L) covering Stock *and* Other buckets, with a
row-select -> Analyse Trade flow, and no exclusion for plain holdings.
It was deleted entirely per an earlier, separate user request, along
with `portfolio_service.is_other_trade_type` (the exact predicate this
page reuses, restored here unchanged). This page is a deliberate
redesign, not a revival of the old one: **stock bucket only** (Index/
Other trades stay visible only via the unfiltered "All Trades" page
under My Portfolio, matching Other Stock Holdings' own scope decision),
**plain holdings excluded** (see above -- the old page didn't do this),
and built to the same **six-column-block** layout My Portfolio Trades
uses (Trade Details incl. Margin Required / Stock Holding / PE Sell /
PE Buy / CE Sell / CE Buy), rather than the old page's flatter summary
table -- an explicit user request to match that page's format.
`_render_other_stock_options_table`/`_LEG_SLOTS`/`_slot_legs`/`_fmt_ltp`
below are a page-local duplicate of My Portfolio Trades' own versions
(same "duplicated business logic across pages" tradeoff this app
already accepts elsewhere, e.g. Other Stock Holdings' covered-call
table vs. Options' Portfolio CC section) -- only the trade filter
differs.

**Margin Required** is the same live, Dhan-only figure My Portfolio
Trades shows: `DhanProvider.get_margin_for_legs` against the trade's own
option Position legs (not the Holding leg), via
`portfolio_page.load_trade_margin`. Needs a connected Dhan account (Data
Provider = Dhan); shows "N/A" otherwise, or if the call fails (expired
token, an unresolvable leg)."""
from __future__ import annotations

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
    load_positions,
    load_returns_and_pe,
    load_trade_groups,
    load_trade_margin,
    load_trade_meta,
    slug,
)
from src.utils.refresh_bar import render_portfolio_refresh_button, render_stock_refresh_button
from src.utils.session import current_user_id, get_user_client_cached, require_login
from src.utils.ui import inject_global_styles, render_disclaimer

st.set_page_config(page_title="Other Stock Options | Nifty 50 Screener", page_icon="\U0001f4cb", layout="wide")
require_login()  # already injects Tailwind + the light-theme CSS design system

client = get_user_client_cached()
user_id = current_user_id()
user_settings = settings_repo.get_user_settings(client, user_id)
inject_global_styles(user_settings.theme)  # re-inject with the user's actual theme

st.title("\U0001f4cb Other Stock Options")
render_disclaimer()
render_stock_refresh_button(client, user_id, user_settings.data_provider)
render_portfolio_refresh_button(client, user_id, user_settings.data_provider)
st.caption(
    'Every stock Trade with at least one option leg whose Trade Type is neither "CSP" nor starts with '
    '"Portfolio " -- a bare Strangle/Jade Lizard/Twisted Sister/IC with no stock holding, a custom label on a '
    "mixed holding+options Trade, or the default \"Trade\" label on a naked option position. Plain holdings "
    "(no option leg at all) aren't shown here -- see Other Stock Holdings for those. Index and Other-bucket "
    "trades aren't shown here either -- see the unfiltered \"All Trades\" page under My Portfolio. Go to My "
    "Trades, select a trade, click \"Analyse Trade\", and rename its Trade Type to \"CSP\" or to start with "
    "\"Portfolio \" to move it off this page instead. Same six-column layout as My Portfolio Trades: the "
    "holding's own numbers (blank if this trade has none) alongside up to 4 option legs (PE Sell/PE Buy/CE "
    'Sell/CE Buy) -- blank where a trade has no leg of that shape. "Margin Required" is Dhan\'s own combined '
    'margin-calculator figure for the trade\'s option legs (Data Provider = Dhan only; "N/A" otherwise).'
)

ensure_cache_bust()

# Margin Required needs a live Dhan call per trade (Dhan's own margin
# calculator, not stored data) -- only possible with a connected Dhan
# account. `dhan_connection` stays None for a yfinance_bhavcopy account
# or an unconnected Dhan one, and every trade's Margin Required column
# just shows "N/A" in that case rather than attempting a call with no
# credentials.
dhan_connection = None
if user_settings.data_provider == "dhan":
    try:
        dhan_connection = portfolio_repo.get_broker_connection(client, user_id, "Dhan")
    except APIError:
        dhan_connection = None

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
    # just means every trade defaults to "Trade" (and so lands here).
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
# Buy, matching My Portfolio Trades). A leg matches a slot when its
# option_type and the sign of its qty (negative = short/sell, positive =
# long/buy) agree; a leg with option_type None (undecoded contract, or a
# futures leg) matches none of them and is simply left out of these 4
# blocks (it still counts toward the trade's Total P&L/Option P&L above).
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


def _render_other_stock_options_table(*, trades: list[dict], portfolio_name: str, dhan_connection) -> None:
    if not trades:
        st.caption('No trades tagged neither "CSP" nor "Portfolio ..." yet for this portfolio.')
        return

    # Stock Holding block, aggregated per trade exactly like My Portfolio
    # Trades does (summed qty, investment-weighted avg price, in case the
    # same underlying is split across more than one lot/broker within one
    # trade). Most trades here have no Holding leg at all (that's what
    # sends a trade to My Portfolio Trades instead) -- this block simply
    # stays blank for those, same as a hand-typed "Portfolio ..." label
    # with no matching holding does on that page.
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
                "position_legs": position_legs,
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

        # Dhan's own combined margin-calculator figure (get_margin_for_legs)
        # for this trade's own option legs -- only possible with a
        # connected Dhan account; every other case (yfinance_bhavcopy,
        # unconnected, no resolvable legs, an expired token) shows "N/A"
        # rather than a number, same "degrade, don't crash" convention
        # every other live-Dhan-dependent figure on this page already uses.
        margin_required = None
        if dhan_connection is not None:
            legs_key = tuple(
                (
                    leg["symbol"],
                    leg["expiry_date"].isoformat() if leg["expiry_date"] else None,
                    leg["strike_price"],
                    str(leg["option_type"]) if leg["option_type"] else None,
                    leg["qty"],
                    leg["avg_price"],
                )
                for leg in row["position_legs"]
                if leg.get("symbol") and leg.get("option_type") is not None
            )
            if legs_key:
                margin_result = load_trade_margin(
                    client,
                    dhan_connection.client_id,
                    dhan_connection.access_token,
                    legs_key,
                    st.session_state["portfolio_cache_bust"],
                )
                if margin_result:
                    margin_required = margin_result.get("totalMargin")

        table_row = {
            "Underlying": t["underlying_label"],
            "Trade Type": t["trade_type"] + (" ⚠️" if t["trade_type_mismatch"] else ""),
            "Total P&L": t["total_pnl"],
            "Option P&L": t["option_pnl"],
            "Margin Required": format_inr(margin_required) if margin_required is not None else "N/A",
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
        key=f"other_stock_options_table_{slug(portfolio_name)}",
        column_config=column_config,
    )


def _render_other_stock_options_tab(
    portfolio_name: str,
    holdings_for_portfolio: list,
    positions_for_portfolio: list,
    trade_groups_for_portfolio: list,
    trade_meta_for_portfolio: list,
    company_type_by_symbol: dict,
    dhan_connection,
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
    other_trades = [t for t in trades if portfolio_service.is_other_trade_type(t["trade_type"])]

    # Exclude plain holdings (no option leg at all), per an explicit user
    # request -- those belong on Other Stock Holdings instead, regardless
    # of what trade_type string they happen to carry (most commonly the
    # auto-classified default "Holding", which is itself neither CSP nor
    # Portfolio-prefixed and would otherwise land here too). A shape
    # check, not a trade_type match, deliberately mirroring Other Stock
    # Holdings' own signal -- so this page and that one now partition
    # cleanly instead of overlapping on a holding-only Trade.
    other_trades = [t for t in other_trades if any(leg["leg_type"] == "Position" for leg in t["legs"])]

    # Stock bucket only, per an explicit user request -- an index or
    # ETF/other-underlying Trade of this shape is still visible only via
    # the unfiltered "All Trades" page (My Portfolio section), same scope
    # decision Other Stock Holdings already made for its own holdings.
    stock_option_trades = [t for t in other_trades if t["bucket"] == "stock"]

    _render_other_stock_options_table(trades=stock_option_trades, portfolio_name=portfolio_name, dhan_connection=dhan_connection)


portfolio_names = sorted({h.portfolio_name for h in saved_holdings} | {p.portfolio_name for p in saved_positions})

if not portfolio_names:
    st.info("No portfolios yet -- go to Settings > Data Provider to connect a Dhan account.")
else:
    all_companies = load_all_companies(client, st.session_state["portfolio_cache_bust"])
    company_type_by_symbol = {c.symbol: c.company_type for c in all_companies}

    tabs = st.tabs(portfolio_names)
    for name, tab in zip(portfolio_names, tabs):
        with tab:
            _render_other_stock_options_tab(
                name,
                [h for h in saved_holdings if h.portfolio_name == name],
                [p for p in saved_positions if p.portfolio_name == name],
                [g for g in saved_trade_groups if g.portfolio_name == name],
                [m for m in saved_trade_meta if m.portfolio_name == name],
                company_type_by_symbol,
                dhan_connection,
            )
