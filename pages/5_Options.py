from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from postgrest.exceptions import APIError

from src.repositories import companies_repo, fo_repo, settings_repo, snapshot_repo
from src.services import fo_service
from src.utils.formatting import format_inr, format_pct
from src.utils.session import current_user_id, get_user_client_cached, require_login
from src.utils.ui import inject_global_styles, plotly_template, render_disclaimer, render_stat_grid

st.set_page_config(page_title="Options & Futures | Nifty 50 Screener", page_icon="📊", layout="wide")
require_login()  # already injects Tailwind + the light-theme CSS design system

client = get_user_client_cached()
user_id = current_user_id()
user_settings = settings_repo.get_user_settings(client, user_id)
inject_global_styles(user_settings.theme)  # re-inject with the user's actual theme

st.title("📊 Options & Futures")
render_disclaimer()


def _fmt_int(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    return f"{int(value):,}"


# ---------------------------------------------------------------------
# Symbol selector -- defaults to whatever the Dashboard / Stock Detail
# handed off via session state.
# ---------------------------------------------------------------------
try:
    fo_symbols = fo_repo.list_fo_symbols(client)
except APIError:
    # The F&O tables/views (migration 0007) don't exist yet -- PostgREST
    # raises rather than returning an empty result. Show a setup hint
    # instead of a redacted crash page.
    st.info(
        "F&O data isn't set up yet. Apply migration "
        "`supabase/migrations/0007_add_fo_tables.sql`, then load data with "
        "`python scripts/fetch_fo_data.py --days 60`."
    )
    st.stop()

if not fo_symbols:
    fo_symbols = sorted(c.symbol for c in companies_repo.list_current_constituents(client))
if not fo_symbols:
    st.info("No F&O data loaded yet. Run `python scripts/fetch_fo_data.py` (or seed mock data).")
    st.stop()

default_symbol = (
    st.session_state.get("fo_symbol")
    or st.session_state.get("selected_symbol")
    or fo_symbols[0]
)
default_index = fo_symbols.index(default_symbol) if default_symbol in fo_symbols else 0
symbol = st.selectbox("Select a stock", fo_symbols, index=default_index)
st.session_state["fo_symbol"] = symbol

futures_rows = fo_repo.get_open_futures(client, symbol)
expiries = fo_repo.list_option_expiries(client, symbol)

if not futures_rows and not expiries:
    st.warning(
        "No open F&O contracts stored for this symbol yet. "
        "Load data with `python scripts/fetch_fo_data.py`."
    )
    st.stop()

# ---------------------------------------------------------------------
# Expiry selector (drives both the summary and the option chain)
# ---------------------------------------------------------------------
selected_expiry = None
if expiries:
    selected_expiry = st.selectbox(
        "Expiry", expiries, index=0, format_func=lambda d: d.strftime("%d %b %Y")
    )

chain_rows = fo_repo.get_option_chain(client, symbol, selected_expiry) if selected_expiry else []
summary = fo_service.option_chain_summary(chain_rows)

# ---------------------------------------------------------------------
# Summary tiles
# ---------------------------------------------------------------------
as_of = summary.get("trade_date")
if as_of is None and futures_rows:
    as_of = futures_rows[0].get("trade_date")
st.caption(f"End-of-day data · as of {as_of or '—'} (NSE F&O bhavcopy)")

pcr = summary.get("pcr")
stats = [
    ("Spot", format_inr(summary.get("spot")), "underlying"),
    ("ATM strike", format_inr(summary.get("atm_strike"), decimals=0), None),
    ("Total CE OI", _fmt_int(summary.get("total_ce_oi")), "calls"),
    ("Total PE OI", _fmt_int(summary.get("total_pe_oi")), "puts"),
    ("Put/Call ratio", f"{pcr:.2f}" if pcr is not None else "—", "PE OI ÷ CE OI"),
]
st.markdown(render_stat_grid(stats, user_settings.theme, cols=5), unsafe_allow_html=True)

# ---------------------------------------------------------------------
# Futures term structure
# ---------------------------------------------------------------------
st.divider()
st.subheader("Futures")
if futures_rows:
    term = fo_service.futures_term_structure(futures_rows)
    term_df = pd.DataFrame(
        [
            {
                "Expiry": r["expiry_date"],
                "Last price": format_inr(r["last_price"]),
                "Settlement": format_inr(r["settlement_price"]),
                "Basis vs spot": format_inr(r["basis"]),
                "Open interest": _fmt_int(r["open_interest"]),
                "Change in OI": _fmt_int(r["change_in_oi"]),
                "Volume": _fmt_int(r["volume"]),
                "Lot size": _fmt_int(r["lot_size"]),
            }
            for r in term
        ]
    )
    st.dataframe(term_df, use_container_width=True, hide_index=True)

    # Daily close chart for the near-month future.
    near = term[0]
    near_expiry = date.fromisoformat(near["expiry_date"]) if isinstance(near["expiry_date"], str) else near["expiry_date"]
    fut_hist = fo_repo.get_futures_daily(
        client, symbol, near_expiry, date.today() - timedelta(days=120), date.today()
    )
    if len(fut_hist) > 1:
        hist_df = pd.DataFrame([p.model_dump() for p in fut_hist])
        fig = go.Figure()
        fig.add_trace(
            go.Scatter(x=hist_df["trade_date"], y=hist_df["close"], mode="lines", name="Close")
        )
        fig.update_layout(
            template=plotly_template(user_settings.theme),
            height=280,
            margin=dict(l=10, r=10, t=30, b=10),
            title=f"{symbol} {near_expiry:%b %Y} futures — daily close",
        )
        st.plotly_chart(fig, use_container_width=True)
else:
    st.info("No open futures contracts for this symbol.")

# ---------------------------------------------------------------------
# Option chain (CE | Strike | PE)
# ---------------------------------------------------------------------
st.divider()
st.subheader("Option chain")
if not chain_rows:
    st.info("No option-chain data for this symbol/expiry yet.")
else:
    shaped = fo_service.shape_option_chain(chain_rows)
    atm = summary.get("atm_strike")
    chain_df = pd.DataFrame(
        [
            {
                "CE OI": _fmt_int(r.get("ce_oi")),
                "CE ΔOI": _fmt_int(r.get("ce_change_oi")),
                "CE Vol": _fmt_int(r.get("ce_volume")),
                "CE LTP": format_inr(r.get("ce_last")),
                "Strike": f"{r['strike']:,.0f}",
                "PE LTP": format_inr(r.get("pe_last")),
                "PE Vol": _fmt_int(r.get("pe_volume")),
                "PE ΔOI": _fmt_int(r.get("pe_change_oi")),
                "PE OI": _fmt_int(r.get("pe_oi")),
                "_strike_num": r["strike"],
            }
            for r in shaped
        ]
    )

    def _highlight_atm(row: pd.Series):
        if atm is not None and row["_strike_num"] == atm:
            return ["background-color: rgba(79, 70, 229, 0.12)"] * len(row)
        return [""] * len(row)

    styler = (
        chain_df.style.apply(_highlight_atm, axis=1)
        .hide(axis="index")
        .hide(subset=["_strike_num"], axis="columns")
    )
    st.dataframe(styler, use_container_width=True, height=520)
    st.caption("ATM strike highlighted. CE = calls (left), PE = puts (right). ΔOI = change in open interest.")

# ---------------------------------------------------------------------
# 5% CSP / 5% ITM PMCC -- both restricted to the nearest available
# expiry regardless of which expiry is selected above for viewing the
# chain, and both use the stock's cash-market latest_price as spot (not
# the option chain's own underlying_price, which is a snapshot from the
# F&O bhavcopy and can lag/differ from the cash price) -- matching
# exactly how the Dashboard's own columns for these two are computed, so
# the numbers always line up between the two screens.
# ---------------------------------------------------------------------
near_expiry = expiries[0] if expiries else None
if near_expiry is None:
    near_chain_rows = []
elif selected_expiry == near_expiry:
    near_chain_rows = chain_rows
else:
    near_chain_rows = fo_repo.get_option_chain(client, symbol, near_expiry)

screener_row = snapshot_repo.get_latest_screener_row(client, symbol)
cash_spot = screener_row.latest_price if screener_row else None
spot_map = {symbol: cash_spot} if cash_spot is not None else {}
csp = fo_service.csp_5pct_map(near_chain_rows, spot_map).get(symbol)
pmcc = fo_service.itm_pmcc_5pct_map(near_chain_rows, spot_map).get(symbol)

st.divider()
st.subheader("5% CSP (cash-secured put)")
st.caption(
    "A cash-secured-put yield: sell 1 lot of the near-expiry put whose strike is "
    "closest to 5% below spot, expressed as a percentage of that strike -- the "
    "full notional a cash-secured put seller sets aside per lot. Not divided by "
    "exchange margin, since NSE doesn't publish SPAN margin as a simple "
    "per-contract percentage."
)
if csp is None:
    st.info("Not enough option data to compute 5% CSP for the nearest expiry.")
else:
    csp_stats = [
        ("Spot", format_inr(csp["spot"]), None),
        ("Strike sold (nearest 5% below spot)", format_inr(csp["strike"], decimals=0), None),
        ("Put premium (LTP)", format_inr(csp["put_price"]), f"traded {csp['put_trade_date']}" if csp["put_trade_date"] else None),
        ("5% CSP", format_pct(csp["csp_pct"], signed=False), "put premium ÷ strike × 100"),
    ]
    st.markdown(render_stat_grid(csp_stats, user_settings.theme, cols=4), unsafe_allow_html=True)
    st.caption(f"Nearest expiry used: {csp['expiry_date']}")

st.divider()
st.subheader("5% ITM PMCC (poor man's covered call)")
st.caption(
    "A synthetic covered call built entirely from near-expiry options: buy 1 lot "
    "of the ITM call closest to spot, sell 1 lot of the put at that same strike, "
    "and sell 1 lot of the call whose strike is closest to 5% below the bought "
    "call's strike. Net credit = put sold + call sold − call bought, expressed as "
    "a percentage of the bought (ITM) call's strike."
)
if pmcc is None:
    st.info("Not enough option data to compute 5% ITM PMCC for the nearest expiry.")
else:
    leg_df = pd.DataFrame(
        [
            {
                "Leg": "Buy 1 CE (ITM, closest to spot)",
                "Strike": format_inr(pmcc["itm_ce_strike"], decimals=0),
                "Price": format_inr(pmcc["buy_ce_price"]),
                "Trade Date": pmcc["buy_ce_trade_date"] or "—",
                "Cash flow": f"-{format_inr(pmcc['buy_ce_price'])}",
            },
            {
                "Leg": "Sell 1 PE (same strike as ITM CE)",
                "Strike": format_inr(pmcc["itm_ce_strike"], decimals=0),
                "Price": format_inr(pmcc["sell_pe_price"]),
                "Trade Date": pmcc["sell_pe_trade_date"] or "—",
                "Cash flow": f"+{format_inr(pmcc['sell_pe_price'])}",
            },
            {
                "Leg": "Sell 1 CE (nearest 5% below ITM CE)",
                "Strike": format_inr(pmcc["otm_ce_strike"], decimals=0),
                "Price": format_inr(pmcc["sell_ce_price"]),
                "Trade Date": pmcc["sell_ce_trade_date"] or "—",
                "Cash flow": f"+{format_inr(pmcc['sell_ce_price'])}",
            },
        ]
    )
    st.dataframe(leg_df, use_container_width=True, hide_index=True)
    pmcc_stats = [
        ("Net credit", format_inr(pmcc["net_credit"]), "PE sold + CE sold − CE bought"),
        ("5% ITM PMCC", format_pct(pmcc["pmcc_pct"], signed=False), "net credit ÷ ITM CE strike × 100"),
    ]
    st.markdown(render_stat_grid(pmcc_stats, user_settings.theme, cols=2), unsafe_allow_html=True)
    st.caption(f"Nearest expiry used: {pmcc['expiry_date']}")
