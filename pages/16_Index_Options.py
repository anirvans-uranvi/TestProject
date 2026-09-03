"""Index Options -- quick short-strangle ideas (sell OTM PE + sell OTM CE)
on the 4 major indices, new page added per an explicit user request as its
own top-level section alongside "Wheel Strategy". Not a per-user portfolio
view -- reads the same public NSE/BSE F&O bhavcopy data every other F&O
page already reads (`option_contracts`/`latest_option_chain_view`), just
for 4 fixed index symbols instead of one stock at a time. No new
migration, no new ingestion.

NIFTY and SENSEX still carry both weekly *and* monthly expiry cadence, so
they get 3 rows each (current week/next week/current month, at 3%/4%/5%
away from spot respectively -- wider term, wider strangle); BANKNIFTY and
FINNIFTY are monthly-only now (confirmed live via `fo_repo.list_option_expiries`
while building this), so they get exactly 1 row (current month, at 5%).
`fo_service.classify_index_expiry_terms` picks the actual dates out of each
symbol's own listed expiries; `fo_service.index_strangle_for_expiry` finds
the nearest listed strike to each side's target and the combined credit.
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st
from postgrest.exceptions import APIError

from src.repositories import settings_repo
from src.services import fo_service
from src.utils.formatting import format_inr
from src.utils.portfolio_page import ensure_cache_bust, load_option_chain, load_option_expiries
from src.utils.refresh_bar import render_stock_refresh_button
from src.utils.session import current_user_id, get_user_client_cached, require_login
from src.utils.ui import inject_global_styles, render_disclaimer

st.set_page_config(page_title="Index Options | Nifty 50 Screener", page_icon="\U0001f4ca", layout="wide")
require_login()  # already injects Tailwind + the light-theme CSS design system

client = get_user_client_cached()
user_id = current_user_id()
user_settings = settings_repo.get_user_settings(client, user_id)
inject_global_styles(user_settings.theme)  # re-inject with the user's actual theme

st.title("\U0001f4ca Index Options")
render_disclaimer()
render_stock_refresh_button(client, user_id, user_settings.data_provider)
st.caption(
    "Short-strangle ideas on the 4 major indices -- for each, the listed strike nearest **x% away from the "
    "current price** on both the put side (sell) and the call side (sell), where x is 3% for the current "
    "week's expiry, 4% for next week's, and 5% for an expiry further out. NIFTY and SENSEX still have weekly "
    "expiries, so all three show as separate rows; Bank Nifty and Fin Nifty are monthly-only now, so only the "
    "current month's row shows. End-of-day NSE/BSE bhavcopy data, same as every other F&O page here -- not a "
    "live quote."
)

ensure_cache_bust()

_INDEX_TERM_PCT: dict[str, dict[str, float]] = {
    "NIFTY": {"current_week": 3.0, "next_week": 4.0, "current_month": 5.0},
    "SENSEX": {"current_week": 3.0, "next_week": 4.0, "current_month": 5.0},
    "BANKNIFTY": {"current_month": 5.0},
    "FINNIFTY": {"current_month": 5.0},
}
_TERM_LABELS = {"current_week": "Current Week", "next_week": "Next Week", "current_month": "Current Month"}

try:
    expiries_by_symbol = load_option_expiries(
        client, tuple(_INDEX_TERM_PCT.keys()), st.session_state["portfolio_cache_bust"]
    )
except APIError:
    st.info(
        "F&O data isn't set up yet. Apply migration "
        "`supabase/migrations/0007_add_fo_tables.sql`, then load data with "
        "`python scripts/fetch_fo_data.py --days 60`."
    )
    st.stop()

for symbol, term_pct in _INDEX_TERM_PCT.items():
    st.subheader(symbol)
    expiries = [date.fromisoformat(d) for d in (expiries_by_symbol.get(symbol) or [])]
    if not expiries:
        st.info(f"No open option data for {symbol} yet.")
        continue

    terms = fo_service.classify_index_expiry_terms(expiries, date.today())
    rows = []
    spot = None
    for term_key, pct in term_pct.items():
        exp = terms.get(term_key)
        if exp is None:
            continue
        chain_rows = load_option_chain(client, symbol, exp.isoformat(), st.session_state["portfolio_cache_bust"])
        strangle = fo_service.index_strangle_for_expiry(chain_rows, pct)
        if strangle is None:
            rows.append(
                {
                    "Term": _TERM_LABELS[term_key],
                    "Expiry": exp.strftime("%d %b %Y"),
                    "Sell PE Strike": "N/A",
                    "PE Premium": "N/A",
                    "Sell CE Strike": "N/A",
                    "CE Premium": "N/A",
                    "Credit (per unit)": "N/A",
                    "Credit (1 lot)": "N/A",
                }
            )
            continue
        spot = strangle["spot"]
        rows.append(
            {
                "Term": _TERM_LABELS[term_key],
                "Expiry": exp.strftime("%d %b %Y"),
                "Sell PE Strike": format_inr(strangle["pe_strike"], decimals=0),
                "PE Premium": format_inr(strangle["pe_premium"]) if strangle["pe_premium"] is not None else "N/A",
                "Sell CE Strike": format_inr(strangle["ce_strike"], decimals=0),
                "CE Premium": format_inr(strangle["ce_premium"]) if strangle["ce_premium"] is not None else "N/A",
                "Credit (per unit)": format_inr(strangle["credit_per_unit"])
                if strangle["credit_per_unit"] is not None
                else "N/A",
                "Credit (1 lot)": format_inr(strangle["credit_total"])
                if strangle["credit_total"] is not None
                else "N/A",
            }
        )

    if not rows:
        st.info(f"Not enough option data to compute a strangle for {symbol} yet.")
        continue
    st.caption(f"Current Price: {format_inr(spot)}" if spot is not None else "Current Price: N/A")
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True, key=f"index_strangle_{symbol}")
