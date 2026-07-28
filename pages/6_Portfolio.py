from __future__ import annotations

import re
from datetime import date

import streamlit as st
from postgrest.exceptions import APIError
from pydantic import ValidationError

from src.repositories import companies_repo, fo_repo, portfolio_repo, settings_repo, snapshot_repo
from src.services import fo_service, portfolio_service
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


@st.cache_data(ttl=300, show_spinner=False)
def _load_constituent_symbols(_client, _cache_bust: int) -> set[str]:
    """Current Nifty50 constituents only -- pages/2_Stock_Detail.py's own
    symbol picker is scoped to this same set, so a "view detail" link for
    any other portfolio symbol (an ETF, or a non-Nifty50 stock like
    Hindustan Zinc) would silently land on whatever stock happens to be
    first alphabetically instead of the one clicked. Gating the search
    icon on membership here avoids that footgun."""
    return {c.symbol for c in companies_repo.list_current_constituents(_client)}


@st.cache_data(ttl=60, show_spinner=False)
def _load_latest_prices(_client, symbols: tuple[str, ...], _cache_bust: int):
    return snapshot_repo.get_latest_prices(_client, list(symbols))


@st.cache_data(ttl=60, show_spinner=False)
def _load_option_expiries(_client, symbols: tuple[str, ...], _cache_bust: int) -> dict[str, list[str]]:
    return {symbol: [d.isoformat() for d in fo_repo.list_option_expiries(_client, symbol)] for symbol in symbols}


@st.cache_data(ttl=60, show_spinner=False)
def _load_option_chain(_client, symbol: str, expiry_iso: str, _cache_bust: int) -> list[dict]:
    return fo_repo.get_option_chain(_client, symbol, date.fromisoformat(expiry_iso))


if "portfolio_cache_bust" not in st.session_state:
    st.session_state["portfolio_cache_bust"] = 0

try:
    saved_holdings = _load_holdings(client, user_id, st.session_state["portfolio_cache_bust"])
except (APIError, ValidationError):
    # Either portfolio_holdings doesn't exist yet (migration 0012 -- a
    # postgrest APIError), or it exists but predates portfolio_name
    # (migration 0014 -- every row then fails PortfolioHolding validation
    # with a "field required" error, not an APIError).
    st.info(
        "Portfolio isn't set up yet. Apply migrations "
        "`supabase/migrations/0012_portfolio_holdings.sql` and "
        "`supabase/migrations/0014_portfolio_holdings_multi_portfolio.sql` "
        "(in that order) in the Supabase SQL editor, then reload this page."
    )
    st.stop()


def _fmt_qty(value: float) -> str:
    if value == int(value):
        return f"{int(value):,}"
    return f"{value:,.2f}"


