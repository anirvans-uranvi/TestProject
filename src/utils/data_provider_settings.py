"""Settings' "Data Provider" section (pages/4_Settings.py) -- lets an
account pick Dhan / Zerodha / YFinance+Bhavcopy as the live source for its
own stock LTP everywhere it's shown (Dashboard, Stock Detail, and the
portfolio pages' own broker-live overrides -- see
src/utils/portfolio_page.py's load_live_broker_prices), and, for Dhan/
Zerodha, connects the account-wide broker credential this app also uses to
sync holdings/positions.

Replaces pages/6_My_Broker.py's per-portfolio "Connect ... account" flow
(migration 0029 collapsed broker_connections to one row per (user_id,
broker), no longer scoped to an individual portfolio_name). CSV upload is
dropped entirely -- connecting here (which always syncs immediately) and
the Portfolio Refresh button on My Trades/My Holdings/My Positions/My CSP
(src/utils/refresh_bar.py, via this module's sync_broker_portfolio) are
now the only ways to populate holdings/positions, always targeting
portfolio_repo.get_or_default_portfolio_name's one resolved portfolio (see
that function's docstring for why there's no portfolio-name picker here
anymore).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import streamlit as st
from postgrest.exceptions import APIError

from src.data_providers.base import ProviderError
from src.data_providers.dhan_provider import DhanAuthError, DhanProvider, refresh_dhan_instrument_master
from src.data_providers.zerodha_provider import ZerodhaAuthError, ZerodhaProvider
from src.models.enums import FetchStatus, FetchType
from src.models.fetch_log import ProviderFetchLog
from src.models.portfolio import BrokerConnection
from src.models.user import UserSettings
from src.repositories import fetch_log_repo, fo_repo, portfolio_repo, settings_repo
from src.services import portfolio_service
from src.utils.portfolio_page import ensure_cache_bust
from src.utils.timezones import format_ist, now_ist, to_ist

_PROVIDER_LABELS = {"yfinance_bhavcopy": "YFinance + NSE/BSE Bhavcopy", "dhan": "Dhan", "zerodha": "Zerodha"}


def _bump_cache_bust() -> None:
    st.session_state["portfolio_cache_bust"] = st.session_state.get("portfolio_cache_bust", 0) + 1
    st.cache_data.clear()


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
    most recent 6am IST at or before now."""
    if token_saved_at is None:
        return False
    now = now_ist()
    boundary = now.replace(hour=6, minute=0, second=0, microsecond=0)
    if now < boundary:
        boundary -= timedelta(days=1)
    return to_ist(token_saved_at) >= boundary


def _fetch_fallback_option_chains(client, positions: list[dict]) -> dict[tuple[str, object], list[dict]]:
    """Fetches this app's own F&O chain (option_daily_prices via
    latest_option_chain_view) for every (symbol, expiry) still missing an
    `ltp` -- the fallback source portfolio_service.apply_fallback_option_ltp
    matches against. Needed for a Dhan account without the separate "Data
    APIs" subscription (the Market Quote call 401s) or for a security
    Dhan's own feed simply omits."""
    needed = {
        (p["symbol"], p["expiry_date"])
        for p in positions
        if p["ltp"] is None and p["symbol"] and p["expiry_date"] and p["option_type"] and p["strike_price"] is not None
    }
    return {(symbol, expiry_date): fo_repo.get_option_chain(client, symbol, expiry_date) for symbol, expiry_date in needed}


def _default_new_position_trade_dates(*, client, user_id: str, portfolio_name: str, broker: str, positions: list[dict]) -> None:
    """Tags every just-synced position leg that has no Trade Date yet
    with today's date -- so My CSP's Target P&L never gets stuck at N/A
    for a user who never visits Analyse Trade's Trade Date form. Never
    overwrites an already-set trade_date."""
    existing_dates = {
        (m.broker, m.raw_name): m.trade_date
        for m in portfolio_repo.list_position_meta(client, user_id)
        if m.portfolio_name == portfolio_name
    }
    today = date.today()
    for p in positions:
        if existing_dates.get((broker, p["raw_name"])) is None:
            portfolio_repo.set_position_trade_date(client, user_id, portfolio_name, broker, p["raw_name"], today)


