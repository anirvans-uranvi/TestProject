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
    st.Page("pages/6_Portfolio.py", title="My Portfolio"),
    st.Page("pages/4_Settings.py", title="Settings"),
]
st.navigation(pages).run()
