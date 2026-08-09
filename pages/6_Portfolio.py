from __future__ import annotations

import re
from datetime import date, datetime, timezone

import pandas as pd
import streamlit as st
from postgrest.exceptions import APIError
from pydantic import ValidationError

from src.data_providers.base import ProviderError
from src.data_providers.dhan_provider import DhanAuthError, DhanProvider
from src.models.portfolio import BrokerConnection
from src.repositories import companies_repo, fo_repo, portfolio_repo, settings_repo, snapshot_repo
from src.services import fo_service, portfolio_service
from src.utils.formatting import format_inr, format_pct
from src.utils.session import current_user_id, get_user_client_cached, require_login
from src.utils.ui import inject_global_styles, render_disclaimer, render_stat_grid

st.set_page_config(page_title="My Portfolio | Nifty 50 Screener", page_icon="\U0001f4bc", layout="wide")
require_login()  # already injects Tailwind + the light-theme CSS design system

client = get_user_client_cached()
user_id = current_user_id()
user_settings = settings_repo.get_user_settings(client, user_id)
inject_global_styles(user_settings.theme)  # re-inject with the user's actual theme

st.title("\U0001f4bc My Portfolio")
render_disclaimer()

BROKERS = ["Zerodha", "Dhan"]
UPLOAD_TYPES = ["Holdings", "Positions"]


@st.cache_data(ttl=60, show_spinner=False)
def _load_holdings(_client, _user_id: str, _cache_bust: int):
    return portfolio_repo.list_holdings(_client, _user_id)


@st.cache_data(ttl=60, show_spinner=False)
def _load_positions(_client, _user_id: str, _cache_bust: int):
    return portfolio_repo.list_positions(_client, _user_id)


@st.cache_data(ttl=60, show_spinner=False)
def _load_broker_connection(_client, _user_id: str, portfolio_name: str, broker: str, _cache_bust: int):
    return portfolio_repo.get_broker_connection(_client, _user_id, portfolio_name, broker)


@st.cache_data(ttl=300, show_spinner=False)
def _load_all_companies(_client, _cache_bust: int):
    return companies_repo.list_all_companies(_client)


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

try:
    saved_positions = _load_positions(client, user_id, st.session_state["portfolio_cache_bust"])
    positions_available = True
except APIError:
    # portfolio_positions doesn't exist yet (migration 0016) -- degrade
    # gracefully rather than st.stop(), since Holdings should keep working
    # even if Positions isn't migrated yet.
    saved_positions = []
    positions_available = False


def _fmt_qty(value: float) -> str:
    if value == int(value):
        return f"{int(value):,}"
    return f"{value:,.2f}"