def _auto_classify_new_trades(*, client, user_id: str, portfolio_name: str) -> None:
    """Auto-detects and saves a strategy label (CSP/Covered Call/Strangle/
    Jade Lizard/Twisted Sister -- portfolio_service.classify_trade_type)
    for any Trade that doesn't have a portfolio_trade_meta row yet --
    i.e. one this account has never visited Analyse Trade for, whether it
    already existed before this sync or a leg just synced into existence
    for the first time. A trade WITH a row is treated as already
    user-classified and is never touched here -- group_into_trades'
    trade_type_mismatch instead *flags* (at render time, on My Trades/
    Analyse Trade) when such a trade's current legs no longer match its
    saved type, rather than silently overwriting the user's own label.

    Re-reads the full current holdings+positions for this portfolio
    (every broker, not just the one just synced) so a strategy spanning
    two brokers on the same underlying -- e.g. a stock held via Zerodha
    with a call written via Dhan -- still groups and classifies as one
    Covered Call. Deliberately builds its own lightweight leg dicts
    rather than reusing build_trade_legs (My Trades/Analyse Trade's own
    loader) -- that also fetches live prices, which classify_trade_type
    (leg_type/option_type/qty only) has no use for."""
    holding_legs = [
        {"leg_type": "Holding", "symbol": h.symbol, "raw_name": h.raw_name, "broker": h.broker, "qty": h.qty, "option_type": None}
        for h in portfolio_repo.list_holdings(client, user_id)
        if h.portfolio_name == portfolio_name
    ]
    position_legs = [
        {"leg_type": "Position", "symbol": p.symbol, "raw_name": p.raw_name, "broker": p.broker, "qty": p.qty, "option_type": p.option_type}
        for p in portfolio_repo.list_positions(client, user_id)
        if p.portfolio_name == portfolio_name
    ]
    if not holding_legs and not position_legs:
        return

    overrides = {
        (g.broker, g.raw_name): g.trade_id
        for g in portfolio_repo.list_trade_groups(client, user_id)
        if g.portfolio_name == portfolio_name
    }
    assigned = portfolio_service.assign_trade_ids(holding_legs + position_legs, overrides)
    legs_by_trade_id: dict[str, list[dict]] = {}
    for leg in assigned:
        legs_by_trade_id.setdefault(leg["trade_id"], []).append(leg)

    already_classified = {
        m.trade_id for m in portfolio_repo.list_trade_meta(client, user_id) if m.portfolio_name == portfolio_name
    }
    for trade_id, legs in legs_by_trade_id.items():
        if trade_id in already_classified:
            continue
        detected_type = portfolio_service.classify_trade_type(legs)
        if detected_type is not None:
            portfolio_repo.set_trade_meta(
                client, user_id, portfolio_name, trade_id, underlying_label=None, trade_type=detected_type
            )


def _sync_dhan(*, client, user_id: str, connection: BrokerConnection) -> None:
    """Pulls holdings + positions straight from Dhan's API into this
    account's one resolved portfolio (get_or_default_portfolio_name) --
    same repo calls the old CSV upload path used, so the rendered My
    Holdings/My Positions tables look identical regardless of source.
    Called both from Settings' "Save & Sync"/"Update credentials" forms
    and from sync_broker_portfolio (the Portfolio Refresh button on My
    Trades/My Holdings/My Positions/My CSP)."""
    started_at = datetime.now(timezone.utc)
    portfolio_name = portfolio_repo.get_or_default_portfolio_name(client, user_id)
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
        # token -- degrade to no LTP rather than discarding a sync that
        # mostly worked.
        ltp_by_security_id = {}

    holdings = portfolio_service.dhan_holdings_from_api(holding_rows)
    positions = portfolio_service.dhan_positions_from_api(position_rows, ltp_by_security_id)
    option_chains = _fetch_fallback_option_chains(client, positions)
    positions = portfolio_service.apply_fallback_option_ltp(positions, option_chains)
    holding_records = portfolio_service.holdings_to_records(user_id, portfolio_name, "Dhan", holdings)
    position_records = portfolio_service.positions_to_records(user_id, portfolio_name, "Dhan", positions)
    portfolio_repo.replace_broker_holdings(client, user_id, portfolio_name, "Dhan", holding_records)
    portfolio_repo.replace_broker_positions(client, user_id, portfolio_name, "Dhan", position_records)
    _default_new_position_trade_dates(client=client, user_id=user_id, portfolio_name=portfolio_name, broker="Dhan", positions=positions)
    _auto_classify_new_trades(client=client, user_id=user_id, portfolio_name=portfolio_name)
    _log_portfolio_sync(client, "Dhan", started_at)
    _bump_cache_bust()
    st.success(
        f"Synced {len(holding_records)} holding(s) and {len(position_records)} position(s) "
        f"from Dhan to \"{portfolio_name}\"."
    )
    st.rerun()


