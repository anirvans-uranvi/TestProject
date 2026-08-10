from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st
from postgrest.exceptions import APIError

from src.calculations.classification import criterion_fundamentals
from src.config import get_settings
from src.models.enums import CompanyType
from src.models.user import SavedFilter
from src.repositories import companies_repo, fetch_log_repo, fo_repo, settings_repo, snapshot_repo
from src.services import edge_refresh
from src.services.market_calendar import get_market_state
from src.services.threshold_override import apply_user_thresholds
from src.utils.formatting import direction_arrow, format_inr, format_pct, pass_fail_icon
from src.utils.session import current_user_id, get_user_client_cached, require_login
from src.utils.timezones import format_ist, now_ist
from src.utils.ui import inject_global_styles, market_state_label, render_disclaimer, render_pill

st.set_page_config(page_title="Dashboard | Nifty 50 Screener", page_icon="📊", layout="wide")
require_login()  # already injects Tailwind + the light-theme CSS design system

client = get_user_client_cached()
user_id = current_user_id()
user_settings = settings_repo.get_user_settings(client, user_id)
inject_global_styles(user_settings.theme)  # re-inject with the user's actual theme -- a later <style> tag wins


@st.cache_data(ttl=60, show_spinner=False)
def _load_screener_rows(_client, _cache_bust: int):
    return snapshot_repo.get_latest_screener(_client)


@st.cache_data(ttl=60, show_spinner=False)
def _load_last_fetch(_client, _cache_bust: int):
    # "all" is the on-demand manual-refresh Edge Function's combined log
    # entry -- included so the header reflects it, not just the cron
    # path's per-mode "intraday_price" entries.
    return fetch_log_repo.get_last_successful_fetch(_client, ["intraday_price", "all"])


@st.cache_data(ttl=60, show_spinner=False)
def _load_last_fo_fetch(_client, _cache_bust: int):
    return fetch_log_repo.get_last_successful_fetch(_client, "fo")


@st.cache_data(ttl=60, show_spinner=False)
def _load_latest_fo_trade_date(_client, _cache_bust: int):
    return fo_repo.get_latest_fo_trade_date(_client)


@st.cache_data(ttl=60, show_spinner=False)
def _load_universe_counts(_client, _cache_bust: int) -> tuple[int, int]:
    """(stock_count, etf_count) across every symbol this app currently
    tracks -- Nifty 50 constituents plus any portfolio-only symbol the
    refresh pipeline has registered (see companies_repo.list_all_companies).
    The refresh summary's own succeeded/total counts include ETFs/funds,
    but the screener list below excludes them (migration 0018) -- so
    "refreshed all N" and "N of N stocks" in the screener look mismatched
    without this breakdown. Counted explicitly by company_type rather than
    "everything that isn't a stock", since `companies` also holds Index
    rows (NIFTY/BANKNIFTY/SENSEX, migration 0018) that were never part of
    the price-refresh pipeline's own total and would otherwise inflate
    stock_count."""
    companies = companies_repo.list_all_companies(_client)
    stock_count = sum(1 for c in companies if c.company_type == CompanyType.EQUITY)
    etf_count = sum(1 for c in companies if c.company_type == CompanyType.ETF)
    return stock_count, etf_count


@st.cache_data(ttl=60, show_spinner=False)
def _load_dashboard_fo_metrics(_client, _cache_bust: int):
    """The precomputed 5% CSP / 5% CC cache (`dashboard_fo_metrics`,
    migration 0011) -- up to 3 rows per symbol (near/next/far expiry),
    kept current by every refresh path (see
    `fo_service.recompute_dashboard_metrics` and its TypeScript port in
    `supabase/functions/_shared/dashboardMetrics.ts`) instead of pulling
    every open option leg (thousands of rows) and recomputing the
    nearest-strike search here on every page load. Raw rows, not grouped
    by symbol -- the page derives the "Options month" dropdown's choices
    from the distinct expiry_dates present, then filters to whichever
    month is selected."""
    return fo_repo.get_dashboard_fo_metrics(_client)


