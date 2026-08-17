from __future__ import annotations

from datetime import datetime, timedelta, timezone

import streamlit as st
from postgrest.exceptions import APIError
from pydantic import ValidationError

from src.data_providers.base import ProviderError
from src.data_providers.dhan_provider import DhanAuthError, DhanProvider
from src.data_providers.zerodha_provider import ZerodhaAuthError, ZerodhaProvider
from src.models.portfolio import BrokerConnection
from src.repositories import fo_repo, portfolio_repo, settings_repo
from src.services import portfolio_service
from src.utils.formatting import format_inr
from src.utils.portfolio_page import ensure_cache_bust, fmt_qty, load_all_companies, load_broker_connection, load_holdings, load_positions, slug
from src.utils.refresh_bar import render_global_refresh_bar
from src.utils.session import current_user_id, get_user_client_cached, require_login
from src.utils.timezones import now_ist, to_ist
from src.utils.ui import inject_global_styles, render_disclaimer

st.set_page_config(page_title="My Broker | Nifty 50 Screener", page_icon="\U0001f3e6", layout="wide")
require_login()  # already injects Tailwind + the light-theme CSS design system

client = get_user_client_cached()
user_id = current_user_id()
user_settings = settings_repo.get_user_settings(client, user_id)
inject_global_styles(user_settings.theme)  # re-inject with the user's actual theme

st.title("\U0001f3e6 My Broker")
render_disclaimer()
render_global_refresh_bar(client)
st.caption("Connect or upload your broker data here, per portfolio. See My Trades / My Holdings / My Positions for the resulting view.")

BROKERS = ["Zerodha", "Dhan"]
UPLOAD_TYPES = ["Holdings", "Positions"]

ensure_cache_bust()

try:
    saved_holdings = load_holdings(client, user_id, st.session_state["portfolio_cache_bust"])
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
    saved_positions = load_positions(client, user_id, st.session_state["portfolio_cache_bust"])
except APIError:
    # portfolio_positions doesn't exist yet (migration 0016) -- degrade
    # gracefully rather than st.stop(), since uploading Holdings should
    # keep working even if Positions isn't migrated yet.
    saved_positions = []

# Computed here (not just inside the tabs section below) so the
# Zerodha-redirect-return block above the tabs can also use it.
portfolio_names = sorted({h.portfolio_name for h in saved_holdings} | {p.portfolio_name for p in saved_positions})


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
            all_companies = load_all_companies(client, st.session_state["portfolio_cache_bust"])
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
            "Qty": fmt_qty(h["qty"]),
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
            "Qty": fmt_qty(p["qty"]),
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


def _zerodha_token_is_fresh(token_saved_at: datetime | None) -> bool:
    """Kite Connect access tokens expire at a fixed daily time (~6am
    IST), not on a rolling window like Dhan's -- so "is this token still
    good" means "was it saved after the most recent 6am IST boundary",
    not "is it less than N hours old". Approximates that boundary as the
    most recent 6am IST at or before now; a token saved before it is
    treated as stale (worth re-verifying against a real Kite Connect
    account -- the exact invalidation time isn't published to the
    minute)."""
    if token_saved_at is None:
        return False
    now = now_ist()
    boundary = now.replace(hour=6, minute=0, second=0, microsecond=0)
    if now < boundary:
        boundary -= timedelta(days=1)
    return to_ist(token_saved_at) >= boundary


def _fetch_fallback_option_chains(positions: list[dict]) -> dict[tuple[str, object], list[dict]]:
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
    so the rendered My Holdings/My Positions tables look identical
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


