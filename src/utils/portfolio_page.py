"""Cross-page helpers for the Portfolio feature's pages -- My Trades (7),
My Holdings (8), My Positions (9), My CSP (11), and Analyse Trade (10,
hidden from the sidebar). Broker connect/sync UI lives in Settings' "Data
Provider" section (src/utils/data_provider_settings.py) now, not a
dedicated page -- these loaders are still shared across every page that
reads the resulting holdings/positions/broker-live prices. Every page needs the same
cached data loaders (e.g. My Trades and Analyse Trade both need holdings +
positions + trade groups + trade meta to rebuild the same leg/Trade view;
My Holdings and My Positions each need a subset), so they're extracted
here instead of duplicated -- same "one shared piece, page-specific body"
split src/utils/refresh_bar.py already uses across all pages. Because
these are plain module-level `@st.cache_data` functions (not redefined per
page), a cache hit in one page is also a cache hit in another -- e.g.
switching from My Holdings to My Trades doesn't re-fetch holdings that are
still fresh.
"""
from __future__ import annotations

import re
from datetime import date

import streamlit as st

from src.data_providers.base import ProviderError
from src.data_providers.dhan_provider import DhanAuthError, DhanProvider
from src.data_providers.zerodha_provider import ZerodhaAuthError, ZerodhaProvider
from src.repositories import companies_repo, fo_repo, portfolio_repo, snapshot_repo
from src.services import portfolio_service


def ensure_cache_bust() -> None:
    """Initializes the portfolio_cache_bust session-state counter every
    loader below is keyed on -- call once near the top of every portfolio
    page. Any save/sync/delete/merge/split action increments this counter
    and calls st.cache_data.clear() before rerunning, which is what makes
    "fresh data after an edit" work across all five pages at once."""
    if "portfolio_cache_bust" not in st.session_state:
        st.session_state["portfolio_cache_bust"] = 0


@st.cache_data(ttl=60, show_spinner=False)
def load_holdings(_client, _user_id: str, _cache_bust: int):
    return portfolio_repo.list_holdings(_client, _user_id)


@st.cache_data(ttl=60, show_spinner=False)
def load_trade_fills(_client, _user_id: str, _cache_bust: int):
    """Trade History page (14) only -- shares the same _cache_bust counter
    as every other loader here, so a fresh "Sync Trade History from Dhan"
    click (data_provider_settings.py's _bump_cache_bust) shows up
    immediately rather than waiting out this 60s TTL."""
    return portfolio_repo.list_trade_fills(_client, _user_id)


def _fo_contract_key(p) -> tuple[str, date, float, str] | None:
    """This position leg's (symbol, expiry_date, strike_price,
    option_type) natural key, matching user_live_prices' F&O rows
    (migration 0032) -- None for a future/option that isn't fully
    resolved (a still-undecoded leg with option_type/strike_price only
    partially set) or a plain equity/ETF holding (no expiry_date at
    all)."""
    if not p.symbol or p.expiry_date is None:
        return None
    if p.option_type is not None and p.strike_price is not None:
        return (p.symbol, p.expiry_date, float(p.strike_price), p.option_type.value)
    if p.option_type is None and p.strike_price is None:
        return (p.symbol, p.expiry_date, 0.0, "FUT")
    return None


def _apply_live_fo_prices(client, user_id: str, positions: list) -> list:
    """Overrides each F&O position leg's `ltp` with this account's own
    cached live Dhan quote (user_live_prices, migration 0032) when one
    exists -- same live-beats-EOD preference load_live_broker_prices
    already applies for equity LTP, just per-contract instead of
    per-symbol. Also clears `ltp_as_of` on a live hit -- a live price is
    never "stale", same rule the equity override already follows on
    Dashboard/Stock Detail. Doesn't need to branch on the account's Data
    Provider setting: only Dhan writes F&O rows here today (Zerodha has
    no F&O instrument resolver yet), so a lookup simply returns nothing
    for every contract when it hasn't."""
    keys = [key for p in positions if (key := _fo_contract_key(p)) is not None]
    if not keys:
        return positions
    live = snapshot_repo.get_user_live_fo_prices(client, user_id, keys)
    if not live:
        return positions
    return [
        p.model_copy(update={"ltp": live[key], "ltp_as_of": None})
        if (key := _fo_contract_key(p)) in live
        else p
        for p in positions
    ]


@st.cache_data(ttl=60, show_spinner=False)
def load_positions(_client, _user_id: str, _cache_bust: int):
    positions = portfolio_repo.list_positions(_client, _user_id)
    return _apply_live_fo_prices(_client, _user_id, positions)