if "dashboard_cache_bust" not in st.session_state:
    st.session_state["dashboard_cache_bust"] = 0

app_settings = get_settings()

st.title("📈 Nifty 50 Momentum & Dividend Screener")
st.caption(
    "Screens all current Nifty 50 constituents on Momentum and Fundamentals (dividend yield "
    "or PEG clearing their thresholds), and classifies each as Green, Amber, Red, or Unavailable."
)
st.caption(
    f"Data sources — Stock prices: `{app_settings.market_data_provider}` · "
    f"Fundamentals (PE/PEG/dividends): `{app_settings.fundamentals_provider}` · "
    "Options/F&O: NSE Bhavcopy (end-of-day)"
)

header_col1, header_col2, header_col3, header_col4 = st.columns([2, 1, 1, 1])
last_fetch = _load_last_fetch(client, st.session_state["dashboard_cache_bust"])
last_fetch_at = last_fetch.finished_at if last_fetch else None
last_fo_fetch = _load_last_fo_fetch(client, st.session_state["dashboard_cache_bust"])
last_fo_fetch_at = last_fo_fetch.finished_at if last_fo_fetch else None
try:
    latest_fo_trade_date = _load_latest_fo_trade_date(client, st.session_state["dashboard_cache_bust"])
except APIError:
    # F&O tables (migration 0007) not applied yet -- degrade to "--" rather than crashing the Dashboard.
    latest_fo_trade_date = None
market_state = get_market_state(
    now=now_ist(),
    last_successful_fetch_at=last_fetch_at,
    stale_threshold_minutes=user_settings.stale_data_threshold_minutes,
)
with header_col1:
    st.markdown(f"**Last stock refresh:** {format_ist(last_fetch_at)}")
    st.markdown(f"**Last F&O refresh:** {format_ist(last_fo_fetch_at)}")
    st.markdown(f"**Market state:** {market_state_label(market_state)}")
with header_col2:
    if last_fetch_at is None:
        st.markdown("**Data freshness:** ⚪ no successful refresh yet")
    else:
        age_min = (now_ist() - last_fetch_at.astimezone(now_ist().tzinfo)).total_seconds() / 60
        st.markdown(f"**Data freshness:** {age_min:.0f} min ago")
    st.markdown(f"**Latest Bhavcopy:** {latest_fo_trade_date.strftime('%d %b %Y') if latest_fo_trade_date else '—'}")
with header_col3:
    if st.button("🔄 Stock Data Refresh", use_container_width=True):
        with st.spinner("Refreshing live data from Yahoo Finance -- this can take up to a minute..."):
            try:
                summary = edge_refresh.trigger_manual_refresh(st.session_state["sb_access_token"])
            except edge_refresh.ManualRefreshError as exc:
                st.session_state["last_manual_refresh_summary"] = {"error": str(exc)}
            else:
                st.session_state["last_manual_refresh_summary"] = summary
        st.session_state["dashboard_cache_bust"] += 1
        st.cache_data.clear()
        st.rerun()
with header_col4:
    if st.button("📊 F&O Data Refresh", use_container_width=True):
        with st.spinner("Checking NSE for a newer F&O bhavcopy -- this can take up to a few minutes..."):
            try:
                fo_summary = edge_refresh.trigger_fo_refresh(st.session_state["sb_access_token"])
            except edge_refresh.ManualRefreshError as exc:
                st.session_state["last_fo_refresh_summary"] = {"error": str(exc)}
            else:
                st.session_state["last_fo_refresh_summary"] = fo_summary
        st.session_state["dashboard_cache_bust"] += 1
        st.cache_data.clear()
        st.rerun()