def _sync_zerodha(*, portfolio_name: str, broker: str, connection: BrokerConnection) -> None:
    """Pulls holdings + positions straight from Zerodha's Kite Connect
    API and replaces this portfolio's Zerodha-sourced rows -- same
    replace_broker_holdings/replace_broker_positions calls the CSV
    upload path and _sync_dhan use, so the rendered My Holdings/My
    Positions tables look identical regardless of source. Unlike Dhan,
    no fallback-LTP step is needed -- Kite's holdings/positions responses
    already include last_price directly (see
    portfolio_service.zerodha_positions_from_api)."""
    if not connection.access_token or not connection.api_secret:
        st.error('Not logged in yet -- click "Log in to Zerodha" below first.')
        return
    provider = ZerodhaProvider(
        api_key=connection.client_id, api_secret=connection.api_secret, access_token=connection.access_token
    )
    try:
        holding_rows = provider.get_holdings()
        position_rows = provider.get_positions()
    except ZerodhaAuthError:
        st.error(
            "Your Zerodha session has expired -- Kite Connect tokens expire daily (around 6am IST), "
            "not on a rolling window like Dhan's. Click \"Log in to Zerodha\" below to start a new session."
        )
        return
    except ProviderError as exc:
        st.error(f"Could not sync from Zerodha: {exc}")
        return

    holdings = portfolio_service.zerodha_holdings_from_api(holding_rows)
    positions = portfolio_service.zerodha_positions_from_api(position_rows)
    holding_records = portfolio_service.holdings_to_records(user_id, portfolio_name, broker, holdings)
    position_records = portfolio_service.positions_to_records(user_id, portfolio_name, broker, positions)
    portfolio_repo.replace_broker_holdings(client, user_id, portfolio_name, broker, holding_records)
    portfolio_repo.replace_broker_positions(client, user_id, portfolio_name, broker, position_records)
    st.session_state["portfolio_cache_bust"] += 1
    st.cache_data.clear()
    st.success(
        f"Synced {len(holding_records)} holding(s) and {len(position_records)} position(s) "
        f"from Zerodha to \"{portfolio_name}\"."
    )
    st.rerun()


def _render_zerodha_connect_section(*, portfolio_name: str, key_prefix: str, on_saved=None) -> None:
    """Enter a Kite Connect API Key + API Secret once (from a Kite
    Connect app registered at developers.kite.trade -- a paid
    subscription, entirely separate from this project), then "Log in to
    Zerodha" starts Kite's own OAuth-style login redirect: this app never
    sees the Zerodha password/TOTP. Landing back here with a
    `request_token` in the URL (handled near the top of this page, before
    the tabs) completes the session exchange and triggers the first
    sync. Unlike Dhan's pasted-token flow, there is no long-lived
    credential to copy -- the resulting access_token expires at a fixed
    daily time (~6am IST), so this login step repeats every trading day
    a sync is needed.

    `st.link_button` always opens a new browser tab (Streamlit's own
    documented behavior) -- if that new tab isn't already signed into
    this app, Zerodha's redirect will land on this app's own sign-in
    screen first; after signing in there, the pending Zerodha connection
    still completes normally (the request_token stays in the URL across
    that sign-in)."""
    try:
        connection = load_broker_connection(
            client, user_id, portfolio_name, "Zerodha", st.session_state["portfolio_cache_bust"]
        )
    except APIError:
        st.info(
            "Connecting a Zerodha account isn't set up yet. Apply migration "
            "`supabase/migrations/0022_broker_connections_api_secret.sql` in the Supabase SQL editor, "
            "then reload this page."
        )
        return

    if connection is None or not connection.api_secret:
        st.caption(
            "Register a Kite Connect app at developers.kite.trade (paid subscription required) and set its "
            "Redirect URL to this page's own URL. This app only ever reads your Holdings/Positions with the "
            "resulting session -- it never places or modifies orders -- but the API Secret/access token are "
            "stored as entered, protected the same way as everything else here (row-level security), not "
            "separately encrypted."
        )
        with st.form(f"zerodha_connect_form_{key_prefix}"):
            new_api_key = st.text_input("Kite Connect API Key")
            new_api_secret = st.text_input("Kite Connect API Secret", type="password")
            submitted = st.form_submit_button("Save")
        if submitted:
            if not new_api_key.strip() or not new_api_secret.strip():
                st.error("Both API Key and API Secret are required.")
                return
            new_connection = BrokerConnection(
                user_id=user_id,
                portfolio_name=portfolio_name,
                broker="Zerodha",
                client_id=new_api_key.strip(),
                api_secret=new_api_secret.strip(),
            )
            portfolio_repo.upsert_broker_connection(client, new_connection)
            st.session_state["portfolio_cache_bust"] += 1
            st.cache_data.clear()
            if on_saved:
                on_saved()
            st.rerun()
        return

    if _zerodha_token_is_fresh(connection.token_saved_at):
        masked_key = f"...{connection.client_id[-4:]}" if len(connection.client_id) > 4 else connection.client_id
        st.caption(f"Connected -- API Key {masked_key}, session started {_relative_age(_hours_since(connection.token_saved_at))}.")
        sync_col, disconnect_col = st.columns(2)
        with sync_col:
            if st.button("Sync now", key=f"zerodha_sync_{key_prefix}"):
                _sync_zerodha(portfolio_name=portfolio_name, broker="Zerodha", connection=connection)
        with disconnect_col:
            if st.button("Disconnect", key=f"zerodha_disconnect_{key_prefix}"):
                portfolio_repo.delete_broker_connection(client, user_id, portfolio_name, "Zerodha")
                st.session_state["portfolio_cache_bust"] += 1
                st.cache_data.clear()
                st.success("Disconnected. Previously synced holdings/positions are unaffected.")
                st.rerun()
    else:
        if connection.access_token is not None:
            st.warning(
                "Your Zerodha session has likely expired -- Kite Connect tokens expire daily "
                "(around 6am IST). Log in again below."
            )
        provider = ZerodhaProvider(api_key=connection.client_id, api_secret=connection.api_secret)
        st.session_state["zerodha_connect_pending"] = {"portfolio_name": portfolio_name}
        st.link_button("Log in to Zerodha", provider.login_url())
        st.caption("Opens in a new tab. If that tab asks you to sign in here first, do so -- the connection will still complete.")

    with st.expander("Update API Key / Secret"):
        st.caption("Changing these doesn't clear an existing session -- \"Sync now\" will simply fail and prompt a fresh login if it's no longer valid under the new credentials.")
        with st.form(f"zerodha_update_form_{key_prefix}"):
            updated_api_key = st.text_input("Kite Connect API Key", value=connection.client_id)
            updated_api_secret = st.text_input(
                "Kite Connect API Secret", type="password", placeholder="Leave blank to keep the current secret"
            )
            update_submitted = st.form_submit_button("Save")
        if update_submitted:
            if not updated_api_key.strip():
                st.error("API Key is required.")
                return
            updated_connection = BrokerConnection(
                user_id=user_id,
                portfolio_name=portfolio_name,
                broker="Zerodha",
                client_id=updated_api_key.strip(),
                api_secret=updated_api_secret.strip() or connection.api_secret,
            )
            portfolio_repo.upsert_broker_connection(client, updated_connection)
            st.session_state["portfolio_cache_bust"] += 1
            st.cache_data.clear()
            st.rerun()


