from __future__ import annotations

import pandas as pd
import streamlit as st

from src.models.user import SavedFilter
from src.repositories import fetch_log_repo, settings_repo, snapshot_repo
from src.services.market_calendar import get_market_state
from src.services.threshold_override import apply_user_thresholds
from src.utils.formatting import direction_arrow, format_inr, format_pct, pass_fail_badge
from src.utils.session import current_user_id, get_user_client_cached, require_login
from src.utils.timezones import format_ist, now_ist
from src.utils.ui import market_state_label, render_disclaimer, status_badge

st.set_page_config(page_title="Dashboard | Nifty 50 Screener", page_icon="📊", layout="wide")
require_login()

client = get_user_client_cached()
user_id = current_user_id()
user_settings = settings_repo.get_user_settings(client, user_id)


@st.cache_data(ttl=60, show_spinner=False)
def _load_screener_rows(_client, _cache_bust: int):
    return snapshot_repo.get_latest_screener(_client)


@st.cache_data(ttl=60, show_spinner=False)
def _load_last_fetch(_client, _cache_bust: int):
    return fetch_log_repo.get_last_successful_fetch(_client, "intraday_price")


if "dashboard_cache_bust" not in st.session_state:
    st.session_state["dashboard_cache_bust"] = 0

st.title("📈 Nifty 50 Momentum & Dividend Screener")

header_col1, header_col2, header_col3 = st.columns([2, 1, 1])
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
    if st.button("🔄 Manual refresh", use_container_width=True):
        st.session_state["dashboard_cache_bust"] += 1
        st.cache_data.clear()
        st.rerun()

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
# Metric cards (also usable as quick filters via session_state)
# ---------------------------------------------------------------------
if "status_filter" not in st.session_state:
    st.session_state["status_filter"] = "All"

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
    ("Total stocks", counts["Total"], None),
    ("🟢 Green", counts["Green"], "green"),
    ("🟠 Amber", counts["Amber"], "amber"),
    ("🔴 Red", counts["Red"], "red"),
    ("⚪ Unavailable", counts["Unavailable"], "unavailable"),
    ("Yield > threshold", extra_counts["Yield > threshold"], None),
    ("All momentum +ve", extra_counts["All momentum +ve"], None),
    ("PEG <= threshold", extra_counts["PEG <= threshold"], None),
]
for col, (label, value, status_value) in zip(metric_cols, metric_specs):
    with col:
        if st.button(f"{label}\n{value}", key=f"metric_{label}", use_container_width=True):
            st.session_state["status_filter"] = status_value.capitalize() if status_value else "All"
            st.rerun()

st.divider()

# ---------------------------------------------------------------------
# Sidebar filters
# ---------------------------------------------------------------------
with st.sidebar:
    st.subheader("Filters")
    status_options = ["All", "Green", "Amber", "Red", "Unavailable"]
    status_filter = st.selectbox(
        "Status", status_options, index=status_options.index(st.session_state["status_filter"])
    )
    st.session_state["status_filter"] = status_filter

    sectors = sorted([s for s in df["sector"].dropna().unique()])
    sector_filter = st.multiselect("Sector", sectors)

    search = st.text_input("Search company or symbol")

    min_yield = st.number_input(
        "Minimum dividend yield (%)", value=float(user_settings.dividend_yield_threshold), step=0.5
    )
    min_peg = st.number_input("Minimum PEG", value=float(user_settings.peg_threshold), step=0.1)

    st.caption("Momentum filters")
    mom_1d = st.selectbox("1D", ["Any", "Positive", "Negative"], key="mom1d")
    mom_5d = st.selectbox("5D", ["Any", "Positive", "Negative"], key="mom5d")
    mom_20d = st.selectbox("20D", ["Any", "Positive", "Negative"], key="mom20d")

    complete_only = st.checkbox("Complete data only (hide Unavailable)")

    sort_col = st.selectbox(
        "Sort by",
        ["Status", "Symbol", "Latest price", "1D return", "5D return", "20D return", "Dividend yield", "PEG"],
    )
    sort_desc = st.checkbox("Descending", value=True)

    st.divider()
    st.subheader("Saved filter presets")
    saved_filters = settings_repo.list_saved_filters(client, user_id)
    preset_names = [f.name for f in saved_filters]
    chosen_preset = st.selectbox("Load preset", ["—"] + preset_names)
    if chosen_preset != "—":
        preset = next(f for f in saved_filters if f.name == chosen_preset)
        fj = preset.filter_json
        status_filter = fj.get("status", status_filter)
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
if status_filter != "All":
    filtered = filtered[filtered["status"] == status_filter.lower()]
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
    "Status": "status",
    "Symbol": "symbol",
    "Latest price": "latest_price",
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
st.subheader(f"Screener ({len(filtered)} of {len(df)} stocks)")

display_rows = []
for _, r in filtered.iterrows():
    display_rows.append(
        {
            "Status": status_badge(r["status"]),
            "Stock": f"**{r['name']}**  \n{r['symbol']} · {r['sector'] or '—'}",
            "Latest price": format_inr(r["latest_price"]),
            "1D": f"{direction_arrow(r['return_1d'])} {format_pct(r['return_1d'])}",
            "5D": f"{direction_arrow(r['return_5d'])} {format_pct(r['return_5d'])}",
            "20D": f"{direction_arrow(r['return_20d'])} {format_pct(r['return_20d'])}",
            "Dividend yield": f"{format_pct(r['ttm_dividend_yield'], signed=False)} {pass_fail_badge(r['criterion_a'])}",
            "PE": f"{r['pe_ratio']:.1f}" if pd.notna(r["pe_ratio"]) else "N/A",
            "PEG": f"{r['peg_ratio']:.2f} {pass_fail_badge(r['criterion_c'])}" if pd.notna(r["peg_ratio"]) else "N/A",
            "Criteria": (
                f"A:{'✅' if r['criterion_a'] else ('❌' if r['criterion_a'] is False else '—')} "
                f"B:{'✅' if r['criterion_b'] else ('❌' if r['criterion_b'] is False else '—')} "
                f"C:{'✅' if r['criterion_c'] else ('❌' if r['criterion_c'] is False else '—')}"
            ),
            "Symbol": r["symbol"],
        }
    )

table_df = pd.DataFrame(display_rows)
if table_df.empty:
    st.info("No stocks match your current filters. Try loosening the sidebar filters (e.g. minimum dividend yield/PEG) or confirm screener data has been seeded/refreshed.")
else:
    st.markdown(
        table_df.drop(columns=["Symbol"]).to_html(escape=False, index=False),
        unsafe_allow_html=True,
    )

st.divider()
selected_symbol = st.selectbox("Open in Stock Detail →", table_df["Symbol"] if not table_df.empty else [])
if selected_symbol and st.button("View stock detail"):
    st.session_state["selected_symbol"] = selected_symbol
    st.switch_page("pages/2_Stock_Detail.py")

st.download_button(
    "⬇️ Download filtered results (CSV)",
    data=filtered.drop(columns=["data_quality"], errors="ignore").to_csv(index=False).encode("utf-8"),
    file_name="nifty50_screener.csv",
    mime="text/csv",
)
