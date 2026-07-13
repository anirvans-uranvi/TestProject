import streamlit as st

from src.config import get_settings
from src.utils.session import (
    current_user_email,
    handle_recovery_redirect,
    inject_hash_to_query_bridge,
    require_login,
    sign_out,
)
from src.utils.ui import render_disclaimer

st.set_page_config(
    page_title="Nifty 50 Momentum & Dividend Screener",
    page_icon="📈",
    layout="wide",
)

# Must run before require_login(): picks up the session token Supabase
# puts in the URL fragment when a user lands here from a sign-up
# confirmation or password-reset email link.
inject_hash_to_query_bridge()
handle_recovery_redirect()

require_login()

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
    st.markdown(
        f"- **Prices**: `{settings.market_data_provider}` provider\n"
        f"- **Fundamentals (PE/PEG/dividends)**: `{settings.fundamentals_provider}` provider\n\n"
        "Dhan (the configured live price vendor) does not expose PE, PEG, or dividend data -- "
        "see the README for the current fundamentals-coverage limitation."
    )