def _slug(text: str) -> str:
    """CSS-class-safe form of an arbitrary portfolio name, for the
    `st-key-*` selectors the search-icon column's CSS scoping relies on."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "x"


def _render_holdings_upload_section(
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


def _render_positions_upload_section(
    *, portfolio_name: str, broker: str, key_prefix: str, save_label: str, on_saved=None
) -> None:
    """Same replace-on-upload flow as _render_holdings_upload_section, for
    F&O positions instead. No manual-symbol override here: unlike a
    holding's free-text name, a position's instrument string already
    encodes its own contract identity -- a row that fails to decode
    (unsupported format) is just saved and shown with no contract detail,
    there's nothing for the user to correct."""
    uploaded_file = st.file_uploader(f"{broker} positions CSV", type="csv", key=f"portfolio_position_upload_{key_prefix}")
    if uploaded_file is None:
        return

    parse_failed = False
    try:
        if broker == "Zerodha":
            parsed = portfolio_service.parse_zerodha_positions_csv(uploaded_file)
        else:
            parsed = portfolio_service.parse_dhan_positions_csv(uploaded_file)
    except Exception as exc:  # noqa: BLE001 -- arbitrary malformed user-uploaded file
        st.error(f"Could not read this file as a {broker} positions export: {exc}")
        parsed = []
        parse_failed = True

    if not parsed:
        if not parse_failed:
            st.warning("No position rows found in this file.")
        return

    preview_rows = [
        {
            "Instrument": p["raw_name"],
            "Symbol": p["symbol"] or "(undecoded)",
            "Expiry": p["expiry_date"].strftime("%d %b %Y") if p["expiry_date"] else "—",
            "Strike": p["strike_price"] if p["strike_price"] is not None else "—",
            "Type": p["option_type"].value if p["option_type"] else "—",
            "Qty": _fmt_qty(p["qty"]),
            "Avg Price": format_inr(p["avg_price"]),
            "LTP": format_inr(p["ltp"]) if p["ltp"] is not None else "N/A",
        }
        for p in parsed
    ]
    st.dataframe(preview_rows, use_container_width=True, hide_index=True)

    undecoded = sum(1 for p in parsed if p["symbol"] is None)
    if undecoded:
        st.info(
            f"{undecoded} row(s) are in a contract format this app doesn't decode yet -- "
            "they'll still be saved and shown, just with no expiry/strike/type."
        )

    if st.button(save_label, key=f"portfolio_position_save_{key_prefix}"):
        records = portfolio_service.positions_to_records(user_id, portfolio_name, broker, parsed)
        portfolio_repo.replace_broker_positions(client, user_id, portfolio_name, broker, records)
        st.session_state["portfolio_cache_bust"] += 1
        st.cache_data.clear()
        st.success(f"Saved {len(records)} position(s) from {broker} to \"{portfolio_name}\".")
        if on_saved:
            on_saved()
        st.rerun()


def _hours_since(dt: datetime) -> float:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600


def _relative_age(hours: float) -> str:
    if hours < 1:
        return f"{max(int(hours * 60), 1)} minute(s) ago"
    if hours < 48:
        return f"{int(hours)} hour(s) ago"
    return f"{int(hours / 24)} day(s) ago"


def _fetch_fallback_option_chains(positions: list[dict]) -> dict[tuple[str, date], list[dict]]:
    """Fetches this app's own F&O chain (option_daily_prices via
    latest_option_chain_view) for every (symbol, expiry) still missing an
    `ltp` -- the fallback source portfolio_service.apply_fallback_option_ltp
    matches against. Needed for a Dhan account without the separate "Data
    APIs" subscription (the Market Quote call 401s, see _sync_dhan) or for
    a security Dhan's own feed simply omits."""
    needed = {
        (p["symbol"], p["expiry_date"])
        for p in positions
        if p["ltp"] is None and p["symbol"] and p["expiry_date"] and p["option_type"] and p["strike_price"] is not None
    }
    return {(symbol, expiry_date): fo_repo.get_option_chain(client, symbol, expiry_date) for symbol, expiry_date in needed}


