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
# sidebar (Streamlit's only native notion of a "sub-page") -- "Market"
# nests Equity/Options underneath the screener itself, "My Portfolio"
# nests Holdings/Positions/Trade History together, and "My Trades" nests
# CSP/Portfolio Trades/Other Trades underneath All Trades, the unfiltered list they're each a
# filtered view of. Settings gets its own single-page section since the
# dict form requires every page to belong to one. Section headers and
# st.Page(title=...) (the sidebar's own page labels) are independent of
# each page's underlying filename/st.set_page_config() browser-tab
# title -- see each page's own docstring/set_page_config call for those.
pages = {
    "Market": [
        st.Page("pages/1_Dashboard.py", title="Screener", default=True),
        st.Page("pages/2_Stock_Detail.py", title="Equity"),
        st.Page("pages/5_Options.py", title="Options"),
    ],
    "My Portfolio": [
        st.Page("pages/8_My_Holdings.py", title="Holdings"),
        st.Page("pages/9_My_Positions.py", title="Positions"),
        st.Page("pages/14_Trade_History.py", title="Trade History"),
    ],
    "My Trades": [
        st.Page("pages/7_My_Trades.py", title="All Trades"),
        st.Page("pages/11_My_CSP.py", title="CSP"),
        st.Page("pages/12_My_Portfolio_Trades.py", title="Portfolio Trades"),
        st.Page("pages/13_My_Other_Trades.py", title="Other Trades"),
        st.Page("pages/10_Analyse_Trade.py", title="Analyse Trade", visibility="hidden"),
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