# Shown once, right after the rerun triggered by the buttons above (a
# message set and then immediately st.rerun()-ed away would never
# actually render, so this is stashed in session_state and displayed on
# the next script run instead).
if st.session_state.get("last_manual_refresh_summary"):
    summary = st.session_state.pop("last_manual_refresh_summary")
    if summary.get("error"):
        st.error(summary["error"])
    else:
        stock_count, etf_count = _load_universe_counts(client, st.session_state["dashboard_cache_bust"])
        breakdown = f" ({stock_count} stocks, {etf_count} ETFs/funds)" if etf_count else ""
        if summary["failed"] == 0:
            st.success(f"✅ Refreshed all {summary['succeeded']} symbols{breakdown}.")
        else:
            failed_symbols = ", ".join(f["symbol"] for f in summary["symbolsFailed"])
            st.warning(
                f"Refreshed {summary['succeeded']} of {summary['total']} symbols{breakdown} -- "
                f"{summary['failed']} failed: {failed_symbols}"
            )

if st.session_state.get("last_fo_refresh_summary"):
    fo_summary = st.session_state.pop("last_fo_refresh_summary")
    if fo_summary.get("error"):
        st.error(fo_summary["error"])
    elif fo_summary.get("updated"):
        st.success(
            f"✅ Loaded F&O bhavcopy for {fo_summary['tradeDate']}: "
            f"{fo_summary['futuresRows']} futures + {fo_summary['optionRows']} option rows."
        )
    else:
        st.info(fo_summary.get("message", "F&O data is already up to date."))

render_disclaimer()

rows = _load_screener_rows(client, st.session_state["dashboard_cache_bust"])
rows = apply_user_thresholds(rows, user_settings)

if not rows:
    st.info(
        "No screener data yet. Run `python scripts/run_refresh.py --mode=eod` and "
        "`--mode=fundamentals` (or `scripts/seed_mock_data.py` for local dev) to populate data."
    )
    st.stop()

df = pd.DataFrame([r.model_dump() for r in rows])

# ---------------------------------------------------------------------
# F&O-derived columns (5% CSP, 5% CC) -- read from the precomputed
# dashboard_fo_metrics cache (migration 0011) rather than recomputed
# here; see _load_dashboard_fo_metrics's docstring. Up to 3 rows per
# symbol (near/next/far expiry) come back here; the "Options month"
# selectbox further below (alongside "Sort By") picks which expiry's
# rows actually populate the two columns -- a pure re-render over
# already-cached data, no new fetch. Degrades to "N/A" in both columns,
# not a crash, if migration 0007/0011 hasn't been applied yet -- same
# APIError-catching pattern as pages/5_Options.py.
# ---------------------------------------------------------------------
try:
    dashboard_fo_metrics_rows = _load_dashboard_fo_metrics(client, st.session_state["dashboard_cache_bust"])
except APIError:
    dashboard_fo_metrics_rows = []

# ---------------------------------------------------------------------
# Metric cards (also usable as quick filters via session_state)
# ---------------------------------------------------------------------
ALL_STATUSES = ["Green", "Amber", "Red", "Unavailable"]

if "status_filter" not in st.session_state:
    st.session_state["status_filter"] = list(ALL_STATUSES)
if "criterion_filter" not in st.session_state:
    st.session_state["criterion_filter"] = None

counts = {
    "Total": len(df),
    "Green": int((df["status"] == "green").sum()),
    "Amber": int((df["status"] == "amber").sum()),
    "Red": int((df["status"] == "red").sum()),
}
extra_counts = {
    "Yield > threshold": int((df["criterion_a"] == True).sum()),  # noqa: E712
    "All momentum +ve": int((df["criterion_b"] == True).sum()),  # noqa: E712
    "PEG <= threshold": int((df["criterion_c"] == True).sum()),
}

metric_cols = st.columns(7)
metric_specs = [
    ("Total stocks", counts["Total"], None, None),
    ("🟢 Green", counts["Green"], "green", None),
    ("🟠 Amber", counts["Amber"], "amber", None),
    ("🔴 Red", counts["Red"], "red", None),
    ("Yield > threshold", extra_counts["Yield > threshold"], None, "criterion_a"),
    ("All momentum +ve", extra_counts["All momentum +ve"], None, "criterion_b"),
    ("PEG <= threshold", extra_counts["PEG <= threshold"], None, "criterion_c"),
]
for col, (label, value, status_value, criterion_key) in zip(metric_cols, metric_specs):
    with col:
        if st.button(f"{label}\n{value}", key=f"metric_{label}", use_container_width=True):
            if criterion_key:
                st.session_state["criterion_filter"] = criterion_key
                st.session_state["status_filter"] = list(ALL_STATUSES)
            else:
                st.session_state["status_filter"] = [status_value.capitalize()] if status_value else list(ALL_STATUSES)
                st.session_state["criterion_filter"] = None
            st.rerun()

