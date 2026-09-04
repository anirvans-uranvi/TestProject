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
# sidebar (Streamlit's only native notion of a "sub-page"). Restructured
# per an explicit user request into a guided "Wheel Strategy" journey --
# screen for a CSP candidate -> track running CSPs -> see stocks that
# have been assigned into holdings with option overlays -> see plain
# holdings with a covered-call trigger -- with "Index Options" (strangle
# ideas on the 4 major indices) as its own
# section alongside it. "Market" (raw Equity/Options lookup, kept as its
# own section per the user's explicit choice) and "My Portfolio"
# (Holdings/Positions/All Trades) and "Trade History" round out the
# rest. Settings gets its own single-page section since the dict form
# requires every page to belong to one. Section headers and
# st.Page(title=...) (the sidebar's own page labels) are independent of
# each page's underlying filename/st.set_page_config() browser-tab
# title -- see each page's own docstring/set_page_config call for those.
pages = {
    "Wheel Strategy": [
        st.Page("pages/1_Dashboard.py", title="Screener for CSP", default=True),
        st.Page("pages/11_My_CSP.py", title="My Current CSPs"),
        st.Page("pages/12_My_Portfolio_Trades.py", title="My Portfolio Trades"),
        st.Page("pages/15_Other_Stock_Holdings.py", title="Other Stock Holdings"),
        st.Page("pages/10_Analyse_Trade.py", title="Analyse Trade", visibility="hidden"),
    ],
    "Index Options": [
        st.Page("pages/16_Index_Options.py", title="Index Options"),
    ],
    "Market": [
        st.Page("pages/2_Stock_Detail.py", title="Equity"),
        st.Page("pages/5_Options.py", title="Options"),
    ],
    "My Portfolio": [
        st.Page("pages/8_My_Holdings.py", title="Holdings"),
        st.Page("pages/9_My_Positions.py", title="Positions"),
        st.Page("pages/7_My_Trades.py", title="All Trades"),
    ],
    "Trade History": [
        st.Page("pages/14_Trade_History.py", title="Trade History"),
    ],
    "Settings": [
        # url_path pinned explicitly (not left to Streamlit's
        # filename-derived default) so this page's URL stays stable even
        # if this file is ever renamed -- originally needed as a stable
        # Kite Connect "Redirect URL" for Zerodha's OAuth login, which no
        # longer exists (Zerodha was removed entirely), but there's no
        # reason to unpin it now either.
        st.Page("pages/4_Settings.py", title="Settings", url_path="Settings"),
    ],
}
st.navigation(pages).run()