def _sync_dhan(*, portfolio_name: str, broker: str, connection: BrokerConnection) -> None:
    """Pulls holdings + positions straight from Dhan's API and replaces
    this portfolio's Dhan-sourced rows -- the exact same repo calls the
    CSV upload path uses (replace_broker_holdings/replace_broker_positions),
    so the rendered "My Holdings"/"My Positions" tables look identical
    regardless of which path populated them."""
    provider = DhanProvider(client_id=connection.client_id, access_token=connection.access_token)
    try:
        holding_rows = provider.get_holdings()
        position_rows = provider.get_positions()
    except DhanAuthError:
        st.error(
            "Your Dhan access token was rejected -- it's likely expired (Dhan tokens last ~24 hours). "
            "Generate a new one on web.dhan.co and paste it below."
        )
        return
    except ProviderError as exc:
        st.error(f"Could not sync from Dhan: {exc}")
        return

    security_ids_by_segment: dict[str, list[str]] = {}
    for row in position_rows:
        if row.get("netQty") and row.get("securityId") and row.get("exchangeSegment"):
            security_ids_by_segment.setdefault(row["exchangeSegment"], []).append(str(row["securityId"]))
    try:
        ltp_by_security_id = provider.get_ltp_by_security_id(security_ids_by_segment) if security_ids_by_segment else {}
    except (DhanAuthError, ProviderError):
        # Holdings/positions themselves already came back fine with this
        # token -- degrade to no LTP (positions still save, shown as N/A)
        # rather than discarding a sync that mostly worked.
        ltp_by_security_id = {}

    holdings = portfolio_service.dhan_holdings_from_api(holding_rows)
    positions = portfolio_service.dhan_positions_from_api(position_rows, ltp_by_security_id)
    option_chains = _fetch_fallback_option_chains(positions)
    positions = portfolio_service.apply_fallback_option_ltp(positions, option_chains)
    holding_records = portfolio_service.holdings_to_records(user_id, portfolio_name, broker, holdings)
    position_records = portfolio_service.positions_to_records(user_id, portfolio_name, broker, positions)
    portfolio_repo.replace_broker_holdings(client, user_id, portfolio_name, broker, holding_records)
    portfolio_repo.replace_broker_positions(client, user_id, portfolio_name, broker, position_records)
    st.session_state["portfolio_cache_bust"] += 1
    st.cache_data.clear()
    st.success(
        f"Synced {len(holding_records)} holding(s) and {len(position_records)} position(s) "
        f"from Dhan to \"{portfolio_name}\"."
    )
    st.rerun()


def _render_dhan_connect_section(*, portfolio_name: str, key_prefix: str, on_saved=None) -> None:
    """Enter a Dhan Client ID + Access Token once, then "Sync now" pulls
    holdings + positions straight from Dhan's API -- no CSV export needed.
    Only offered for the Dhan broker (see _render_upload_section); Zerodha
    stays CSV-only since its API needs a separate paid subscription."""
    try:
        connection = _load_broker_connection(
            client, user_id, portfolio_name, "Dhan", st.session_state["portfolio_cache_bust"]
        )
    except APIError:
        st.info(
            "Connecting a Dhan account isn't set up yet. Apply migration "
            "`supabase/migrations/0017_broker_connections.sql` in the Supabase SQL editor, "
            "then reload this page."
        )
        return

    if connection is None:
        st.caption(
            "Generate an Access Token on web.dhan.co -> Profile -> \"DhanHQ Trading APIs\" "
            "(valid for 24 hours). This app only ever reads your Holdings/Positions/quotes with "
            "it -- it never places or modifies orders -- but the token is stored as entered in "
            "your account's data, protected the same way as everything else here (row-level "
            "security), not separately encrypted. Anyone who could read your account's data could "
            "use it -- including to trade -- until it expires."
        )
        with st.form(f"dhan_connect_form_{key_prefix}"):
            new_client_id = st.text_input("Dhan Client ID")
            new_token = st.text_input("Dhan Access Token", type="password")
            submitted = st.form_submit_button("Save & Sync")
        if submitted:
            if not new_client_id.strip() or not new_token.strip():
                st.error("Both Client ID and Access Token are required.")
                return
            new_connection = BrokerConnection(
                user_id=user_id,
                portfolio_name=portfolio_name,
                broker="Dhan",
                client_id=new_client_id.strip(),
                access_token=new_token.strip(),
                token_saved_at=datetime.now(timezone.utc),
            )
            portfolio_repo.upsert_broker_connection(client, new_connection)
            st.session_state["portfolio_cache_bust"] += 1
            st.cache_data.clear()
            if on_saved:
                on_saved()
            _sync_dhan(portfolio_name=portfolio_name, broker="Dhan", connection=new_connection)
        return

    masked_id = f"...{connection.client_id[-4:]}" if len(connection.client_id) > 4 else connection.client_id
    if connection.token_saved_at is not None:
        hours_old = _hours_since(connection.token_saved_at)
        st.caption(f"Connected -- Client ID {masked_id}, token saved {_relative_age(hours_old)}.")
        if hours_old >= 23:
            st.warning(
                "This token is likely expired (Dhan tokens last ~24 hours) -- regenerate it on "
                "web.dhan.co and update it below."
            )
    else:
        st.caption(f"Connected -- Client ID {masked_id}.")

    sync_col, disconnect_col = st.columns(2)
    with sync_col:
        if st.button("Sync now", key=f"dhan_sync_{key_prefix}"):
            _sync_dhan(portfolio_name=portfolio_name, broker="Dhan", connection=connection)
    with disconnect_col:
        if st.button("Disconnect", key=f"dhan_disconnect_{key_prefix}"):
            portfolio_repo.delete_broker_connection(client, user_id, portfolio_name, "Dhan")
            st.session_state["portfolio_cache_bust"] += 1
            st.cache_data.clear()
            st.success("Disconnected. Previously synced holdings/positions are unaffected.")
            st.rerun()

    with st.expander("Update credentials"):
        with st.form(f"dhan_update_form_{key_prefix}"):
            updated_client_id = st.text_input("Dhan Client ID", value=connection.client_id)
            updated_token = st.text_input(
                "Dhan Access Token", type="password", placeholder="Paste a freshly generated token"
            )
            update_submitted = st.form_submit_button("Save & Sync")
        if update_submitted:
            if not updated_client_id.strip() or not updated_token.strip():
                st.error("Both Client ID and Access Token are required.")
                return
            updated_connection = BrokerConnection(
                user_id=user_id,
                portfolio_name=portfolio_name,
                broker="Dhan",
                client_id=updated_client_id.strip(),
                access_token=updated_token.strip(),
                token_saved_at=datetime.now(timezone.utc),
            )
            portfolio_repo.upsert_broker_connection(client, updated_connection)
            st.session_state["portfolio_cache_bust"] += 1
            st.cache_data.clear()
            _sync_dhan(portfolio_name=portfolio_name, broker="Dhan", connection=updated_connection)