@st.cache_data(ttl=60, show_spinner=False)
def load_trade_groups(_client, _user_id: str, _cache_bust: int):
    return portfolio_repo.list_trade_groups(_client, _user_id)


@st.cache_data(ttl=60, show_spinner=False)
def load_trade_meta(_client, _user_id: str, _cache_bust: int):
    return portfolio_repo.list_trade_meta(_client, _user_id)


@st.cache_data(ttl=60, show_spinner=False)
def load_position_meta(_client, _user_id: str, _cache_bust: int):
    return portfolio_repo.list_position_meta(_client, _user_id)


@st.cache_data(ttl=300, show_spinner=False)
def load_all_companies(_client, _cache_bust: int):
    return companies_repo.list_all_companies(_client)


@st.cache_data(ttl=60, show_spinner=False)
def load_latest_prices(_client, symbols: tuple[str, ...], _cache_bust: int):
    return snapshot_repo.get_latest_prices(_client, list(symbols))


@st.cache_data(ttl=60, show_spinner=False)
def load_returns_and_pe(_client, symbols: tuple[str, ...], _cache_bust: int):
    return snapshot_repo.get_latest_returns_and_pe(_client, list(symbols))


@st.cache_data(ttl=60, show_spinner=False)
def load_live_dhan_prices(
    client_id: str, access_token: str, symbols: tuple[str, ...], _cache_bust: int, _client=None
) -> dict[str, float]:
    """Live NSE equity LTPs straight from Dhan's marketfeed
    (DhanProvider.get_quotes), keyed by symbol. Only meaningful for an
    account with Dhan connected (Settings' "Data Provider" section),
    whose already-saved access_token this reuses -- no separate app-wide
    Dhan credentials required. Returns {}
    on any provider error (expired token, a symbol Dhan's instrument
    master doesn't resolve, network) -- see load_live_broker_prices below
    for how the caller falls back when this comes back empty. `_client`
    (leading underscore -- excluded from this function's own cache key,
    same convention load_latest_prices' `_client` above uses) is passed
    through to DhanProvider so its instrument-master resolution can hit
    the shared dhan_equity_instruments cache (migration 0035) instead of
    downloading Dhan's full instrument master on every cache miss."""
    if not symbols:
        return {}
    try:
        quotes = DhanProvider(client_id=client_id, access_token=access_token, supabase_client=_client).get_quotes(
            list(symbols)
        )
    except (DhanAuthError, ProviderError):
        return {}
    return {symbol: quote.latest_price for symbol, quote in quotes.items()}


@st.cache_data(ttl=60, show_spinner=False)
def load_live_zerodha_prices(
    api_key: str, api_secret: str, access_token: str, symbols: tuple[str, ...], _cache_bust: int
) -> dict[str, float]:
    """Live NSE equity LTPs straight from Kite Connect's /quote/ltp
    (ZerodhaProvider.get_ltp), keyed by symbol -- Zerodha's counterpart to
    load_live_dhan_prices above. Only meaningful for an account with
    Zerodha connected whose Kite session hasn't expired yet (Settings'
    "Data Provider" section); reuses the already-saved
    api_key/api_secret/access_token, no separate app-wide credentials.
    Returns {} on any provider error (expired daily session,
    an unrecognized symbol, network) -- see load_live_broker_prices below
    for how the caller falls back when this comes back empty."""
    if not symbols:
        return {}
    try:
        return ZerodhaProvider(api_key=api_key, api_secret=api_secret, access_token=access_token).get_ltp(list(symbols))
    except (ZerodhaAuthError, ProviderError):
        return {}


def load_live_broker_prices(_client, user_id: str, symbols: tuple[str, ...], cache_bust: int) -> dict[str, float]:
    """Live equity LTPs from whichever broker(s) this account has
    actually connected (Dhan and/or Zerodha -- Settings' "Data Provider"
    section, src/utils/data_provider_settings.py) -- fresher than
    load_latest_prices' daily_screener_snapshots value, which only
    reflects whatever provider/timing the last "Market Data Refresh"
    click used for a yfinance_bhavcopy account. Account-wide since
    migration 0029 (broker_connections no longer keyed by
    portfolio_name), so this no longer takes a portfolio_name -- one
    connected Dhan/Zerodha account covers every portfolio this account
    has. Checks both broker connections and merges their live quotes; if
    both are connected and both quote the same symbol, Zerodha's value
    wins simply because it's applied last -- an arbitrary tie-break,
    since either is equally "live". A symbol neither broker can quote (or
    no broker connected at all, or a connected broker whose
    session/token has expired) is simply absent from the result, leaving
    the caller's daily_screener_snapshots value as the fallback for it --
    not a special case here, just an empty/partial dict. Not itself
    `@st.cache_data`-wrapped (the two loaders it calls already are) so
    that a fresh get_broker_connection lookup always sees the latest
    saved connection state (e.g. right after a Portfolio Refresh bumps
    portfolio_cache_bust)."""
    live: dict[str, float] = {}
    dhan_connection = portfolio_repo.get_broker_connection(_client, user_id, "Dhan")
    if dhan_connection is not None and dhan_connection.access_token:
        live.update(
            load_live_dhan_prices(
                dhan_connection.client_id, dhan_connection.access_token, symbols, cache_bust, _client
            )
        )
    zerodha_connection = portfolio_repo.get_broker_connection(_client, user_id, "Zerodha")
    if zerodha_connection is not None and zerodha_connection.access_token and zerodha_connection.api_secret:
        live.update(
            load_live_zerodha_prices(
                zerodha_connection.client_id,
                zerodha_connection.api_secret,
                zerodha_connection.access_token,
                symbols,
                cache_bust,
            )
        )
    return live


