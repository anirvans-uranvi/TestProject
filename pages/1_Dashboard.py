from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st
from postgrest.exceptions import APIError

from src.calculations.classification import criterion_fundamentals
from src.config import get_settings
from src.models.user import SavedFilter
from src.repositories import fetch_log_repo, fo_repo, settings_repo, snapshot_repo
from src.services.market_calendar import get_market_state
from src.services.threshold_override import apply_user_thresholds
from src.utils.formatting import direction_arrow, format_inr, format_pct, pass_fail_icon
from src.utils.refresh_bar import render_stock_refresh_button
from src.utils.session import current_user_id, get_user_client_cached, require_login
from src.utils.timezones import now_ist
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
def _load_latest_fo_trade_date(_client, _cache_bust: int, source_prefix: str | None = None):
    return fo_repo.get_latest_fo_trade_date(_client, source_prefix=source_prefix)


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
    f"Data sources — Stock prices: `{user_settings.data_provider}` (Settings > Data Provider) · "
    f"Fundamentals (PE/PEG/dividends): `{app_settings.fundamentals_provider}` · "
    "Options/F&O: NSE + BSE Bhavcopy (end-of-day) — always, regardless of Data Provider"
)

header_col1, header_col2 = st.columns(2)
last_fetch = _load_last_fetch(client, st.session_state["dashboard_cache_bust"])
last_fetch_at = last_fetch.finished_at if last_fetch else None
try:
    latest_nse_fo_trade_date = _load_latest_fo_trade_date(client, st.session_state["dashboard_cache_bust"], "nse_fo_bhavcopy")
    latest_bse_fo_trade_date = _load_latest_fo_trade_date(client, st.session_state["dashboard_cache_bust"], "bse_fo_bhavcopy")
except APIError:
    # F&O tables (migration 0007) not applied yet -- degrade to "--" rather than crashing the Dashboard.
    latest_nse_fo_trade_date = None
    latest_bse_fo_trade_date = None
market_state = get_market_state(
    now=now_ist(),
    last_successful_fetch_at=last_fetch_at,
    stale_threshold_minutes=user_settings.stale_data_threshold_minutes,
)
with header_col1:
    st.markdown(f"**Market state:** {market_state_label(market_state)}")
    st.markdown(f"**Latest NSE Bhavcopy:** {latest_nse_fo_trade_date.strftime('%d %b %Y') if latest_nse_fo_trade_date else '—'}")
    st.markdown(f"**Latest BSE Bhavcopy:** {latest_bse_fo_trade_date.strftime('%d %b %Y') if latest_bse_fo_trade_date else '—'}")
with header_col2:
    if last_fetch_at is None:
        st.markdown("**Data freshness:** ⚪ no successful refresh yet")
    else:
        age_min = (now_ist() - last_fetch_at.astimezone(now_ist().tzinfo)).total_seconds() / 60
        st.markdown(f"**Data freshness:** {age_min:.0f} min ago")

render_disclaimer()
render_stock_refresh_button(client, user_id, user_settings.data_provider)

rows = _load_screener_rows(client, st.session_state["dashboard_cache_bust"])
rows = apply_user_thresholds(rows, user_settings)

if not rows:
    st.info(
        "No screener data yet. Run `python scripts/run_refresh.py --mode=eod` and "
        "`--mode=fundamentals` (or `scripts/seed_mock_data.py` for local dev) to populate data."
    )
    st.stop()

df = pd.DataFrame([r.model_dump() for r in rows])

