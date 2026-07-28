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
# 5% CSP / 5% CC -- both shown for the near/next/far monthly expiries
# regardless of which expiry is selected above for viewing the chain,
# and both use the stock's cash-market latest_price as spot (not the
# option chain's own underlying_price, which is a snapshot from the F&O
# bhavcopy and can lag/differ from the cash price) -- matching exactly
# how the Dashboard's own columns for these two are computed, so the
# numbers always line up between the two screens.
# ---------------------------------------------------------------------
screener_row = snapshot_repo.get_latest_screener_row(client, symbol)
cash_spot = screener_row.latest_price if screener_row else None


def _chain_rows_for(exp) -> list[dict]:
    return chain_rows if exp == selected_expiry else fo_repo.get_option_chain(client, symbol, exp)


st.divider()
st.subheader("5% CSP (cash-secured put)")
st.caption(
    "A cash-secured-put yield: sell 1 lot of the put whose strike is closest to "
    "5% below spot, expressed as a percentage of that strike -- the full notional "
    "a cash-secured put seller sets aside per lot. Not divided by exchange margin, "
    "since NSE doesn't publish SPAN margin as a simple per-contract percentage. "
    "Shown for the near, next, and far monthly expiries, same as the Futures "
    "term structure above."
)
if cash_spot is None or not expiries:
    st.info("Not enough option data to compute 5% CSP.")
else:
    term_labels = ["Near month", "Next month", "Far month"]
    csp_term_rows = []
    for label, exp in zip(term_labels, expiries[:3]):
        exp_csp = fo_service.csp_5pct_for_rows(_chain_rows_for(exp), cash_spot, exp)
        csp_term_rows.append(
            {
                "Term": label,
                "Expiry": exp.strftime("%d %b %Y"),
                "Spot": format_inr(exp_csp["spot"]) if exp_csp else "N/A",
                "Strike (nearest 5% below spot)": format_inr(exp_csp["strike"], decimals=0) if exp_csp else "N/A",
                "Put Premium": format_inr(exp_csp["put_price"]) if exp_csp else "N/A",
                "Trade Date": (exp_csp["put_trade_date"] or "—") if exp_csp else "N/A",
                "5% CSP": format_pct(exp_csp["csp_pct"], signed=False) if exp_csp and exp_csp["csp_pct"] is not None else "N/A",
            }
        )
    st.dataframe(pd.DataFrame(csp_term_rows), use_container_width=True, hide_index=True)

st.divider()
st.subheader("5% CC (covered call)")
st.caption(
    "A covered-call yield: sell 1 lot of the call whose strike is the "
    "lowest one still at or above 5% above spot (not merely the nearest "
    "strike to that line, which could round down below it). **Net "
    "Investment** = last traded price of the stock − premium collected -- "
    "the real capital outlay after the premium reduces it. **5% CC** = "
    "premium ÷ last traded price × 100 -- the yield on the stock itself, "
    "as if writing this call against shares you already hold. "
    "**Assignment Profit** = (strike ÷ Net Investment − 1) × 100 -- the "
    "total return of the whole covered-call trade if the shares are called "
    "away at the strike. Shown for the near, next, and far monthly "
    "expiries, same as 5% CSP above."
)
if cash_spot is None or not expiries:
    st.info("Not enough option data to compute 5% CC.")
else:
    term_labels = ["Near month", "Next month", "Far month"]
    cc_term_rows = []
    for label, exp in zip(term_labels, expiries[:3]):
        exp_cc = fo_service.cc_5pct_for_rows(_chain_rows_for(exp), cash_spot, exp)
        cc_term_rows.append(
            {
                "Term": label,
                "Expiry": exp.strftime("%d %b %Y"),
                "Spot": format_inr(exp_cc["spot"]) if exp_cc else "N/A",
                "Strike (lowest ≥5% above spot)": format_inr(exp_cc["strike"], decimals=0) if exp_cc else "N/A",
                "Premium": format_inr(exp_cc["premium"]) if exp_cc else "N/A",
                "Trade Date": (exp_cc["trade_date"] or "—") if exp_cc else "N/A",
                "Net Investment": format_inr(exp_cc["net_investment"]) if exp_cc and exp_cc["net_investment"] is not None else "N/A",
                "5% CC": format_pct(exp_cc["cc_pct"], signed=False) if exp_cc and exp_cc["cc_pct"] is not None else "N/A",
                "Assignment Profit": format_pct(exp_cc["assignment_profit_pct"], signed=False)
                if exp_cc and exp_cc["assignment_profit_pct"] is not None
                else "N/A",
            }
        )
    st.dataframe(pd.DataFrame(cc_term_rows), use_container_width=True, hide_index=True)