@st.cache_data(ttl=60, show_spinner=False)
def load_option_expiries(_client, symbols: tuple[str, ...], _cache_bust: int) -> dict[str, list[str]]:
    return {symbol: [d.isoformat() for d in fo_repo.list_option_expiries(_client, symbol)] for symbol in symbols}


@st.cache_data(ttl=60, show_spinner=False)
def load_option_chain(_client, symbol: str, expiry_iso: str, _cache_bust: int) -> list[dict]:
    return fo_repo.get_option_chain(_client, symbol, date.fromisoformat(expiry_iso))


def build_trade_legs(
    client, user_id: str, cache_bust: int, holdings_for_portfolio: list, positions_for_portfolio: list
) -> list[dict]:
    """Unmerged per-broker leg list for one portfolio, ready for
    portfolio_service.group_into_trades -- shared by My Trades (the list
    view) and Analyse Trade (one trade's detail view) so bucket/label
    logic never drifts between the two. Unlike My Holdings' merge_holdings
    (which combines a symbol's rows across brokers into one row), Trades
    need per-leg identity: that's the same natural key (broker, raw_name)
    portfolio_trade_groups overrides are keyed by, and what a merge/split
    in Analyse Trade actually reassigns. No portfolio_name parameter --
    load_live_broker_prices below is account-wide (migration 0029), not
    scoped per portfolio; the caller still pre-filters
    holdings_for_portfolio/positions_for_portfolio to one portfolio_name
    before calling this."""
    holding_dicts = [
        {"raw_name": h.raw_name, "symbol": h.symbol, "qty": h.qty, "avg_price": h.avg_price, "investment": h.investment}
        for h in holdings_for_portfolio
    ]
    holding_symbols = tuple(sorted({d["symbol"] for d in holding_dicts if d["symbol"]}))
    ltp_by_symbol = load_latest_prices(client, holding_symbols, cache_bust)
    # Same broker-live-first, daily_screener_snapshots-fallback preference
    # as My CSP's LTP Underlying (see load_live_broker_prices) -- a
    # Holding leg's own LTP/Cur Val/P&L here feeds both My Trades' Total
    # P&L and Analyse Trade's legs table.
    live_ltp_by_symbol = load_live_broker_prices(client, user_id, holding_symbols, cache_bust)
    ltp_by_symbol = {**ltp_by_symbol, **live_ltp_by_symbol}
    computed_holding_rows, _totals = portfolio_service.compute_portfolio_view(holding_dicts, ltp_by_symbol)
    holding_legs = [
        {**row, "broker": h.broker, "leg_type": "Holding"}
        for h, row in zip(holdings_for_portfolio, computed_holding_rows)
    ]

    position_dicts = [
        {
            "raw_name": p.raw_name,
            "broker": p.broker,
            "symbol": p.symbol,
            "expiry_date": p.expiry_date,
            "strike_price": p.strike_price,
            "option_type": p.option_type,
            "qty": p.qty,
            "avg_price": p.avg_price,
            "ltp": p.ltp,
            "ltp_as_of": p.ltp_as_of,
        }
        for p in positions_for_portfolio
    ]
    computed_position_rows = portfolio_service.compute_positions_view(position_dicts)
    position_legs = [{**row, "leg_type": "Position"} for row in computed_position_rows]

    return holding_legs + position_legs


def fmt_qty(value: float) -> str:
    if value == int(value):
        return f"{int(value):,}"
    return f"{value:,.2f}"


def slug(text: str) -> str:
    """CSS-class-safe form of an arbitrary portfolio name, for any
    per-portfolio-scoped widget key."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "x"