def _render_upload_section(
    *, portfolio_name: str, broker: str, key_prefix: str, save_label: str, on_saved=None
) -> None:
    """What-are-you-uploading dispatcher shared by every "Upload" spot on
    the page (an existing portfolio's tab, the first-portfolio flow, and
    the "+ New portfolio" tab) -- routes to the holdings or positions
    upload flow based on the selection, both of which share the same
    (portfolio_name, broker, key_prefix, save_label, on_saved) contract.
    For Dhan specifically, a CSV-vs-API toggle comes first -- Zerodha has
    no API path here, so it goes straight to the upload-type picker."""
    if broker == "Dhan":
        connect_mode = st.radio(
            "How do you want to add Dhan data?",
            ["Upload CSV", "Connect Dhan account"],
            key=f"portfolio_dhan_mode_{key_prefix}",
            horizontal=True,
        )
        if connect_mode == "Connect Dhan account":
            _render_dhan_connect_section(portfolio_name=portfolio_name, key_prefix=key_prefix, on_saved=on_saved)
            return

    upload_type = st.selectbox("What are you uploading?", UPLOAD_TYPES, key=f"portfolio_upload_type_{key_prefix}")
    if upload_type == "Holdings":
        _render_holdings_upload_section(
            portfolio_name=portfolio_name, broker=broker, key_prefix=key_prefix, save_label=save_label, on_saved=on_saved
        )
    else:
        _render_positions_upload_section(
            portfolio_name=portfolio_name, broker=broker, key_prefix=key_prefix, save_label=save_label, on_saved=on_saved
        )


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
    portfolio_name: str, holdings_for_portfolio: list, positions_for_portfolio: list, cc_expiry_iso: str | None
) -> None:
    """My Holdings table, My Positions table (both merged across brokers
    within this one portfolio), plus this portfolio's own upload section
    -- everything a tab shows."""
    st.subheader("My Holdings")
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
    for r in rows:
        stock = r["symbol"] or f'{r["raw_name"]} (unmatched)'
        cc = cc_by_symbol.get(r["symbol"]) if r["symbol"] else None
        table_rows.append(
            {
                "Stock": stock,
                "Qty": r["qty"],
                "Avg Price": r["avg_price"],
                "LTP": r["ltp"],
                "Investment": r["investment"],
                "Cur Val": r["cur_val"],
                "P&L": r["pnl"],
                "P&L %": r["pnl_pct"],
                "CC ROI": cc["cc_roi_pct"] if cc and cc["cc_roi_pct"] is not None else None,
                "CC Assignment ROI": cc["assignment_roi_pct"] if cc and cc["assignment_roi_pct"] is not None else None,
            }
        )

    st.caption(
        "CC ROI is the ROI when the call expires OTM. CC Assignment ROI is the ROI if the stock gets "
        "called away. In both cases the ROI is considered against the total invested amount of the "
        "stock, not just the margin. It is assumed that the stock is pledged and the pledge is used "
        "as margin."
    )

    # A plain st.dataframe (same as the Futures table on the Options page)
    # instead of the hand-rendered render_screener_table -- its column
    # headers are natively clickable/sortable in the browser, which the
    # HTML table can't do without a JS bridge back to Python (a real
    # <a href> sort link would force a browser navigation, and this app
    # keeps the Supabase session only in st.session_state -- see
    # session.py's docstring -- so that would log the user out).
    #
    # That native client-side sort is exactly why the old per-row "open
    # detail" button column (one st.button beside each table row) had to
    # go: those buttons are positioned by their *pre-sort* Python index, so
    # clicking a header to reorder the table in the browser would leave
    # them pointing at the wrong row. Row selection (on_select="rerun")
    # sidesteps this -- Streamlit maps a click back to the correct row in
    # the original data regardless of how the table is currently sorted --
    # so a pair of buttons below the table replaces the whole column.
    #
    # Columns are kept as real numbers (not format_inr()/format_pct()
    # strings) and formatted for display via column_config instead: a
    # native header-click sort compares the *underlying* cell value, so a
    # column of currency strings like "₹10,81,452.10"/"₹9,80,135.00" sorts
    # alphabetically ("1" < "9") rather than by amount -- exactly the bug
    # this replaced. NumberColumn's printf-style format has no Indian
    # lakh/crore grouping, so amounts show Western 3-digit comma grouping
    # here (e.g. "₹1,081,452.10") instead of format_inr's "₹10,81,452.10".
    event = st.dataframe(
        pd.DataFrame(table_rows),
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key=f"portfolio_table_{_slug(portfolio_name)}",
        column_config={
            "Qty": st.column_config.NumberColumn(format="%,.0f"),
            "Avg Price": st.column_config.NumberColumn(format="₹%,.2f"),
            "LTP": st.column_config.NumberColumn(format="₹%,.2f"),
            "Investment": st.column_config.NumberColumn(format="₹%,.2f"),
            "Cur Val": st.column_config.NumberColumn(format="₹%,.2f"),
            "P&L": st.column_config.NumberColumn(format="₹%,.2f"),
            "P&L %": st.column_config.NumberColumn(format="%+.2f%%"),
            "CC ROI": st.column_config.NumberColumn(format="%.2f%%"),
            "CC Assignment ROI": st.column_config.NumberColumn(format="%+.2f%%"),
        },
    )
    selected_rows = event.selection.rows if event and event.selection else []
    if selected_rows:
        selected_symbol = rows[selected_rows[0]]["symbol"]
        if selected_symbol:
            # Every resolved symbol here is, by definition, one of this
            # signed-in user's own portfolio symbols -- and both Stock
            # Detail's and Options' own symbol pickers now union in
            # exactly that set (pages/2_Stock_Detail.py, pages/5_Options.py),
            # so any resolved row is always viewable on either page.
            detail_col, options_col = st.columns(2)
            with detail_col:
                if st.button(
                    f"Open {selected_symbol} in Stock Detail", key=f"portfolio_open_detail_{portfolio_name}"
                ):
                    st.session_state["selected_symbol"] = selected_symbol
                    st.switch_page("pages/2_Stock_Detail.py")
            with options_col:
                if st.button(
                    f"Open {selected_symbol} in Options", key=f"portfolio_open_options_{portfolio_name}"
                ):
                    st.session_state["fo_symbol"] = selected_symbol
                    st.switch_page("pages/5_Options.py")
        else:
            st.caption("This holding has no resolved symbol yet, so there's no Stock Detail or Options page for it.")

    st.divider()
    st.subheader("My Positions")
    if not positions_available:
        st.info(
            "Positions isn't set up yet. Apply migration "
            "`supabase/migrations/0016_portfolio_positions.sql` in the Supabase SQL editor, "
            "then reload this page."
        )
    elif not positions_for_portfolio:
        st.caption("No positions saved yet -- upload one below.")
    else:
        position_rows = [
            {
                "raw_name": p.raw_name,
                "symbol": p.symbol,
                "expiry_date": p.expiry_date,
                "strike_price": p.strike_price,
                "option_type": p.option_type,
                "qty": p.qty,
                "avg_price": p.avg_price,
                "ltp": p.ltp,
            }
            for p in positions_for_portfolio
        ]
        computed_positions = portfolio_service.compute_positions_view(position_rows)
        computed_positions.sort(key=lambda p: (p["symbol"] or p["raw_name"], p["expiry_date"] or date.max))

        priced = [p for p in computed_positions if p["pnl"] is not None]
        st.markdown(
            render_stat_grid(
                [("Total P&L", format_inr(sum(p["pnl"] for p in priced)) if priced else "N/A", None)],
                user_settings.theme,
                cols=1,
            ),
            unsafe_allow_html=True,
        )
        if len(priced) < len(computed_positions):
            st.caption(
                f"Total excludes {len(computed_positions) - len(priced)} position(s) with no LTP available "
                "(shown as N/A below)."
            )
        st.caption(
            "P&L is (LTP - Avg Price) x Qty (qty is signed -- negative is short) against each position's "
            "last-known LTP, not live -- index F&O (NIFTY/BANKNIFTY/SENSEX) isn't tracked by this app's own "
            "market data."
        )

        position_table_rows = [
            {
                "Symbol": p["symbol"] or f'{p["raw_name"]} (undecoded)',
                "Expiry": p["expiry_date"].strftime("%d %b %Y") if p["expiry_date"] else None,
                "Strike": p["strike_price"],
                "Type": p["option_type"].value if p["option_type"] else None,
                "Qty": p["qty"],
                "Avg Price": p["avg_price"],
                "LTP": p["ltp"],
                "P&L": p["pnl"],
                "P&L %": p["pnl_pct"],
            }
            for p in computed_positions
        ]
        st.dataframe(
            pd.DataFrame(position_table_rows),
            use_container_width=True,
            hide_index=True,
            key=f"portfolio_positions_table_{_slug(portfolio_name)}",
            column_config={
                "Qty": st.column_config.NumberColumn(format="%+,.0f"),
                "Avg Price": st.column_config.NumberColumn(format="₹%,.2f"),
                "LTP": st.column_config.NumberColumn(format="₹%,.2f"),
                "P&L": st.column_config.NumberColumn(format="₹%,.2f"),
                "P&L %": st.column_config.NumberColumn(format="%+.2f%%"),
            },
        )

    st.divider()
    st.subheader("Upload")
    st.caption("Uploading a broker's file replaces that broker's previously saved holdings or positions in this portfolio.")
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
            f'This permanently deletes every holding, position, and saved broker connection in "{portfolio_name}" '
            "(every broker within it). This cannot be undone."
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
portfolio_names = sorted({h.portfolio_name for h in saved_holdings} | {p.portfolio_name for p in saved_positions})

if not portfolio_names:
    st.info("No holdings or positions saved yet -- create your first portfolio below.")
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
                "Which monthly expiry the CC ROI / CC Assignment ROI columns below use. "
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
                name,
                [h for h in saved_holdings if h.portfolio_name == name],
                [p for p in saved_positions if p.portfolio_name == name],
                cc_expiry_iso,
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
