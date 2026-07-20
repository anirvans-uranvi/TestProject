from __future__ import annotations

import pandas as pd
import streamlit as st
from postgrest.exceptions import APIError

from src.models.enums import OptionType
from src.models.user import SavedFilter
from src.repositories import fetch_log_repo, fo_repo, settings_repo, snapshot_repo
from src.services import edge_refresh, fo_service
from src.services.market_calendar import get_market_state
from src.services.threshold_override import apply_user_thresholds
from src.utils.formatting import direction_arrow, format_inr, format_pct, pass_fail_icon
from src.utils.session import current_user_id, get_user_client_cached, require_login
from src.utils.timezones import format_ist, now_ist
from src.utils.ui import inject_global_styles, market_state_label, render_disclaimer, render_pill, render_screener_table

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
def _load_fo_data(_client, _cache_bust: int):
    """Every open future (all symbols) + every open PE leg (all symbols) in
    two bulk queries, for the near-month future / 5% CSP columns. Cached
    like the screener rows above -- Streamlit reruns this whole script on
    every widget interaction, and each of these queries is thousands of
    rows."""
    return fo_repo.get_all_open_futures(_client), fo_repo.get_all_open_options(_client, OptionType.PE)


if "dashboard_cache_bust" not in st.session_state:
    st.session_state["dashboard_cache_bust"] = 0

st.title("📈 Nifty 50 Momentum & Dividend Screener")

header_col1, header_col2, header_col3, header_col4 = st.columns([2, 1, 1, 1])
last_fetch = _load_last_fetch(client, st.session_state["dashboard_cache_bust"])
last_fetch_at = last_fetch.finished_at if last_fetch else None
market_state = get_market_state(
    now=now_ist(),
    last_successful_fetch_at=last_fetch_at,
    stale_threshold_minutes=user_settings.stale_data_threshold_minutes,
)
with header_col1:
    st.markdown(f"**Last refresh:** {format_ist(last_fetch_at)}")
    st.markdown(f"**Market state:** {market_state_label(market_state)}")
with header_col2:
    if last_fetch_at is None:
        st.markdown("**Data freshness:** ⚪ no successful refresh yet")
    else:
        age_min = (now_ist() - last_fetch_at.astimezone(now_ist().tzinfo)).total_seconds() / 60
        st.markdown(f"**Data freshness:** {age_min:.0f} min ago")
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
        st.rerun()

# Shown once, right after the rerun triggered by the buttons above (a
# message set and then immediately st.rerun()-ed away would never
# actually render, so this is stashed in session_state and displayed on
# the next script run instead).
if st.session_state.get("last_manual_refresh_summary"):
    summary = st.session_state.pop("last_manual_refresh_summary")
    if summary.get("error"):
        st.error(summary["error"])
    elif summary["failed"] == 0:
        st.success(f"✅ Refreshed all {summary['succeeded']} stocks.")
    else:
        failed_symbols = ", ".join(f["symbol"] for f in summary["symbolsFailed"])
        st.warning(
            f"Refreshed {summary['succeeded']} of {summary['total']} stocks -- "
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
# F&O-derived columns (near-month future price, 5% CSP) -- a separate data
# source from latest_screener_view (the F&O tables), joined in by symbol.
# Degrades to "N/A" for both columns, not a crash, if migration 0007 /
# F&O data hasn't been loaded yet -- same APIError-catching pattern as
# pages/5_Options.py.
# ---------------------------------------------------------------------
try:
    futures_rows, put_rows = _load_fo_data(client, st.session_state["dashboard_cache_bust"])
except APIError:
    futures_rows, put_rows = [], []

near_month = fo_service.near_month_futures_map(futures_rows)
future_col_label = fo_service.near_month_column_label(near_month)
spot_by_symbol = dict(zip(df["symbol"], df["latest_price"]))
csp_map = fo_service.csp_5pct_map(put_rows, spot_by_symbol)

df["future_price"] = df["symbol"].map(lambda s: (near_month.get(s) or {}).get("price"))
df["csp_5pct"] = df["symbol"].map(lambda s: (csp_map.get(s) or {}).get("csp_pct"))

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
    "Unavailable": int((df["status"] == "unavailable").sum()),
}
extra_counts = {
    "Yield > threshold": int((df["criterion_a"] == True).sum()),  # noqa: E712
    "All momentum +ve": int((df["criterion_b"] == True).sum()),  # noqa: E712
    "PEG <= threshold": int((df["criterion_c"] == True).sum()),
}

