from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from src.repositories import companies_repo, fo_repo, settings_repo
from src.services import fo_service
from src.utils.formatting import format_inr
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
fo_symbols = fo_repo.list_fo_symbols(client)
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