def _slug(text: str) -> str:
    """CSS-class-safe form of an arbitrary portfolio name, for the
    `st-key-*` selectors the search-icon column's CSS scoping relies on."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "x"


def _render_upload_section(
    *, portfolio_name: str, broker: str, key_prefix: str, save_label: str, on_saved=None
) -> None:
    """Parse -> preview -> (manual symbol override for unresolved rows) ->
    save. Always a full sync for this exact (portfolio_name, broker) pair
    -- for a portfolio_name never used before this is simply an insert
    (nothing exists yet to delete), which is how creating a brand-new
    portfolio and updating an existing one end up sharing this one
    function. `on_saved`, if given, runs right before the rerun -- used by
    the "+ New portfolio" tab to clear its name input, since otherwise the
    just-created name lingers in that widget's session_state and the tab
    immediately (and correctly, but confusingly) flags it as already
    existing on the very next render."""
    uploaded_file = st.file_uploader(f"{broker} holdings CSV", type="csv", key=f"portfolio_upload_{key_prefix}")
    if uploaded_file is None:
        return

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
        return

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
    with st.form(f"portfolio_save_form_{key_prefix}"):
        manual_symbols: dict[str, str] = {}
        if unresolved:
            st.info(
                f"{len(unresolved)} row(s) couldn't be matched to a known NSE symbol. "
                "Enter one to have it tracked from the next data refresh -- leave blank to keep as N/A."
            )
            for h in unresolved:
                manual_symbols[h["raw_name"]] = st.text_input(
                    f"NSE symbol for “{h['raw_name']}”",
                    key=f"portfolio_symbol_{key_prefix}_{h['raw_name']}",
                )
        submitted = st.form_submit_button(save_label)

    if submitted:
        for h in parsed:
            if h["symbol"] is None:
                manual = manual_symbols.get(h["raw_name"], "").strip().upper()
                if manual:
                    h["symbol"] = manual
        records = portfolio_service.holdings_to_records(user_id, portfolio_name, broker, parsed)
        portfolio_repo.replace_broker_holdings(client, user_id, portfolio_name, broker, records)
        st.session_state["portfolio_cache_bust"] += 1
        st.cache_data.clear()
        st.success(f"Saved {len(records)} holding(s) from {broker} to \"{portfolio_name}\".")
        if on_saved:
            on_saved()
        st.rerun()


def _load_covered_calls(symbols: tuple[str, ...], expiry_iso: str | None) -> dict[str, dict | None]:
    """Best-effort per-symbol covered-call chain lookup for the selected
    expiry -- returns {} entirely if the F&O tables aren't migrated yet
    (or no expiry is selected), and skips (rather than errors on) any
    individual symbol with no F&O data at all (ETFs, ex-Nifty50 stocks) or
    that doesn't have a contract for this exact expiry date (matched by
    actual date, not position, since NSE monthly expiries occasionally
    drift out of alignment across symbols)."""
    if expiry_iso is None:
        return {}
    try:
        expiries_by_symbol = _load_option_expiries(client, symbols, st.session_state["portfolio_cache_bust"])
    except APIError:
        return {}

    chains: dict[str, dict | None] = {}
    for symbol in symbols:
        if expiry_iso not in (expiries_by_symbol.get(symbol) or []):
            continue
        try:
            chains[symbol] = {
                "rows": _load_option_chain(client, symbol, expiry_iso, st.session_state["portfolio_cache_bust"]),
                "expiry_iso": expiry_iso,
            }
        except APIError:
            continue
    return chains


def _render_portfolio_tab(
    portfolio_name: str, holdings_for_portfolio: list, cc_expiry_iso: str | None, constituent_symbols: set[str]
) -> None:
    """Holdings table (merged across brokers within this one portfolio)
    plus this portfolio's own upload section -- everything a tab shows."""
    raw_rows = [
        {
            "raw_name": h.raw_name,
            "symbol": h.symbol,
            "qty": h.qty,
            "avg_price": h.avg_price,
            "investment": h.investment,
        }
        for h in holdings_for_portfolio
    ]
    merged = portfolio_service.merge_holdings(raw_rows)
    symbols = tuple(sorted({r["symbol"] for r in merged if r["symbol"]}))
    ltp_by_symbol = _load_latest_prices(client, symbols, st.session_state["portfolio_cache_bust"])
    rows, totals = portfolio_service.compute_portfolio_view(merged, ltp_by_symbol)
    rows.sort(key=lambda r: r["investment"], reverse=True)

    cc_chains = _load_covered_calls(symbols, cc_expiry_iso)
    cc_by_symbol: dict[str, dict | None] = {}
    for r in rows:
        symbol = r["symbol"]
        chain = cc_chains.get(symbol) if symbol else None
        cc_by_symbol[symbol] = (
            fo_service.covered_call_for_holding(
                chain["rows"], avg_price=r["avg_price"], ltp=r["ltp"], qty=r["qty"], expiry_date=chain["expiry_iso"]
            )
            if chain
            else None
        )

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
        cc = cc_by_symbol.get(r["symbol"]) if r["symbol"] else None
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
                "CC ROI": format_pct(cc["cc_roi_pct"], signed=False) if cc and cc["cc_roi_pct"] is not None else "N/A",
                "Assignment ROI": format_pct(cc["assignment_roi_pct"]) if cc and cc["assignment_roi_pct"] is not None else "N/A",
            }
        )

    # A slim native-widget column of "open detail" buttons sits beside the
    # table, same pattern (and same reasoning -- render_screener_table is
    # hand-rendered HTML with no way to trigger a same-session page
    # switch) as pages/1_Dashboard.py. Gated to current Nifty50
    # constituents since that's Stock Detail's own symbol universe --
    # see _load_constituent_symbols's docstring.
    table_col, link_col = st.columns([30, 1])
    with table_col:
        st.markdown(render_screener_table(table_rows, user_settings.theme), unsafe_allow_html=True)
    with link_col:
        container_key = f"portfolio-stock-links-{_slug(portfolio_name)}"
        st.markdown(
            f"""
            <style>
            .st-key-{container_key}.stVerticalBlock {{ gap: 0rem !important; }}
            .st-key-{container_key} div[data-testid="stElementContainer"] {{ margin: 0; }}
            .st-key-{container_key} button {{
                height: 2.04rem; min-height: 2.04rem; width: 100%;
                padding: 0; display: flex; align-items: center; justify-content: center;
            }}
            </style>
            """,
            unsafe_allow_html=True,
        )
        with st.container(key=container_key):
            st.markdown("<div style='height:1.8rem'></div>", unsafe_allow_html=True)
            for i, r in enumerate(rows):
                symbol = r["symbol"]
                if symbol and symbol in constituent_symbols:
                    if st.button(
                        "\U0001f50d",
                        key=f"portfolio_open_detail_{portfolio_name}_{i}_{symbol}",
                        help=f"Open {symbol} in Stock Detail",
                    ):
                        st.session_state["selected_symbol"] = symbol
                        st.switch_page("pages/2_Stock_Detail.py")
                else:
                    st.markdown("<div style='height:2.04rem'></div>", unsafe_allow_html=True)

    st.divider()
    st.subheader("Upload holdings")
    st.caption("Uploading a broker's file replaces that broker's previously saved holdings in this portfolio.")
    broker = st.selectbox("Broker", BROKERS, key=f"portfolio_broker_{portfolio_name}")
    _render_upload_section(
        portfolio_name=portfolio_name,
        broker=broker,
        key_prefix=f"{portfolio_name}_{broker}",
        save_label="Save portfolio",
    )

    st.divider()
    with st.expander(f'🗑️ Delete "{portfolio_name}"'):
        st.warning(
            f'This permanently deletes every holding in "{portfolio_name}" (every broker within it). '
            "This cannot be undone."
        )
        confirm = st.checkbox(
            f'I understand -- permanently delete "{portfolio_name}"',
            key=f"portfolio_delete_confirm_{portfolio_name}",
        )
        if st.button(
            "Delete this portfolio",
            key=f"portfolio_delete_btn_{portfolio_name}",
            disabled=not confirm,
            type="primary",
        ):
            portfolio_repo.delete_portfolio(client, user_id, portfolio_name)
            st.session_state["portfolio_cache_bust"] += 1
            st.cache_data.clear()
            # The deleted name no longer matches any tab -- request
            # clearing the tracked active tab (see _pending_active_tab
            # below for why this can't be done directly here) so
            # st.tabs() falls back to its default (the first remaining
            # portfolio) instead of holding a stale reference.
            st.session_state["portfolio_pending_active_tab"] = ""
            st.success(f'Deleted "{portfolio_name}".')
            st.rerun()