def _sync_zerodha(*, client, user_id: str, connection: BrokerConnection) -> None:
    """Pulls holdings + positions straight from Zerodha's Kite Connect
    API into this account's one resolved portfolio. Unlike Dhan, no
    fallback-LTP step is needed -- Kite's responses already include
    last_price directly. Called both from Settings' "Log in to Zerodha"
    redirect handler and from sync_broker_portfolio (the Portfolio
    Refresh button on My Trades/My Holdings/My Positions/My CSP)."""
    if not connection.access_token or not connection.api_secret:
        st.error('Not logged in yet -- click "Log in to Zerodha" below first.')
        return
    started_at = datetime.now(timezone.utc)
    portfolio_name = portfolio_repo.get_or_default_portfolio_name(client, user_id)
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
    holding_records = portfolio_service.holdings_to_records(user_id, portfolio_name, "Zerodha", holdings)
    position_records = portfolio_service.positions_to_records(user_id, portfolio_name, "Zerodha", positions)
    portfolio_repo.replace_broker_holdings(client, user_id, portfolio_name, "Zerodha", holding_records)
    portfolio_repo.replace_broker_positions(client, user_id, portfolio_name, "Zerodha", position_records)
    _default_new_position_trade_dates(client=client, user_id=user_id, portfolio_name=portfolio_name, broker="Zerodha", positions=positions)
    _auto_classify_new_trades(client=client, user_id=user_id, portfolio_name=portfolio_name)
    _log_portfolio_sync(client, "Zerodha", started_at)
    _bump_cache_bust()
    st.success(
        f"Synced {len(holding_records)} holding(s) and {len(position_records)} position(s) "
        f"from Zerodha to \"{portfolio_name}\"."
    )
    st.rerun()


def _log_portfolio_sync(client, broker: str, started_at: datetime) -> None:
    """Logs a provider_fetch_log row for a completed broker portfolio
    sync -- the same table/pattern every other refresh button already
    writes to, so "Last portfolio refresh" (render_portfolio_refresh_button
    in src/utils/refresh_bar.py) has something to read. `provider_name` is
    the lowercased broker, matching the Data Provider setting's own values
    ("dhan"/"zerodha")."""
    fetch_log_repo.log_fetch(
        client,
        ProviderFetchLog(
            provider_name=broker.lower(),
            fetch_type=FetchType.PORTFOLIO_SYNC,
            status=FetchStatus.SUCCESS,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
        ),
    )


def sync_broker_portfolio(*, client, user_id: str, data_provider: str) -> None:
    """Entry point for the Portfolio Refresh button (My Trades/My
    Holdings/My Positions/My CSP -- src/utils/refresh_bar.py) -- resolves
    this account's connected broker from its Data Provider setting and
    runs the same sync Settings' "Save & Sync"/"Update credentials" forms
    trigger. Shows an error directing back to Settings if the account has
    no (or an incomplete) broker connection yet, rather than crashing --
    a data_provider of "dhan"/"zerodha" doesn't guarantee credentials were
    ever actually saved."""
    broker = {"dhan": "Dhan", "zerodha": "Zerodha"}.get(data_provider)
    if broker is None:
        return
    connection = portfolio_repo.get_broker_connection(client, user_id, broker)
    if connection is None:
        st.error(f"No connected {broker} account yet -- connect one in Settings' Data Provider section.")
        return
    if broker == "Dhan":
        _sync_dhan(client=client, user_id=user_id, connection=connection)
    else:
        _sync_zerodha(client=client, user_id=user_id, connection=connection)