# Prefer a live broker quote over the shared, possibly-stale
# daily_screener_snapshots value -- only when this account's Data
# Provider setting (Settings page) is Dhan, and only for
# whichever symbols "Market Data Refresh" actually cached a live price
# for (user_live_prices, migration 0030); every other symbol keeps its
# snapshot value. `live_prices` is also consulted below to skip the
# "(as of <date>)" stale-fallback marker for a live-priced row -- its
# LTP is fresh even though the snapshot row backing 52W/returns/PEG for
# that symbol may not be.
live_prices: dict[str, float] = {}
if user_settings.data_provider != "yfinance_bhavcopy":
    live_prices = snapshot_repo.get_user_live_prices(client, user_id, df["symbol"].tolist())
    if live_prices:
        df["latest_price"] = df.apply(lambda r: live_prices.get(r["symbol"], r["latest_price"]), axis=1)

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
#
# dashboard_fo_metrics_rows spans every symbol with F&O data, which
# includes the seeded Index rows (NIFTY, BANKNIFTY, BANKEX, ...) used
# for the BSE index-options feed (bse_fo_provider is index-only, see
# migration 0019 era). Pooling expiries across ALL of those would leak
# an index's own expiry (e.g. BANKEX's BSE Aug expiry) into this
# dropdown even though no Nifty 50 STOCK actually has data for that
# month -- restrict to expiries that belong to a symbol actually shown
# in the screener below.
_screener_symbols = set(df["symbol"])
available_expiries = sorted(
    {
        r["expiry_date"]
        for r in dashboard_fo_metrics_rows
        if r.get("expiry_date") and r.get("symbol") in _screener_symbols
    }
)
if available_expiries:
    if st.session_state.get("dashboard_options_month") not in available_expiries:
        st.session_state["dashboard_options_month"] = available_expiries[0]
    selected_month = st.selectbox(
        "Options month",
        available_expiries,
        key="dashboard_options_month",
        # Full date, not just "Aug 2026" -- these are pooled *exact* dates
        # (available_expiries is a set of real expiry_date values, not
        # month buckets), so two entries landing in the same month would
        # otherwise render as indistinguishable duplicates -- e.g. a data
        # issue surfacing a stray expiry a few days off the real one
        # would be invisible until this showed the day too.
        format_func=lambda d: date.fromisoformat(d).strftime("%b %Y (%d-%b-%y)"),
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

# ---------------------------------------------------------------------
# Live F&O override (Dhan only, migration 0032) -- "Market Data Refresh"
# also live-prices the exact CSP/CC legs cached above for a Dhan-provider
# account (see src/utils/refresh_bar.py's _dhan_fo_universe); recompute
# csp_pct/cc_pct from the live premium using the same formulas
# fo_service.csp_5pct_for_rows/cc_5pct_for_rows already document
# (csp_pct = put_price / strike * 100, cc_pct = premium / spot * 100) --
# only the premium itself is live, the cached strike/spot are unchanged.
# ---------------------------------------------------------------------
live_fo_premiums: dict = {}
if user_settings.data_provider == "dhan" and metrics_by_symbol:
    fo_contracts = []
    for m in metrics_by_symbol.values():
        expiry = date.fromisoformat(m["expiry_date"])
        if m.get("csp_strike") is not None:
            fo_contracts.append((m["symbol"], expiry, float(m["csp_strike"]), "PE"))
        if m.get("cc_strike") is not None:
            fo_contracts.append((m["symbol"], expiry, float(m["cc_strike"]), "CE"))
    live_fo_premiums = snapshot_repo.get_user_live_fo_prices(client, user_id, fo_contracts)


def _csp_pct(symbol: str) -> float | None:
    m = metrics_by_symbol.get(symbol)
    if not m or m.get("csp_strike") is None:
        return (m or {}).get("csp_pct")
    key = (symbol, date.fromisoformat(m["expiry_date"]), float(m["csp_strike"]), "PE")
    live_price = live_fo_premiums.get(key)
    if live_price is not None:
        return live_price / m["csp_strike"] * 100
    return m.get("csp_pct")


def _cc_pct(symbol: str) -> float | None:
    m = metrics_by_symbol.get(symbol)
    if not m or m.get("cc_strike") is None:
        return (m or {}).get("cc_pct")
    key = (symbol, date.fromisoformat(m["expiry_date"]), float(m["cc_strike"]), "CE")
    live_price = live_fo_premiums.get(key)
    if live_price is not None and m.get("spot"):
        return live_price / m["spot"] * 100
    return m.get("cc_pct")


filtered["csp_5pct"] = filtered["symbol"].map(_csp_pct)
filtered["cc_5pct"] = filtered["symbol"].map(_cc_pct)

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
        r["symbol"] not in live_prices
        and pd.notna(r["latest_price"])
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
