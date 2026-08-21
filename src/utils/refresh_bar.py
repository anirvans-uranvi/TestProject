"""Five independent, targeted refresh buttons -- replaces the old single
"🔄 Market Data Refresh" button that fired everything at once regardless
of what a page actually needed:

- **Fundamental Data Refresh** / **Bhavcopy Refresh** (NSE + BSE) --
  Settings only, `render_fundamental_and_bhavcopy_refresh`.
- **Stock Data Refresh** (YFinance/Bhavcopy accounts) / **Stock & Option
  Data Refresh** (Dhan/Zerodha accounts) -- every page except Settings,
  `render_stock_refresh_button` (shows exactly one of the two, by Data
  Provider setting).
- **Portfolio Refresh** -- My Trades/My Holdings/My Positions/My CSP
  only, Dhan/Zerodha accounts only, `render_portfolio_refresh_button`.

A click always calls `st.cache_data.clear()` -- the *entire* app-wide
cache, not just the current page's -- before `st.rerun()`-ing, so every
page's own `@st.cache_data` loaders re-fetch fresh data regardless of
which page the click happened on. Each button fetches independently (no
shared cooldown/thread pool across buttons); "Bhavcopy Refresh" still
fires its NSE + BSE legs concurrently via a `ThreadPoolExecutor`, since
that's one button covering two independent exchanges.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import streamlit as st

from src.data_providers.base import ProviderError
from src.data_providers.dhan_provider import DhanAuthError, DhanProvider
from src.models.enums import CompanyType
from src.repositories import companies_repo, fetch_log_repo, fo_repo, portfolio_repo, snapshot_repo
from src.services import edge_refresh
from src.utils.data_provider_settings import sync_broker_portfolio
from src.utils.portfolio_page import load_live_zerodha_prices
from src.utils.timezones import format_ist

_NSE_FO_PROVIDER = "fo_edge_nse"
_BSE_FO_PROVIDER = "fo_edge_bse"
_BROKER_BY_PROVIDER = {"dhan": "Dhan", "zerodha": "Zerodha"}


def _last_fetch_caption(client, label: str, fetch_type: str | list[str], provider_name: str | None = None) -> str:
    entry = fetch_log_repo.get_last_successful_fetch(client, fetch_type, provider_name)
    when = format_ist(entry.finished_at) if entry else "never"
    return f"{label}: {when}"


def _universe_breakdown(client) -> str:
    """Same "(X stocks, Y ETFs/funds)" breakdown the stock-refresh message
    has always shown (see git history of pages/1_Dashboard.py)."""
    companies = companies_repo.list_all_companies(client)
    stock_count = sum(1 for c in companies if c.company_type == CompanyType.EQUITY)
    etf_count = sum(1 for c in companies if c.company_type == CompanyType.ETF)
    return f" ({stock_count} stocks, {etf_count} ETFs/funds)" if etf_count else ""


def _dhan_fo_universe(client, user_id: str) -> list[tuple[str, date, float, str]]:
    """Every futures/option contract a Dhan-provider account's live-price
    refresh should quote: this account's own open portfolio F&O
    positions (any broker, any portfolio -- a position is a position
    regardless of which broker it was synced from), plus the Dashboard's
    cached 5% CSP / 5% CC legs (fo_repo.get_dashboard_fo_metrics) -- the
    only options the Screener/Dashboard itself actually uses, not the
    full chain for every strike/expiry. Returns (symbol, expiry_date,
    strike_price, option_type) tuples -- option_type='FUT' for a futures
    position (strike_price is meaningless there, sentinel 0.0) -- see
    DhanProvider.get_fo_quotes."""
    contracts: set[tuple[str, date, float, str]] = set()
    for p in portfolio_repo.list_positions(client, user_id):
        if not p.symbol or p.expiry_date is None:
            continue
        if p.option_type is not None and p.strike_price is not None:
            contracts.add((p.symbol, p.expiry_date, float(p.strike_price), p.option_type.value))
        elif p.option_type is None and p.strike_price is None:
            contracts.add((p.symbol, p.expiry_date, 0.0, "FUT"))
    for row in fo_repo.get_dashboard_fo_metrics(client):
        symbol = row.get("symbol")
        expiry_date = row.get("expiry_date")
        if not symbol or not expiry_date:
            continue
        if isinstance(expiry_date, str):
            expiry_date = date.fromisoformat(expiry_date)
        if row.get("csp_strike") is not None:
            contracts.add((symbol, expiry_date, float(row["csp_strike"]), "PE"))
        if row.get("cc_strike") is not None:
            contracts.add((symbol, expiry_date, float(row["cc_strike"]), "CE"))
    return list(contracts)


def _refresh_dhan_equity_leg(client, user_id: str, connection, symbols: tuple[str, ...]) -> dict:
    """The equity/ETF half of a Dhan live-price refresh -- resolves and
    quotes `symbols`, then caches the result. Split out from
    _refresh_user_live_prices so it can run concurrently with
    _refresh_dhan_fo_leg (both are independent blocking network calls).
    Returns {"error": ...} on failure, else {"prices": {...}}."""
    # Calls DhanProvider directly rather than going through
    # load_live_dhan_prices (which swallows DhanAuthError/ProviderError
    # into a silent {}) -- confirmed live: that made a real failure (an
    # expired token, or a Dhan account missing the separate paid "Data
    # APIs" subscription market quotes require -- see
    # portfolio_service.apply_fallback_option_ltp's docstring for the
    # same 401 already seen on the positions-sync path) indistinguishable
    # from "nothing to quote", showing a misleading green "0 of N"
    # instead of a real error.
    try:
        quotes = DhanProvider(
            client_id=connection.client_id, access_token=connection.access_token, supabase_client=client
        ).get_quotes(list(symbols))
    except DhanAuthError as exc:
        return {
            "error": (
                f"Dhan rejected the access token (401): {exc}. Reconnect in Settings' Data Provider section, "
                'or confirm this Dhan account has the separate paid "Data APIs" subscription -- live market '
                "quotes need it even though holdings/positions sync doesn't."
            )
        }
    except ProviderError as exc:
        return {"error": f"Dhan live-price fetch failed: {exc}"}
    prices = {symbol: quote.latest_price for symbol, quote in quotes.items()}
    snapshot_repo.upsert_user_live_prices(client, user_id, prices)
    return {"prices": prices}


def _refresh_dhan_fo_leg(client, user_id: str, connection) -> dict:
    """The F&O half of a Dhan live-price refresh (migration 0032) -- every
    futures/option contract this account's own portfolio holds or the
    Dashboard's cached CSP/CC legs reference. Split out for the same
    concurrency reason as _refresh_dhan_equity_leg. Best-effort: a
    failure here is surfaced (fo_error) but never raised, since the
    equity leg running alongside it may still have succeeded."""
    contracts = _dhan_fo_universe(client, user_id)
    fo_total = len(contracts)
    fo_quoted, fo_error, fo_missing = 0, None, []
    if contracts:
        try:
            fo_prices = DhanProvider(
                client_id=connection.client_id, access_token=connection.access_token, supabase_client=client
            ).get_fo_quotes(contracts)
        except (DhanAuthError, ProviderError) as exc:
            # Not fatal to the whole refresh -- the equity leg running
            # alongside this may still have succeeded and shouldn't be
            # discarded over an F&O-only failure -- but still surfaced
            # (not silently swallowed into "0 of N" with no explanation,
            # the exact bug that made the equity leg's own real failure
            # undiagnosable before this).
            fo_prices = {}
            fo_error = str(exc)
        snapshot_repo.upsert_user_live_fo_prices(client, user_id, fo_prices)
        fo_quoted = len(fo_prices)
        # A partial miss (some, not all, contracts quoted) is still worth
        # naming -- same "don't just say N failed, say WHICH ones"
        # convention _render_stock_summary's symbolsFailed already
        # follows for the equity/fundamentals refresh.
        fo_missing = [c for c in contracts if c not in fo_prices]
    return {"fo_quoted": fo_quoted, "fo_total": fo_total, "fo_error": fo_error, "fo_missing": fo_missing}


def _refresh_user_live_prices(client, user_id: str, broker: str) -> dict:
    """Refetches this account's live LTP across the full watched-symbol
    universe (Nifty50 constituents + this account's own portfolio
    symbols -- the same union Stock Detail/Options already use to widen
    their pickers) from its connected broker, and caches the result in
    user_live_prices for Dashboard/Stock Detail to read as an override.
    Only invoked when this account's Data Provider setting is Dhan/
    Zerodha -- see render_stock_refresh_button. For Dhan specifically,
    also widens the equity/ETF universe with every tracked ETF and
    refetches live LTP for every F&O contract _dhan_fo_universe returns
    (migration 0032) -- Zerodha has no F&O instrument resolver yet, so
    its branch is unchanged (a single call, no separate F&O leg).

    The Dhan equity and F&O legs (_refresh_dhan_equity_leg/
    _refresh_dhan_fo_leg) run **sequentially**, not concurrently --
    deliberately reverted from an earlier `ThreadPoolExecutor` version.
    Confirmed live: both legs make calls through this function's own
    `client` -- the instrument-master loaders internally
    (dhan_provider.py, migration 0035's Supabase cache) and this
    function's own `snapshot_repo.upsert_*`/`_dhan_fo_universe` reads --
    and Supabase's cached client object is not safe for two threads to
    call concurrently (`httpx.RemoteProtocolError`, the connection gets
    corrupted rather than queued). The two genuinely expensive shared
    resources this was trying to overlap are already serialized by locks
    regardless of threading -- the Dhan CSV download
    (`_get_raw_instrument_master`, once per IST day) and every Dhan API
    call (`_throttle`, a global minimum-interval gate) -- so sequential
    execution here costs little beyond not overlapping the two legs' own
    network *wait* time, a small trade for not corrupting Supabase
    connections. Returns a small summary dict for
    _render_live_prices_summary."""
    connection = portfolio_repo.get_broker_connection(client, user_id, broker)
    if connection is None or not connection.access_token:
        return {"error": f"No connected {broker} account yet -- connect one in Settings' Data Provider section."}

    equity_etf_symbols = {c.symbol for c in companies_repo.list_current_constituents(client)} | set(
        portfolio_repo.list_portfolio_symbols(client, user_id)
    )
    if broker == "Dhan":
        equity_etf_symbols |= {
            c.symbol for c in companies_repo.list_all_companies(client) if c.company_type == CompanyType.ETF
        }
    symbols = tuple(sorted(equity_etf_symbols))

    if broker == "Dhan":
        equity_result = _refresh_dhan_equity_leg(client, user_id, connection, symbols)
        fo_result = _refresh_dhan_fo_leg(client, user_id, connection)
        if equity_result.get("error"):
            # Matches the pre-parallelization behavior: an equity-leg
            # failure (most commonly an expired token, which would also
            # doom the F&O leg) reports just that error, not a partial
            # F&O result alongside it.
            return {"error": equity_result["error"]}
        prices = equity_result["prices"]
        fo_quoted, fo_total = fo_result["fo_quoted"], fo_result["fo_total"]
        fo_error, fo_missing = fo_result["fo_error"], fo_result["fo_missing"]
    else:
        # A unique cache_bust per click -- this is a user-initiated
        # "fetch fresh now" action, not something that should reuse
        # load_live_zerodha_prices' own 60s @st.cache_data TTL from an
        # earlier, possibly-stale call.
        prices = load_live_zerodha_prices(
            connection.client_id, connection.api_secret, connection.access_token, symbols, time.time()
        )
        snapshot_repo.upsert_user_live_prices(client, user_id, prices)
        fo_quoted, fo_total, fo_error, fo_missing = 0, 0, None, []

    return {
        "broker": broker,
        "quoted": len(prices),
        "total": len(symbols),
        "fo_quoted": fo_quoted,
        "fo_total": fo_total,
        "fo_error": fo_error,
        "fo_missing": fo_missing,
    }


def _run_manual_refresh(mode: str, session_key: str) -> None:
    access_token = st.session_state["sb_access_token"]
    spinner_text = (
        "Refreshing fundamentals (PE, PEG, dividend yield, 52-week high/low)..."
        if mode == "fundamentals"
        else "Refreshing stock prices and recomputing screener status..."
    )
    with st.spinner(spinner_text):
        try:
            st.session_state[session_key] = edge_refresh.trigger_manual_refresh(access_token, mode=mode)
        except edge_refresh.ManualRefreshError as exc:
            st.session_state[session_key] = {"error": str(exc)}
    st.cache_data.clear()
    st.rerun()


def _run_bhavcopy_refresh() -> None:
    access_token = st.session_state["sb_access_token"]
    with st.spinner("Refreshing NSE + BSE F&O bhavcopy..."):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {
                "nse_fo": pool.submit(edge_refresh.trigger_fo_refresh, access_token, "NSE"),
                "bse_fo": pool.submit(edge_refresh.trigger_fo_refresh, access_token, "BSE"),
            }
            for key, future in futures.items():
                try:
                    st.session_state[f"_refresh_bar_{key}"] = future.result()
                except edge_refresh.ManualRefreshError as exc:
                    st.session_state[f"_refresh_bar_{key}"] = {"error": str(exc)}
    st.cache_data.clear()
    st.rerun()


def _run_live_prices_refresh(client, user_id: str, broker: str) -> None:
    with st.spinner(f"Refreshing live {broker} stock & option quotes..."):
        st.session_state["_refresh_bar_live_prices"] = _refresh_user_live_prices(client, user_id, broker)
    st.cache_data.clear()
    st.rerun()


def _render_stock_summary(client) -> None:
    # Shown once, right after the rerun the triggering function causes --
    # a message set and then immediately st.rerun()-ed away would never
    # actually render, so this is stashed in session_state and displayed
    # on the next script run instead (same pattern for every summary
    # below).
    summary = st.session_state.pop("_refresh_bar_stock", None)
    if not summary:
        return
    if summary.get("error"):
        st.error(summary["error"])
        return
    breakdown = _universe_breakdown(client)
    if summary["failed"] == 0:
        st.success(f"✅ Refreshed all {summary['succeeded']} symbols{breakdown}.")
    else:
        failed_symbols = ", ".join(f["symbol"] for f in summary["symbolsFailed"])
        st.warning(
            f"Refreshed {summary['succeeded']} of {summary['total']} symbols{breakdown} -- "
            f"{summary['failed']} failed: {failed_symbols}"
        )


def _render_fundamentals_summary() -> None:
    summary = st.session_state.pop("_refresh_bar_fundamentals", None)
    if not summary:
        return
    if summary.get("error"):
        st.error(summary["error"])
        return
    if summary["failed"] == 0:
        st.success(f"✅ Refreshed fundamentals for all {summary['succeeded']} symbols.")
    else:
        failed_symbols = ", ".join(f["symbol"] for f in summary["symbolsFailed"])
        st.warning(
            f"Refreshed fundamentals for {summary['succeeded']} of {summary['total']} symbols -- "
            f"{summary['failed']} failed: {failed_symbols}"
        )


def _render_fo_summary(key: str, exchange_label: str) -> None:
    summary = st.session_state.pop(f"_refresh_bar_{key}", None)
    if not summary:
        return
    if summary.get("error"):
        st.error(summary["error"])
    elif summary.get("updated"):
        st.success(
            f"✅ Loaded {exchange_label} F&O bhavcopy for {summary['tradeDate']}: "
            f"{summary['futuresRows']} futures + {summary['optionRows']} option rows."
        )
    else:
        st.info(summary.get("message", f"{exchange_label} F&O data is already up to date."))


def _fmt_fo_contract(contract: tuple) -> str:
    symbol, expiry_date, strike_price, option_type = contract
    expiry = expiry_date.strftime("%d-%b-%y") if hasattr(expiry_date, "strftime") else str(expiry_date)
    if option_type == "FUT":
        return f"{symbol} {expiry} FUT"
    return f"{symbol} {expiry} {strike_price:g} {option_type}"


def _render_live_prices_summary() -> None:
    summary = st.session_state.pop("_refresh_bar_live_prices", None)
    if not summary:
        return
    if summary.get("error"):
        st.error(summary["error"])
        return
    message = f"✅ Cached live {summary['broker']} quotes for {summary['quoted']} of {summary['total']} watched symbols."
    if summary.get("fo_total"):
        message += f" Plus {summary['fo_quoted']} of {summary['fo_total']} futures/option contracts."
    st.success(message)
    if summary.get("fo_error"):
        st.warning(f"Futures/option live pricing failed: {summary['fo_error']}")
    elif summary.get("fo_missing"):
        # Capped at 20 -- a partial miss is worth naming (which
        # contracts, not just how many), but a truly large list would
        # just be noise in a one-line caption.
        shown = summary["fo_missing"][:20]
        names = ", ".join(_fmt_fo_contract(c) for c in shown)
        suffix = f", +{len(summary['fo_missing']) - 20} more" if len(summary["fo_missing"]) > 20 else ""
        st.caption(f"Not live-priced (Dhan has no matching contract, or it's simply not trading): {names}{suffix}")


def render_fundamental_and_bhavcopy_refresh(client) -> None:
    """Settings-only "Data Refresh" section: Fundamental Data Refresh
    (YFinance fundamentals -- PE, PEG, dividend yield, 52-week high/low)
    and Bhavcopy Refresh (NSE + BSE F&O, one button covering both
    exchanges). Neither depends on this account's Data Provider setting
    -- fundamentals and the full F&O chain always come from YFinance/
    Bhavcopy regardless of it."""
    st.subheader("Data Refresh")
    st.caption(_last_fetch_caption(client, "Last fundamentals refresh", ["fundamentals", "all"]))
    st.caption(_last_fetch_caption(client, "Last NSE F&O refresh", "fo", _NSE_FO_PROVIDER))
    st.caption(_last_fetch_caption(client, "Last BSE F&O refresh", "fo", _BSE_FO_PROVIDER))

    col1, col2 = st.columns(2)
    with col1:
        if st.button("Fundamental Data Refresh", key="refresh_bar_fundamentals_btn"):
            _run_manual_refresh("fundamentals", "_refresh_bar_fundamentals")
    with col2:
        if st.button("Bhavcopy Refresh", key="refresh_bar_bhavcopy_btn"):
            _run_bhavcopy_refresh()

    _render_fundamentals_summary()
    _render_fo_summary("nse_fo", "NSE")
    _render_fo_summary("bse_fo", "BSE")


def render_stock_refresh_button(client, user_id: str, data_provider: str) -> None:
    """Every page except Settings: shows exactly one of Stock Data
    Refresh (YFinance/Bhavcopy accounts -- price + screener recompute) or
    Stock & Option Data Refresh (Dhan/Zerodha accounts -- live broker
    LTP, plus F&O contracts for Dhan -- see _refresh_user_live_prices),
    by this account's Data Provider setting (Settings page)."""
    broker = _BROKER_BY_PROVIDER.get(data_provider)
    if broker:
        st.caption(f"Stock LTP source: live {broker} quotes (Data Provider setting, in Settings).")
        if st.button("Stock & Option Data Refresh", key="refresh_bar_broker_btn"):
            _run_live_prices_refresh(client, user_id, broker)
        _render_live_prices_summary()
    else:
        st.caption(_last_fetch_caption(client, "Last stock refresh", ["intraday_price", "price", "all"]))
        if st.button("Stock Data Refresh", key="refresh_bar_stock_btn"):
            _run_manual_refresh("price", "_refresh_bar_stock")
        _render_stock_summary(client)


def render_portfolio_refresh_button(client, user_id: str, data_provider: str) -> None:
    """My Trades/My Holdings/My Positions/My CSP only: Portfolio Refresh,
    visible only for a Dhan/Zerodha-provider account -- re-syncs
    holdings/positions from that broker via
    data_provider_settings.sync_broker_portfolio (the same sync Settings'
    "Save & Sync"/"Update credentials" forms trigger). Renders nothing
    for a YFinance/Bhavcopy account, which has no broker connection to
    sync from."""
    broker = _BROKER_BY_PROVIDER.get(data_provider)
    if not broker:
        return
    st.caption(_last_fetch_caption(client, "Last portfolio refresh", "portfolio_sync", broker.lower()))
    if st.button("Portfolio Refresh", key="refresh_bar_portfolio_btn"):
        sync_broker_portfolio(client=client, user_id=user_id, data_provider=data_provider)