def _render_dhan_connect_section(*, client, user_id: str) -> None:
    try:
        connection = portfolio_repo.get_broker_connection(client, user_id, "Dhan")
    except APIError:
        st.info(
            "Connecting a Dhan account isn't set up yet. Apply migrations "
            "`supabase/migrations/0017_broker_connections.sql` through "
            "`supabase/migrations/0029_broker_connections_account_wide.sql` "
            "(in order) in the Supabase SQL editor, then reload this page."
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
        with st.form("dhan_connect_form"):
            new_client_id = st.text_input("Dhan Client ID")
            new_token = st.text_input("Dhan Access Token", type="password")
            submitted = st.form_submit_button("Save & Sync")
        if submitted:
            if not new_client_id.strip() or not new_token.strip():
                st.error("Both Client ID and Access Token are required.")
                return
            new_connection = BrokerConnection(
                user_id=user_id,
                broker="Dhan",
                client_id=new_client_id.strip(),
                access_token=new_token.strip(),
                token_saved_at=datetime.now(timezone.utc),
            )
            portfolio_repo.upsert_broker_connection(client, new_connection)
            _bump_cache_bust()
            _sync_dhan(client=client, user_id=user_id, connection=new_connection)
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

    st.caption('Use the "Portfolio Refresh" button on My Trades/My Holdings/My Positions/My CSP to re-sync.')
    if st.button("Disconnect", key="dhan_disconnect"):
        portfolio_repo.delete_broker_connection(client, user_id, "Dhan")
        _bump_cache_bust()
        st.success("Disconnected. Previously synced holdings/positions are unaffected.")
        st.rerun()

    with st.expander("Update credentials"):
        with st.form("dhan_update_form"):
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
                broker="Dhan",
                client_id=updated_client_id.strip(),
                access_token=updated_token.strip(),
                token_saved_at=datetime.now(timezone.utc),
            )
            portfolio_repo.upsert_broker_connection(client, updated_connection)
            _bump_cache_bust()
            _sync_dhan(client=client, user_id=user_id, connection=updated_connection)


def _render_dhan_instrument_master_refresh(*, client) -> None:
    """"Refresh Instrument Master - Dhan" -- the only thing that
    downloads and persists dhan_equity_instruments/dhan_fo_instruments
    (migration 0035) now that Stock & Option Data Refresh no longer does
    so implicitly on a stale-cache click (see dhan_provider.py's
    _load_instrument_master/_load_fo_instrument_master docstrings for
    why that was decoupled). Shown regardless of whether a Dhan account
    is actually connected yet -- the instrument master is public Dhan
    reference data, unrelated to any one account's own credentials, so
    there's nothing to connect first."""
    entry = fetch_log_repo.get_last_successful_fetch(client, FetchType.DHAN_INSTRUMENT_MASTER, "fo")
    when = format_ist(entry.finished_at) if entry else "never"
    st.caption(f"Last instrument master refresh: {when}")
    st.caption(
        "Resolves every symbol/contract Stock & Option Data Refresh needs to quote (~211,742-row Dhan CSV, "
        "so it's a slower, deliberately separate step -- Stock & Option Data Refresh only ever reads "
        "whatever was fetched here, however old, rather than re-downloading this itself)."
    )
    if st.button("Refresh Instrument Master - Dhan", key="dhan_instrument_master_refresh_btn"):
        with st.spinner("Downloading Dhan's instrument master..."):
            try:
                summary = refresh_dhan_instrument_master(client)
            except ProviderError as exc:
                st.error(f"Failed to refresh Dhan instrument master: {exc}")
            else:
                st.success(
                    f"Refreshed -- {summary['equity_count']} equity/ETF instruments, "
                    f"{summary['fo_count']} futures/option contracts."
                )