st.divider()

# ---------------------------------------------------------------------
# Sidebar filters
# ---------------------------------------------------------------------
with st.sidebar:
    st.subheader("Filters")
    status_filter = st.multiselect(
        "Status", ALL_STATUSES, default=st.session_state["status_filter"],
        help="Pick any combination -- e.g. Green + Red only. Leave all selected (or click 'Total stocks' above) to show everything.",
    )
    st.session_state["status_filter"] = status_filter

    sectors = sorted([s for s in df["sector"].dropna().unique()])
    sector_filter = st.multiselect("Sector", sectors)

    search = st.text_input("Search company or symbol")

    min_yield = st.number_input(
        "Minimum dividend yield (%)", value=0.0, step=0.5,
        help=f"Independent of your Settings threshold ({user_settings.dividend_yield_threshold}% for criterion A) -- "
             "defaults to 0 so nothing is excluded until you raise it.",
    )
    min_peg = st.number_input(
        "Minimum PEG", value=0.0, step=0.1,
        help=f"Independent of your Settings threshold ({user_settings.peg_threshold} for criterion C) -- "
             "defaults to 0 so nothing is excluded until you raise it.",
    )

    st.caption("Momentum filters")
    mom_1d = st.selectbox("1D", ["Any", "Positive", "Negative"], key="mom1d")
    mom_5d = st.selectbox("5D", ["Any", "Positive", "Negative"], key="mom5d")
    mom_20d = st.selectbox("20D", ["Any", "Positive", "Negative"], key="mom20d")

    complete_only = st.checkbox("Complete data only (hide Unavailable)")

    st.divider()
    st.subheader("Saved filter presets")
    saved_filters = settings_repo.list_saved_filters(client, user_id)
    preset_names = [f.name for f in saved_filters]
    chosen_preset = st.selectbox("Load preset", ["—"] + preset_names)
    if chosen_preset != "—":
        preset = next(f for f in saved_filters if f.name == chosen_preset)
        fj = preset.filter_json
        loaded_status = fj.get("status", status_filter)
        if isinstance(loaded_status, str):  # backward-compat with presets saved before multi-select
            status_filter = list(ALL_STATUSES) if loaded_status == "All" else [loaded_status]
        else:
            status_filter = loaded_status
        sector_filter = fj.get("sector", sector_filter)
        search = fj.get("search", search)
        min_yield = fj.get("min_yield", min_yield)
        min_peg = fj.get("min_peg", min_peg)
        complete_only = fj.get("complete_only", complete_only)

    new_preset_name = st.text_input("Save current filters as")
    if st.button("💾 Save preset") and new_preset_name:
        settings_repo.upsert_saved_filter(
            client,
            SavedFilter(
                user_id=user_id,
                name=new_preset_name,
                filter_json={
                    "status": status_filter,
                    "sector": sector_filter,
                    "search": search,
                    "min_yield": min_yield,
                    "min_peg": min_peg,
                    "complete_only": complete_only,
                },
            ),
        )
        st.success(f"Saved preset '{new_preset_name}'")

# ---------------------------------------------------------------------
# Apply filters
# ---------------------------------------------------------------------
filtered = df.copy()
filtered = filtered[filtered["status"].isin([s.lower() for s in status_filter])]
if st.session_state["criterion_filter"]:
    filtered = filtered[filtered[st.session_state["criterion_filter"]] == True]  # noqa: E712
if sector_filter:
    filtered = filtered[filtered["sector"].isin(sector_filter)]
if search:
    needle = search.strip().lower()
    filtered = filtered[
        filtered["symbol"].str.lower().str.contains(needle) | filtered["name"].str.lower().str.contains(needle)
    ]
if min_yield:
    filtered = filtered[filtered["ttm_dividend_yield"].fillna(-1e9) >= min_yield]
