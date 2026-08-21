import streamlit as st

# Pure navigation router -- there is no "app" home screen. Each entry
# below is one of the existing pages/*.py scripts, run in place exactly
# as it always was; st.navigation() just gives us explicit control over
# the sidebar label/order instead of the legacy pages/-directory
# auto-discovery (which derived both from the filename). Titles here are
# what the sidebar shows; each page's own st.set_page_config() still
# separately controls its browser-tab title/icon.
#
# The dict form groups pages under a labeled section header in the
# sidebar (Streamlit's only native notion of a "sub-page") -- "Screener"
# nests Equity/Options underneath the screener itself, and "My Trades"
# nests My CSP/My CC/My Other Trades underneath the trades list they're
# each a filtered view of. My Holdings/My Positions/Settings get their
# own single-page section since the dict form requires every page to
# belong to one.
pages = {
    "Screener": [
        st.Page("pages/1_Dashboard.py", title="Screener", default=True),
        st.Page("pages/2_Stock_Detail.py", title="Equity"),
        st.Page("pages/5_Options.py", title="Options"),
    ],
    "My Holdings": [
        st.Page("pages/8_My_Holdings.py", title="My Holdings"),
    ],
    "My Positions": [
        st.Page("pages/9_My_Positions.py", title="My Positions"),
    ],
    "My Trades": [
        st.Page("pages/7_My_Trades.py", title="My Trades"),
        st.Page("pages/11_My_CSP.py", title="My CSP"),
        st.Page("pages/12_My_CC.py", title="My CC"),
        st.Page("pages/13_My_Other_Trades.py", title="My Other Trades"),
        st.Page("pages/10_Analyse_Trade.py", title="Analyse Trade", visibility="hidden"),
    ],
    "Settings": [
        # url_path pinned explicitly (not left to Streamlit's
        # filename-derived default) so the URL to register as this app's
        # Kite Connect "Redirect URL" (Zerodha's "Connect Zerodha
        # account" flow, now Settings' "Data Provider" section) stays
        # stable even if this file is ever renamed.
        st.Page("pages/4_Settings.py", title="Settings", url_path="Settings"),
    ],
}
st.navigation(pages).run()
