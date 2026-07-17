import streamlit as st

from src.config import get_settings
from src.utils.session import current_user_email, require_login, sign_out
from src.utils.ui import inject_tailwind, render_disclaimer

st.set_page_config(
    page_title="Nifty 50 Momentum & Dividend Screener",
    page_icon="📈",
    layout="wide",
)

require_login()
inject_tailwind()

with st.sidebar:
    st.caption(f"Signed in as {current_user_email()}")
    if st.button("Sign out", use_container_width=True):
        sign_out()
        st.rerun()

st.title("📈 Nifty 50 Momentum & Dividend Screener")
st.caption(
    "Screens all current Nifty 50 constituents on momentum, dividend yield, and PEG, "
    "and classifies each as Green, Amber, Red, or Unavailable."
)
render_disclaimer()

settings = get_settings()
col1, col2 = st.columns(2)
with col1:
    st.subheader("Get started")
    st.page_link("pages/1_Dashboard.py", label="Open the Dashboard", icon="📊")
    st.page_link("pages/2_Stock_Detail.py", label="Look up a stock", icon="🔍")
    st.page_link("pages/3_Alerts.py", label="Manage alerts", icon="🔔")
    st.page_link("pages/4_Settings.py", label="Configure thresholds", icon="⚙️")
with col2:
    st.subheader("Data sources")
    _provider_notes = {
        "dhan": "Dhan (the configured live price vendor) does not expose PE, PEG, or dividend "
        "data -- see the README for the current fundamentals-coverage limitation.",
        "yfinance": "yfinance is an unofficial Yahoo Finance client covering prices, dividends, "
        "and fundamentals in one provider -- see the README's Limitations for the caveats "
        "that come with that (unofficial API, no uptime/rate-limit guarantee).",
        "manual": "Fundamentals are read from hand-curated CSVs in `data/` -- see the README "
        "for the CSV schema.",
        "mock": "Running on synthetic mock data (deterministic per symbol, not real market "
        "data) -- set `MARKET_DATA_PROVIDER`/`FUNDAMENTALS_PROVIDER` to switch providers.",
    }
    st.markdown(
        f"- **Prices**: `{settings.market_data_provider}` provider\n"
        f"- **Fundamentals (PE/PEG/dividends)**: `{settings.fundamentals_provider}` provider\n\n"
        + _provider_notes.get(settings.market_data_provider, "")
    )