def _render_dhan_connect_section(*, portfolio_name: str, key_prefix: str, on_saved=None) -> None:
    """Enter a Dhan Client ID + Access Token once, then "Sync now" pulls
    holdings + positions straight from Dhan's API -- no CSV export needed.
    See _render_zerodha_connect_section below for Zerodha's equivalent
    (a genuinely different flow -- Kite Connect's OAuth-style login
    redirect, not a pasted token)."""
    try:
        connection = load_broker_connection(
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
    this page (an existing portfolio's tab, the first-portfolio flow, and
    the "+ New portfolio" tab) -- routes to the holdings or positions
    upload flow based on the selection, both of which share the same
    (portfolio_name, broker, key_prefix, save_label, on_saved) contract.
    For Dhan and Zerodha, a CSV-vs-API toggle comes first -- both brokers'
    "Connect" flows pull holdings/positions live instead of a file
    upload, though the two flows work quite differently under the hood
    (see _render_dhan_connect_section/_render_zerodha_connect_section)."""
    if broker in ("Dhan", "Zerodha"):
        connect_mode = st.radio(
            f"How do you want to add {broker} data?",
            ["Upload CSV", f"Connect {broker} account"],
            key=f"portfolio_{broker.lower()}_mode_{key_prefix}",
            horizontal=True,
        )
        if connect_mode == f"Connect {broker} account":
            if broker == "Dhan":
                _render_dhan_connect_section(portfolio_name=portfolio_name, key_prefix=key_prefix, on_saved=on_saved)
            else:
                _render_zerodha_connect_section(portfolio_name=portfolio_name, key_prefix=key_prefix, on_saved=on_saved)
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


def _render_broker_tab(portfolio_name: str) -> None:
    st.caption(f'Uploading a broker\'s file replaces that broker\'s previously saved holdings or positions in "{portfolio_name}".')
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
            f'This permanently deletes every holding, position, saved broker connection, and Trade grouping in '
            f'"{portfolio_name}" (every broker within it). This cannot be undone.'
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
            st.session_state["broker_pending_active_tab"] = ""
            st.success(f'Deleted "{portfolio_name}".')
            st.rerun()


# ---------------------------------------------------------------------
# Zerodha login redirect landing here: Kite Connect always redirects back
# to this Kite Connect app's own configured Redirect URL with
# `request_token` in the query string on a successful login. Handled
# before the tabs render, and deliberately NOT dependent on
# st.session_state["zerodha_connect_pending"] (set by
# _render_zerodha_connect_section right before the login link) surviving
# the round trip -- st.link_button always opens a new browser tab
# (Streamlit's own documented behavior), which creates a brand-new
# Streamlit session there, so any session_state from the tab that
# initiated the login is not guaranteed to be present. The portfolio
# picker below defaults to it when available, but works either way.
# ---------------------------------------------------------------------
if st.query_params.get("status") == "error":
    st.error("Zerodha login was cancelled or failed -- click \"Log in to Zerodha\" again to retry.")
    st.query_params.clear()
elif "request_token" in st.query_params:
    st.divider()
    st.subheader("Completing Zerodha login")
    request_token = st.query_params["request_token"]
    pending_portfolio = (st.session_state.get("zerodha_connect_pending") or {}).get("portfolio_name")
    picker_options = portfolio_names or ["Portfolio 1"]
    default_index = picker_options.index(pending_portfolio) if pending_portfolio in picker_options else 0
    target_portfolio = st.selectbox(
        "Save this Zerodha connection to which portfolio?", picker_options, index=default_index
    )
    if st.button("Save & Sync", key="zerodha_redirect_save_sync"):
        pending_connection = load_broker_connection(
            client, user_id, target_portfolio, "Zerodha", st.session_state["portfolio_cache_bust"]
        )
        if pending_connection is None or not pending_connection.api_secret:
            st.error(
                f'No saved Zerodha API Key/Secret found for "{target_portfolio}" -- go to that portfolio\'s '
                'tab, enter them under "Connect Zerodha account", then click "Log in to Zerodha" again.'
            )
        else:
            provider = ZerodhaProvider(api_key=pending_connection.client_id, api_secret=pending_connection.api_secret)
            try:
                access_token = provider.generate_session(request_token)
            except ProviderError as exc:
                st.error(f"Could not complete Zerodha login: {exc}")
            else:
                updated_connection = BrokerConnection(
                    user_id=user_id,
                    portfolio_name=target_portfolio,
                    broker="Zerodha",
                    client_id=pending_connection.client_id,
                    api_secret=pending_connection.api_secret,
                    access_token=access_token,
                    token_saved_at=datetime.now(timezone.utc),
                )
                portfolio_repo.upsert_broker_connection(client, updated_connection)
                st.session_state["portfolio_cache_bust"] += 1
                st.cache_data.clear()
                st.session_state.pop("zerodha_connect_pending", None)
                st.session_state["broker_pending_active_tab"] = target_portfolio
                st.query_params.clear()
                _sync_zerodha(portfolio_name=target_portfolio, broker="Zerodha", connection=updated_connection)
    st.divider()

# ---------------------------------------------------------------------
# One tab per portfolio -- each independent and never affected by
# uploads to any other portfolio. A "+ New portfolio" tab is always
# available to start another one from scratch -- this is the only page
# where a brand-new portfolio can be created.
# ---------------------------------------------------------------------
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
    # Same "pending active tab" promotion pattern as the old combined
    # Portfolio page -- Streamlit forbids writing to a widget's own
    # session_state key after that widget has already been instantiated in
    # the current run, so a save/delete stashes the request and reruns;
    # the promotion below happens on the fresh run, safely before st.tabs()
    # is instantiated for that run. "" means "clear" (fall back to the
    # first tab); any other string means "select this one".
    _pending_active_tab = st.session_state.pop("broker_pending_active_tab", None)
    if _pending_active_tab == "":
        st.session_state.pop("broker_active_tab", None)
    elif _pending_active_tab:
        st.session_state["broker_active_tab"] = _pending_active_tab

    tabs = st.tabs(portfolio_names + ["+ New portfolio"], key="broker_active_tab", on_change="rerun")
    for name, tab in zip(portfolio_names, tabs[:-1]):
        with tab:
            _render_broker_tab(name)
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
                st.session_state["broker_pending_active_tab"] = created_name

            _render_upload_section(
                portfolio_name=resolved_name,
                broker=new_broker,
                key_prefix=f"newportfolio_{new_broker}",
                save_label="Create portfolio",
                on_saved=_open_new_portfolio_tab,
            )