metric_cols = st.columns(8)
metric_specs = [
    ("Total stocks", counts["Total"], None, None),
    ("🟢 Green", counts["Green"], "green", None),
    ("🟠 Amber", counts["Amber"], "amber", None),
    ("🔴 Red", counts["Red"], "red", None),
    ("⚪ Unavailable", counts["Unavailable"], "unavailable", None),
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

    sort_col = st.selectbox(
        "Sort by",
        ["Stock", "Status", "Latest price", future_col_label, "5% CSP", "1D return", "5D return", "20D return", "Dividend yield", "PEG"],
        index=0,
    )
    sort_desc = st.checkbox("Descending", value=False)

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

sort_map = {
    "Stock": "symbol",
    "Status": "status",
    "Latest price": "latest_price",
    "52W High": "week_52_high",
    "52W Low": "week_52_low",
    future_col_label: "future_price",
    "5% CSP": "csp_5pct",
    "1D return": "return_1d",
    "5D return": "return_5d",
    "20D return": "return_20d",
    "Dividend yield": "ttm_dividend_yield",
    "PEG": "peg_ratio",
}
filtered = filtered.sort_values(sort_map[sort_col], ascending=not sort_desc, na_position="last")

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

display_rows = []
for i, (_, r) in enumerate(filtered.iterrows(), start=1):
    display_rows.append(
        {
            "#": i,
            "Stock": r["symbol"],
            "Latest price": format_inr(r["latest_price"]),
            "52W High": f"{format_inr(r['week_52_high'])} {pass_fail_icon(r['criterion_52w_high'])}" if pd.notna(r["week_52_high"]) else "N/A",
            "52W Low": f"{format_inr(r['week_52_low'])} {pass_fail_icon(r['criterion_52w_low'])}" if pd.notna(r["week_52_low"]) else "N/A",
            "1D": f"{direction_arrow(r['return_1d'])} {format_pct(r['return_1d'])}",
            "5D": f"{direction_arrow(r['return_5d'])} {format_pct(r['return_5d'])}",
            "20D": f"{direction_arrow(r['return_20d'])} {format_pct(r['return_20d'])}",
            "Momentum": pass_fail_icon(r["criterion_b"]),
            future_col_label: format_inr(r["future_price"]) if pd.notna(r["future_price"]) else "N/A",
            "5% CSP": format_pct(r["csp_5pct"], signed=False) if pd.notna(r["csp_5pct"]) else "N/A",
            "Dividend yield": f"{format_pct(r['ttm_dividend_yield'], signed=False)} {pass_fail_icon(r['criterion_a'])}",
            "PE": f"{r['pe_ratio']:.1f}" if pd.notna(r["pe_ratio"]) else "N/A",
            "PEG": f"{r['peg_ratio']:.2f} {pass_fail_icon(r['criterion_c'])}" if pd.notna(r["peg_ratio"]) else "N/A",
            "Symbol": r["symbol"],
        }
    )

table_df = pd.DataFrame(display_rows)
if table_df.empty:
    st.info("No stocks match your current filters. Try loosening the sidebar filters (e.g. minimum dividend yield/PEG) or confirm screener data has been seeded/refreshed.")
else:
    st.markdown(render_screener_table(display_rows, user_settings.theme), unsafe_allow_html=True)

st.divider()
open_symbols = table_df["Symbol"] if not table_df.empty else []
detail_col, options_col = st.columns(2)
with detail_col:
    selected_symbol = st.selectbox("Open in Stock Detail →", open_symbols)
    if selected_symbol and st.button("View stock detail"):
        st.session_state["selected_symbol"] = selected_symbol
        st.switch_page("pages/2_Stock_Detail.py")
with options_col:
    fo_symbol = st.selectbox("Open in Options →", open_symbols, key="dashboard_fo_symbol")
    if fo_symbol and st.button("📊 View F&O / options"):
        st.session_state["fo_symbol"] = fo_symbol
        st.switch_page("pages/5_Options.py")

st.download_button(
    "⬇️ Download filtered results (CSV)",
    data=filtered.drop(columns=["data_quality"], errors="ignore").to_csv(index=False).encode("utf-8"),
    file_name="nifty50_screener.csv",
    mime="text/csv",
)