def _render_dhan_trade_history_sync(*, client, user_id: str) -> None:
    """"Sync Trade History from Dhan" -- pulls GET /v2/trades into
    portfolio_trade_fills (migration 0038), feeding the Trade History
    page's realized-P&L/trade-journal views. A separate, explicit sync
    from _sync_dhan/sync_broker_portfolio's holdings/positions snapshot --
    same reasoning as _render_dhan_instrument_master_refresh's own
    decoupling: pulling a potentially long date range of history is a
    different cost/cadence than the fast current-snapshot sync, and a
    first-time backfill is a one-off, occasional action. Requires an
    already-connected Dhan account (_render_dhan_connect_section, shown
    just above this in render_data_provider_section) -- there's no
    trade-history credential separate from the one used for
    holdings/positions."""
    try:
        connection = portfolio_repo.get_broker_connection(client, user_id, "Dhan")
    except APIError:
        return  # _render_dhan_connect_section above already surfaced the same migration-not-applied message
    if connection is None:
        return  # nothing to sync until a Dhan account is connected above

    portfolio_name = portfolio_repo.get_or_default_portfolio_name(client, user_id)
    latest = portfolio_repo.latest_trade_fill_date(client, user_id, portfolio_name, "Dhan")
    st.caption(f"Last trade synced: {latest.isoformat() if latest else 'never'}")

    if latest is None:
        from_date = st.date_input(
            "Sync from",
            value=date.today() - timedelta(days=365),
            key="dhan_trade_history_from_date",
            help="First sync only -- how far back to backfill. Later syncs continue automatically from "
            "wherever the last one left off.",
        )
    else:
        from_date = latest + timedelta(days=1)

    if st.button("Sync Trade History from Dhan", key="dhan_trade_history_sync_btn"):
        started_at = datetime.now(timezone.utc)
        to_date_value = date.today()
        if from_date > to_date_value:
            st.info("Already up to date.")
            return
        provider = DhanProvider(client_id=connection.client_id, access_token=connection.access_token)
        with st.spinner(f"Fetching trades from {from_date} to {to_date_value}..."):
            try:
                raw_rows = provider.get_trade_history(from_date, to_date_value)
                fills = portfolio_service.dhan_trade_fills_from_api(raw_rows)
            except DhanAuthError:
                st.error(
                    "Your Dhan access token was rejected -- it's likely expired (Dhan tokens last ~24 hours). "
                    "Generate a new one on web.dhan.co and paste it above."
                )
                return
            except ProviderError as exc:
                st.error(f"Could not sync trade history from Dhan: {exc}")
                return
        records = portfolio_service.trade_fills_to_records(user_id, portfolio_name, "Dhan", fills)
        portfolio_repo.upsert_trade_fills(client, user_id, portfolio_name, "Dhan", records)
        fetch_log_repo.log_fetch(
            client,
            ProviderFetchLog(
                provider_name="dhan",
                fetch_type=FetchType.TRADE_HISTORY_SYNC,
                status=FetchStatus.SUCCESS,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
            ),
        )
        _bump_cache_bust()
        st.success(f"Synced {len(records)} trade fill(s) from {from_date} to {to_date_value}.")
        st.rerun()


def _render_zerodha_connect_section(*, client, user_id: str) -> None:
    try:
        connection = portfolio_repo.get_broker_connection(client, user_id, "Zerodha")
    except APIError:
        st.info(
            "Connecting a Zerodha account isn't set up yet. Apply migrations "
            "`supabase/migrations/0022_broker_connections_api_secret.sql` through "
            "`supabase/migrations/0029_broker_connections_account_wide.sql` "
            "(in order) in the Supabase SQL editor, then reload this page."
        )
        return

    if connection is None or not connection.api_secret:
        st.caption(
            "Register a Kite Connect app at developers.kite.trade (paid subscription required) and set its "
            "Redirect URL to this Settings page's own URL. This app only ever reads your Holdings/Positions "
            "with the resulting session -- it never places or modifies orders -- but the API Secret/access "
            "token are stored as entered, protected the same way as everything else here (row-level "
            "security), not separately encrypted."
        )
        with st.form("zerodha_connect_form"):
            new_api_key = st.text_input("Kite Connect API Key")
            new_api_secret = st.text_input("Kite Connect API Secret", type="password")
            submitted = st.form_submit_button("Save")
        if submitted:
            if not new_api_key.strip() or not new_api_secret.strip():
                st.error("Both API Key and API Secret are required.")
                return
            new_connection = BrokerConnection(
                user_id=user_id, broker="Zerodha", client_id=new_api_key.strip(), api_secret=new_api_secret.strip()
            )
            portfolio_repo.upsert_broker_connection(client, new_connection)
            _bump_cache_bust()
            st.rerun()
        return

    if connection.access_token and _zerodha_token_is_fresh(connection.token_saved_at):
        masked_key = f"...{connection.client_id[-4:]}" if len(connection.client_id) > 4 else connection.client_id
        st.caption(
            f"Connected -- API Key {masked_key}, session started {_relative_age(_hours_since(connection.token_saved_at))}."
        )
        st.caption('Use the "Portfolio Refresh" button on My Trades/My Holdings/My Positions/My CSP to re-sync.')
        if st.button("Disconnect", key="zerodha_disconnect"):
            portfolio_repo.delete_broker_connection(client, user_id, "Zerodha")
            _bump_cache_bust()
            st.success("Disconnected. Previously synced holdings/positions are unaffected.")
            st.rerun()
    else:
        if connection.access_token is not None:
            st.warning(
                "Your Zerodha session has likely expired -- Kite Connect tokens expire daily "
                "(around 6am IST). Log in again below."
            )
        provider = ZerodhaProvider(api_key=connection.client_id, api_secret=connection.api_secret)
        st.link_button("Log in to Zerodha", provider.login_url())
        st.caption("Opens in a new tab. If that tab asks you to sign in here first, do so -- the connection will still complete.")

    with st.expander("Update API Key / Secret"):
        st.caption(
            "Changing these doesn't clear an existing session -- \"Portfolio Refresh\" will simply fail and "
            "prompt a fresh login if it's no longer valid under the new credentials."
        )
        with st.form("zerodha_update_form"):
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
                broker="Zerodha",
                client_id=updated_api_key.strip(),
                api_secret=updated_api_secret.strip() or connection.api_secret,
            )
            portfolio_repo.upsert_broker_connection(client, updated_connection)
            _bump_cache_bust()
            st.rerun()