if min_peg:
    filtered = filtered[filtered["peg_ratio"].fillna(-1e9) >= min_peg]


def _momentum_mask(series: pd.Series, choice: str) -> pd.Series:
    if choice == "Positive":
        return series > 0
    if choice == "Negative":
        return series < 0
    return pd.Series(True, index=series.index)


filtered = filtered[_momentum_mask(filtered["return_1d"], mom_1d)]
filtered = filtered[_momentum_mask(filtered["return_5d"], mom_5d)]
filtered = filtered[_momentum_mask(filtered["return_20d"], mom_20d)]

if complete_only:
    filtered = filtered[filtered["status"] != "unavailable"]

# ---------------------------------------------------------------------
# Screener table
# ---------------------------------------------------------------------
_CRITERION_FILTER_LABEL = {
    "criterion_a": "Yield > threshold",
    "criterion_b": "All momentum +ve",
    "criterion_c": "PEG <= threshold",
}
_active_criterion_label = _CRITERION_FILTER_LABEL.get(st.session_state["criterion_filter"])
_status_filter_active = sorted(st.session_state["status_filter"]) != sorted(ALL_STATUSES)
_filter_active = bool(_active_criterion_label) or _status_filter_active

subheader_col, clear_col = st.columns([5, 1])
with subheader_col:
    _subheader_html = f'<span class="text-xl font-semibold">Screener ({len(filtered)} of {len(df)} stocks)</span>'
    if _active_criterion_label:
        _subheader_html += " " + render_pill(f"filtered to: {_active_criterion_label}", theme=user_settings.theme)
    st.markdown(_subheader_html, unsafe_allow_html=True)
with clear_col:
    if _filter_active and st.button("✕ Clear filter", use_container_width=True):
        st.session_state["status_filter"] = list(ALL_STATUSES)
        st.session_state["criterion_filter"] = None
        st.rerun()

# Expiry dates come back from Supabase as plain "YYYY-MM-DD" strings
# (get_dashboard_fo_metrics returns raw dict rows, not a parsed model) --
# sorting/equality-comparing them as strings works fine (ISO format sorts
# chronologically), and format_func below only parses one for display.
available_expiries = sorted({r["expiry_date"] for r in dashboard_fo_metrics_rows if r.get("expiry_date")})
if available_expiries:
    if st.session_state.get("dashboard_options_month") not in available_expiries:
        st.session_state["dashboard_options_month"] = available_expiries[0]
    selected_month = st.selectbox(
        "Options month",
        available_expiries,
        key="dashboard_options_month",
        format_func=lambda d: date.fromisoformat(d).strftime("%b %Y"),
        help="Which monthly options expiry feeds the 5% CSP / 5% CC columns below.",
    )
else:
    selected_month = None
    st.selectbox("Options month", ["N/A"], disabled=True)

metrics_by_symbol = (
    {r["symbol"]: r for r in dashboard_fo_metrics_rows if r["expiry_date"] == selected_month}
    if selected_month is not None
    else {}
)
filtered["csp_5pct"] = filtered["symbol"].map(lambda s: (metrics_by_symbol.get(s) or {}).get("csp_pct"))
filtered["cc_5pct"] = filtered["symbol"].map(lambda s: (metrics_by_symbol.get(s) or {}).get("cc_pct"))

filtered = filtered.sort_values("symbol", ascending=True, na_position="last")

# latest_screener_view (migration 0013) falls back to the most recent
# snapshot that actually has a price when today's fetch failed for a
# symbol, rather than showing a blank -- but that means some rows may be
# showing an older price than others. df["snapshot_date"].max() is the
# best date *any* symbol in this batch actually got refreshed to, so any
# row whose own snapshot_date falls short of it was a fallback -- flag
# those with a small "as of <date>" caption under the LTP.
_known_snapshot_dates = df["snapshot_date"].dropna()
_latest_snapshot_date = _known_snapshot_dates.max() if not _known_snapshot_dates.empty else None

