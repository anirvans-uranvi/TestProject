from __future__ import annotations

import streamlit as st
from postgrest.exceptions import APIError

from src.repositories import companies_repo, portfolio_repo, settings_repo, snapshot_repo
from src.services import portfolio_service
from src.utils.formatting import format_inr, format_pct
from src.utils.session import current_user_id, get_user_client_cached, require_login
from src.utils.ui import inject_global_styles, render_disclaimer, render_pill, render_screener_table, render_stat_grid

st.set_page_config(page_title="Portfolio | Nifty 50 Screener", page_icon="\U0001f4bc", layout="wide")
require_login()  # already injects Tailwind + the light-theme CSS design system

client = get_user_client_cached()
user_id = current_user_id()
user_settings = settings_repo.get_user_settings(client, user_id)
inject_global_styles(user_settings.theme)  # re-inject with the user's actual theme

st.title("\U0001f4bc Portfolio")
render_disclaimer()

BROKERS = ["Zerodha", "Dhan"]


@st.cache_data(ttl=60, show_spinner=False)
def _load_holdings(_client, _user_id: str, _cache_bust: int):
    return portfolio_repo.list_holdings(_client, _user_id)


@st.cache_data(ttl=300, show_spinner=False)
def _load_all_companies(_client, _cache_bust: int):
    return companies_repo.list_all_companies(_client)


@st.cache_data(ttl=60, show_spinner=False)
def _load_latest_prices(_client, symbols: tuple[str, ...], _cache_bust: int):
    return snapshot_repo.get_latest_prices(_client, list(symbols))


if "portfolio_cache_bust" not in st.session_state:
    st.session_state["portfolio_cache_bust"] = 0

try:
    saved_holdings = _load_holdings(client, user_id, st.session_state["portfolio_cache_bust"])
except APIError:
    # migration 0012 not applied yet
    st.info(
        "Portfolio isn't set up yet. Apply migration "
        "`supabase/migrations/0012_portfolio_holdings.sql` in the Supabase SQL editor, then reload this page."
    )
    st.stop()


def _fmt_qty(value: float) -> str:
    if value == int(value):
        return f"{int(value):,}"
    return f"{value:,.2f}"


# ---------------------------------------------------------------------
# Holdings table -- merges holdings saved across every broker into one
# row per stock, then values each against the app's own market data.
# ---------------------------------------------------------------------
if saved_holdings:
    raw_rows = [
        {
            "raw_name": h.raw_name,
            "symbol": h.symbol,
            "qty": h.qty,
            "avg_price": h.avg_price,
            "investment": h.investment,
        }
        for h in saved_holdings
    ]
    merged = portfolio_service.merge_holdings(raw_rows)
    symbols = tuple(sorted({r["symbol"] for r in merged if r["symbol"]}))
    ltp_by_symbol = _load_latest_prices(client, symbols, st.session_state["portfolio_cache_bust"])
    rows, totals = portfolio_service.compute_portfolio_view(merged, ltp_by_symbol)
    rows.sort(key=lambda r: r["investment"], reverse=True)

    stats = [
        ("Total Investment", format_inr(totals["total_investment"]), None),
        ("Total Current Value", format_inr(totals["total_cur_val"]), None),
        ("Total P&L", format_inr(totals["total_pnl"]), None),
        ("Total P&L %", format_pct(totals["total_pnl_pct"]), None),
    ]
    st.markdown(render_stat_grid(stats, user_settings.theme, cols=4), unsafe_allow_html=True)
    if totals["unpriced_count"]:
        st.caption(
            f"Totals exclude {totals['unpriced_count']} holding(s) with no market data yet "
            "(shown as N/A below -- they'll be picked up by the next data refresh)."
        )

    table_rows = []
    for i, r in enumerate(rows, start=1):
        stock = r["symbol"] or f'{r["raw_name"]} {render_pill("unmatched", "neutral", user_settings.theme)}'
        table_rows.append(
            {
                "#": i,
                "Stock": stock,
                "Qty": _fmt_qty(r["qty"]),
                "Avg Price": format_inr(r["avg_price"]),
                "LTP": format_inr(r["ltp"]),
                "Investment": format_inr(r["investment"]),
                "Cur Val": format_inr(r["cur_val"]),
                "P&L": format_inr(r["pnl"]),
                "P&L %": format_pct(r["pnl_pct"]),
            }
        )
    st.markdown(render_screener_table(table_rows, user_settings.theme), unsafe_allow_html=True)
else:
    st.info("No holdings saved yet -- upload a broker CSV below to get started.")

# ---------------------------------------------------------------------
# Upload holdings
# ---------------------------------------------------------------------
st.divider()
st.subheader("Upload holdings")
st.caption("Uploading a broker's file replaces that broker's previously saved holdings.")

broker = st.selectbox("Broker", BROKERS, key="portfolio_broker")
uploaded_file = st.file_uploader(f"{broker} holdings CSV", type="csv", key=f"portfolio_upload_{broker}")

if uploaded_file is not None:
    parse_failed = False
    try:
        if broker == "Zerodha":
            parsed = portfolio_service.parse_zerodha_csv(uploaded_file)
        else:
            all_companies = _load_all_companies(client, st.session_state["portfolio_cache_bust"])
            parsed = portfolio_service.parse_dhan_csv(uploaded_file, all_companies)
    except Exception as exc:  # noqa: BLE001 -- arbitrary malformed user-uploaded file
        st.error(f"Could not read this file as a {broker} holdings export: {exc}")
        parsed = []
        parse_failed = True

    if not parsed:
        if not parse_failed:
            st.warning("No holding rows found in this file.")
    else:
        preview_rows = [
            {
                "Instrument": h["raw_name"],
                "Matched symbol": h["symbol"] or "(unmatched)",
                "Qty": _fmt_qty(h["qty"]),
                "Avg Price": format_inr(h["avg_price"]),
                "Investment": format_inr(h["investment"]),
            }
            for h in parsed
        ]
        st.dataframe(preview_rows, use_container_width=True, hide_index=True)

        unresolved = [h for h in parsed if h["symbol"] is None]
        with st.form(f"portfolio_save_form_{broker}"):
            manual_symbols: dict[str, str] = {}
            if unresolved:
                st.info(
                    f"{len(unresolved)} row(s) couldn't be matched to a known NSE symbol. "
                    "Enter one to have it tracked from the next data refresh -- leave blank to keep as N/A."
                )
                for h in unresolved:
                    manual_symbols[h["raw_name"]] = st.text_input(
                        f"NSE symbol for “{h['raw_name']}”",
                        key=f"portfolio_symbol_{broker}_{h['raw_name']}",
                    )
            submitted = st.form_submit_button("Save portfolio")

        if submitted:
            for h in parsed:
                if h["symbol"] is None:
                    manual = manual_symbols.get(h["raw_name"], "").strip().upper()
                    if manual:
                        h["symbol"] = manual
            records = portfolio_service.holdings_to_records(user_id, broker, parsed)
            portfolio_repo.replace_broker_holdings(client, user_id, broker, records)
            st.session_state["portfolio_cache_bust"] += 1
            st.cache_data.clear()
            st.success(f"Saved {len(records)} holding(s) from {broker}.")
            st.rerun()
