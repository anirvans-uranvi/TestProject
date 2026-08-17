import streamlit as st

# Pure navigation router -- there is no "app" home screen. Each entry
# below is one of the existing pages/*.py scripts, run in place exactly
# as it always was; st.navigation() just gives us explicit control over
# the sidebar label/order instead of the legacy pages/-directory
# auto-discovery (which derived both from the filename). Titles here are
# what the sidebar shows; each page's own st.set_page_config() still
# separately controls its browser-tab title/icon.
pages = [
    st.Page("pages/1_Dashboard.py", title="Screener", default=True),
    st.Page("pages/2_Stock_Detail.py", title="Equity"),
    st.Page("pages/5_Options.py", title="Options"),
    st.Page("pages/6_My_Broker.py", title="My Broker"),
    st.Page("pages/7_My_Trades.py", title="My Trades"),
    st.Page("pages/8_My_Holdings.py", title="My Holdings"),
    st.Page("pages/9_My_Positions.py", title="My Positions"),
    st.Page("pages/10_Analyse_Trade.py", title="Analyse Trade", visibility="hidden"),
    st.Page("pages/4_Settings.py", title="Settings"),
]
st.navigation(pages).run()