display_rows = []
for _, r in filtered.iterrows():
    ltp_cell = format_inr(r["latest_price"])
    if (
        pd.notna(r["latest_price"])
        and pd.notna(r["snapshot_date"])
        and _latest_snapshot_date is not None
        and r["snapshot_date"] != _latest_snapshot_date
    ):
        as_of = pd.Timestamp(r["snapshot_date"]).strftime("%d %b %Y")
        ltp_cell += f" (as of {as_of})"
    display_rows.append(
        {
            "Stock": r["symbol"],
            "LTP": ltp_cell,
            "52W High": f"{format_inr(r['week_52_high'])} {pass_fail_icon(r['criterion_52w_high'])}" if pd.notna(r["week_52_high"]) else "N/A",
            "52W Low": f"{format_inr(r['week_52_low'])} {pass_fail_icon(r['criterion_52w_low'])}" if pd.notna(r["week_52_low"]) else "N/A",
            "1D": f"{direction_arrow(r['return_1d'])} {format_pct(r['return_1d'])}",
            "5D": f"{direction_arrow(r['return_5d'])} {format_pct(r['return_5d'])}",
            "20D": f"{direction_arrow(r['return_20d'])} {format_pct(r['return_20d'])}",
            "Momentum": pass_fail_icon(r["criterion_b"]),
            "5% CSP": format_pct(r["csp_5pct"], signed=False) if pd.notna(r["csp_5pct"]) else "N/A",
            "5% CC": format_pct(r["cc_5pct"], signed=False) if pd.notna(r["cc_5pct"]) else "N/A",
            "Dividend": f"{format_pct(r['ttm_dividend_yield'], signed=False)} {pass_fail_icon(r['criterion_a'])}",
            "PEG": f"{r['peg_ratio']:.2f} {pass_fail_icon(r['criterion_c'])}" if pd.notna(r["peg_ratio"]) else "N/A",
            "Fundamentals": pass_fail_icon(criterion_fundamentals(r["criterion_a"], r["criterion_c"])),
        }
    )

table_df = pd.DataFrame(display_rows)
if table_df.empty:
    st.info("No stocks match your current filters. Try loosening the sidebar filters (e.g. minimum dividend yield/PEG) or confirm screener data has been seeded/refreshed.")
else:
    # A plain st.dataframe (same as the Futures table on the Options page
    # and the Portfolio page's holdings table) instead of the hand-rendered
    # render_screener_table -- its column headers are natively
    # clickable/sortable in the browser, which the HTML table can't do
    # without a JS bridge back to Python (a real <a href> sort link would
    # force a browser navigation, and this app keeps the Supabase session
    # only in st.session_state, so that would log the user out).
    #
    # That native client-side sort is exactly why the old per-row "open
    # detail" button column (one st.button beside each table row) had to
    # go: those buttons are positioned by their *pre-sort* Python index, so
    # clicking a header to reorder the table in the browser would leave
    # them pointing at the wrong row. Row selection (on_select="rerun")
    # sidesteps this -- Streamlit maps a click back to the correct row in
    # the original data regardless of how the table is currently sorted --
    # so a pair of buttons below the table replaces the whole column.
    event = st.dataframe(
        table_df,
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key="dashboard_table",
    )
    selected_rows = event.selection.rows if event and event.selection else []
    if selected_rows:
        selected_symbol = display_rows[selected_rows[0]]["Stock"]
        row_detail_col, row_options_col = st.columns(2)
        with row_detail_col:
            if st.button(f"Open {selected_symbol} in Stock Detail", key="dashboard_open_detail"):
                st.session_state["selected_symbol"] = selected_symbol
                st.switch_page("pages/2_Stock_Detail.py")
        with row_options_col:
            if st.button(f"Open {selected_symbol} in Options", key="dashboard_open_options"):
                st.session_state["fo_symbol"] = selected_symbol
                st.switch_page("pages/5_Options.py")

st.divider()
st.download_button(
    "⬇️ Download filtered results (CSV)",
    data=filtered.drop(columns=["data_quality"], errors="ignore").to_csv(index=False).encode("utf-8"),
    file_name="nifty50_screener.csv",
    mime="text/csv",
)