def _render_zerodha_redirect_handler(*, client, user_id: str) -> None:
    """Kite Connect always redirects back to this Kite Connect app's own
    configured Redirect URL with `request_token` in the query string on a
    successful login -- **that Redirect URL must be updated to point at
    this Settings page** (it used to point at My Broker) on
    developers.kite.trade, a manual step outside this codebase. Unlike
    the old per-portfolio flow, there's no portfolio picker needed here
    anymore -- the account has exactly one Zerodha connection
    (account-wide since migration 0029), so landing here with a
    request_token unambiguously completes *that* connection's login and
    immediately syncs, no extra confirmation click required."""
    if st.query_params.get("status") == "error":
        st.error("Zerodha login was cancelled or failed -- click \"Log in to Zerodha\" again to retry.")
        st.query_params.clear()
        return
    if "request_token" not in st.query_params:
        return

    request_token = st.query_params["request_token"]
    st.query_params.clear()
    pending_connection = portfolio_repo.get_broker_connection(client, user_id, "Zerodha")
    if pending_connection is None or not pending_connection.api_secret:
        st.error(
            'No saved Zerodha API Key/Secret found -- enter them under "Zerodha" below, then click '
            '"Log in to Zerodha" again.'
        )
        return

    provider = ZerodhaProvider(api_key=pending_connection.client_id, api_secret=pending_connection.api_secret)
    try:
        access_token = provider.generate_session(request_token)
    except ProviderError as exc:
        st.error(f"Could not complete Zerodha login: {exc}")
        return

    updated_connection = BrokerConnection(
        user_id=user_id,
        broker="Zerodha",
        client_id=pending_connection.client_id,
        api_secret=pending_connection.api_secret,
        access_token=access_token,
        token_saved_at=datetime.now(timezone.utc),
    )
    portfolio_repo.upsert_broker_connection(client, updated_connection)
    _bump_cache_bust()
    _sync_zerodha(client=client, user_id=user_id, connection=updated_connection)


def render_data_provider_section(*, client, user_id: str, current: UserSettings) -> None:
    """Settings' "Data Provider" section -- the single entry point
    pages/4_Settings.py calls. Handles a returning Zerodha login redirect
    first (if any), then the provider picker, then whichever
    connect/sync UI (if any) that choice needs."""
    ensure_cache_bust()
    _render_zerodha_redirect_handler(client=client, user_id=user_id)

    st.divider()
    st.subheader("Data Provider")
    st.caption(
        "Which live source prices your stock LTP everywhere it's shown (Dashboard, Stock Detail, and your "
        "portfolio pages). Fundamentals (PEG, Dividend Yield) and the full options chain always come from "
        "YFinance / NSE+BSE Bhavcopy regardless of this choice -- neither Dhan nor Zerodha's API exposes "
        "that data."
    )
    keys = list(_PROVIDER_LABELS.keys())
    selected_label = st.selectbox(
        "Provider", list(_PROVIDER_LABELS.values()), index=keys.index(current.data_provider)
    )
    selected = keys[list(_PROVIDER_LABELS.values()).index(selected_label)]
    if selected != current.data_provider:
        settings_repo.upsert_user_settings(client, current.model_copy(update={"data_provider": selected}))
        st.rerun()

    if selected == "dhan":
        _render_dhan_connect_section(client=client, user_id=user_id)
        st.divider()
        _render_dhan_instrument_master_refresh(client=client)
        st.divider()
        _render_dhan_trade_history_sync(client=client, user_id=user_id)
    elif selected == "zerodha":
        _render_zerodha_connect_section(client=client, user_id=user_id)
    else:
        st.caption(
            "Covered by the daily scheduled refresh (8pm IST) and the \"Market Data Refresh\" button above -- "
            "nothing else to connect here."
        )