# ---------------------------------------------------------------------
# One tab per portfolio -- each independent and never affected by
# uploads to any other portfolio (multiple portfolios can freely
# coexist). A "+ New portfolio" tab is always available to start another
# one from scratch.
# ---------------------------------------------------------------------
portfolio_names = sorted({h.portfolio_name for h in saved_holdings})

if not portfolio_names:
    st.info("No holdings saved yet -- create your first portfolio below.")
    st.divider()
    st.subheader("Create a portfolio")
    first_name = st.text_input("Portfolio name", value="Portfolio 1", key="portfolio_first_name")
    first_broker = st.selectbox("Broker", BROKERS, key="portfolio_first_broker")
    _render_upload_section(
        portfolio_name=first_name.strip() or "Portfolio 1",
        broker=first_broker,
        key_prefix=f"first_{first_broker}",
        save_label="Create portfolio",
    )
else:
    # key="portfolio_active_tab" + on_change="rerun" makes Streamlit track
    # which tab is open in st.session_state, and -- in principle -- lets
    # us *set* st.session_state["portfolio_active_tab"] ourselves (e.g.
    # right after creating a portfolio) so the page opens straight to
    # that portfolio's tab instead of defaulting back to the first one.
    #
    # In practice, Streamlit forbids writing to a widget's session_state
    # key after that widget has already been instantiated in the current
    # run (StreamlitAPIException) -- and by the time a save/delete inside
    # one of the tabs below calls back up to here, st.tabs() has already
    # run earlier in *this* script execution. So a save/delete never
    # writes "portfolio_active_tab" directly; it stashes the request in
    # "portfolio_pending_active_tab" instead and calls st.rerun() --  on
    # the fresh run that follows, this promotion happens here, safely
    # *before* st.tabs() is instantiated for that run. "" means "clear"
    # (fall back to the first tab); any other string means "select this
    # one".
    _pending_active_tab = st.session_state.pop("portfolio_pending_active_tab", None)
    if _pending_active_tab == "":
        st.session_state.pop("portfolio_active_tab", None)
    elif _pending_active_tab:
        st.session_state["portfolio_active_tab"] = _pending_active_tab

    # Options for the dropdown are the actual expiry dates present, taken
    # live from the system (option_contracts) rather than a fixed "Near
    # month" style label -- these shift every month as monthly contracts
    # expire and new ones are listed, so a hardcoded label would drift out
    # of sync with what's actually being priced. Union across every
    # symbol held anywhere in this account (not just the active tab) so
    # switching tabs never resets the choice.
    all_portfolio_symbols = tuple(sorted({h.symbol for h in saved_holdings if h.symbol}))
    constituent_symbols = _load_constituent_symbols(client, st.session_state["portfolio_cache_bust"])
    try:
        all_expiries_by_symbol = _load_option_expiries(
            client, all_portfolio_symbols, st.session_state["portfolio_cache_bust"]
        )
    except APIError:
        all_expiries_by_symbol = {}
    cc_expiry_options = sorted({d for expiries in all_expiries_by_symbol.values() for d in expiries})[:3]

    if cc_expiry_options:
        if st.session_state.get("portfolio_cc_expiry") not in cc_expiry_options:
            st.session_state["portfolio_cc_expiry"] = cc_expiry_options[0]
        cc_expiry_iso = st.selectbox(
            "Covered call expiry",
            cc_expiry_options,
            key="portfolio_cc_expiry",
            format_func=lambda d: date.fromisoformat(d).strftime("%b %Y"),
            help=(
                "Which monthly expiry the CC ROI / Assignment ROI columns below use. "
                "If avg buy price is above LTP, the strike targeted is ~3% above avg buy price; "
                "otherwise it's ~5% above LTP."
            ),
        )
    else:
        st.selectbox("Covered call expiry", ["N/A"], disabled=True)
        cc_expiry_iso = None

    tabs = st.tabs(portfolio_names + ["+ New portfolio"], key="portfolio_active_tab", on_change="rerun")
    for name, tab in zip(portfolio_names, tabs[:-1]):
        with tab:
            _render_portfolio_tab(
                name, [h for h in saved_holdings if h.portfolio_name == name], cc_expiry_iso, constituent_symbols
            )
    with tabs[-1]:
        st.caption("Start a brand-new portfolio, separate from your existing one(s) -- nothing existing is affected.")
        new_name = st.text_input(
            "Portfolio name", key="portfolio_new_name", placeholder=f"Portfolio {len(portfolio_names) + 1}"
        )
        new_broker = st.selectbox("Broker", BROKERS, key="portfolio_new_broker")
        resolved_name = new_name.strip() or f"Portfolio {len(portfolio_names) + 1}"
        if resolved_name in portfolio_names:
            st.error(
                f'A portfolio named "{resolved_name}" already exists -- switch to that tab to update it, '
                "or pick a different name here."
            )
        else:

            def _open_new_portfolio_tab(created_name: str = resolved_name) -> None:
                st.session_state.pop("portfolio_new_name", None)
                st.session_state["portfolio_pending_active_tab"] = created_name

            _render_upload_section(
                portfolio_name=resolved_name,
                broker=new_broker,
                key_prefix=f"newportfolio_{new_broker}",
                save_label="Create portfolio",
                on_saved=_open_new_portfolio_tab,
            )
