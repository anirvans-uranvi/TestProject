"""Shared "refresh data" bar shown at a consistent location -- right
after the page title/disclaimer -- on every page (Dashboard, Stock
Detail, Options, My Trades, My Holdings, My Positions, My CSP, Analyse
Trade, Settings), so a user never has to navigate back to one specific
page just to trigger a data refresh.

One "🔄 Market Data Refresh" button (previously three separate ones --
Stock Data Refresh, NSE F&O Data Refresh, BSE F&O Data Refresh --
collapsed into one on request). A click fires every applicable fetch
*concurrently* via a ThreadPoolExecutor rather than one-after-another
(these are blocking network calls; running them in parallel cuts wall-
clock time to roughly the slowest one instead of their sum):
- Stock prices + fundamentals + screener recompute, via the
  manual-refresh Edge Function (Yahoo Finance) -- always, regardless of
  this account's Data Provider setting, since fundamentals (PEG,
  dividend) aren't available from Dhan/Zerodha's APIs at all.
- NSE and BSE F&O bhavcopy, via the fo-refresh Edge Function -- also
  always; neither broker exposes a bhavcopy-equivalent full options
  chain dump, so F&O ingestion stays bhavcopy-sourced regardless of
  provider.
- **New**: if this account's Data Provider setting (Settings page) is
  Dhan or Zerodha, also refetches live stock LTP from that broker across
  the full watched-symbol universe and caches it in `user_live_prices`
  (migration 0030) -- what Dashboard/Stock Detail read as an override on
  top of the shared daily_screener_snapshots value, see
  src/repositories/snapshot_repo.py's get_user_live_prices.
- **New**, Dhan only (migration 0032): also refetches live LTP for every
  ETF (tracked but not shown on the Screener table), plus every futures/
  option contract this account's own portfolio holds or the Dashboard's
  cached 5% CSP/5% CC legs reference -- see _dhan_fo_universe and
  DhanProvider.get_fo_quotes. Zerodha has no F&O instrument resolver yet,
  so a Zerodha-provider account's refresh is unchanged (equity LTP only).

A click always calls `st.cache_data.clear()` -- the *entire* app-wide
cache, not just the current page's -- before `st.rerun()`-ing, so every
page's own `@st.cache_data` loaders re-fetch fresh data regardless of
which page the click happened on.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import streamlit as st

from src.data_providers.base import ProviderError
from src.data_providers.dhan_provider import DhanAuthError, DhanProvider
from src.models.enums import CompanyType
from src.repositories import companies_repo, fetch_log_repo, fo_repo, portfolio_repo, settings_repo, snapshot_repo
from src.services import edge_refresh
from src.utils.portfolio_page import load_live_zerodha_prices
from src.utils.session import current_user_id
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


def _refresh_user_live_prices(client, user_id: str, broker: str) -> dict:
    """Refetches this account's live LTP across the full watched-symbol
    universe (Nifty50 constituents + this account's own portfolio
    symbols -- the same union Stock Detail/Options already use to widen
    their pickers) from its connected broker, and caches the result in
    user_live_prices for Dashboard/Stock Detail to read as an override.
    Only invoked when this account's Data Provider setting is Dhan/
    Zerodha -- see render_global_refresh_bar. For Dhan specifically, also
    widens the equity/ETF universe with every tracked ETF and refetches
    live LTP for every F&O contract _dhan_fo_universe returns (migration
    0032) -- Zerodha has no F&O instrument resolver yet, so its branch is
    unchanged. Returns a small summary dict for _render_live_prices_summary."""
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
    # A unique cache_bust per click -- this is a user-initiated "fetch
    # fresh now" action, not something that should reuse
    # load_live_zerodha_prices' own 60s @st.cache_data TTL from an
    # earlier, possibly-stale call.
    cache_bust = time.time()
    if broker == "Dhan":
        # Calls DhanProvider directly rather than going through
        # load_live_dhan_prices (which swallows DhanAuthError/
        # ProviderError into a silent {}) -- confirmed live: that made a
        # real failure (an expired token, or a Dhan account missing the
        # separate paid "Data APIs" subscription market quotes require --
        # see portfolio_service.apply_fallback_option_ltp's docstring for
        # the same 401 already seen on the positions-sync path)
        # indistinguishable from "nothing to quote", showing a misleading
        # green "0 of N" instead of a real error. No caching benefit lost
        # here either way -- the fresh cache_bust above already busts
        # load_live_dhan_prices' own cache on every call.
        try:
            quotes = DhanProvider(client_id=connection.client_id, access_token=connection.access_token).get_quotes(
                list(symbols)
            )
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
    else:
        prices = load_live_zerodha_prices(
            connection.client_id, connection.api_secret, connection.access_token, symbols, cache_bust
        )
    snapshot_repo.upsert_user_live_prices(client, user_id, prices)

    fo_quoted, fo_total, fo_error = 0, 0, None
    if broker == "Dhan":
        contracts = _dhan_fo_universe(client, user_id)
        fo_total = len(contracts)
        if contracts:
            try:
                fo_prices = DhanProvider(
                    client_id=connection.client_id, access_token=connection.access_token
                ).get_fo_quotes(contracts)
            except (DhanAuthError, ProviderError) as exc:
                # Best-effort, not fatal to the whole refresh -- the
                # equity leg above already succeeded and shouldn't be
                # discarded over an F&O-only failure -- but still
                # surfaced (not silently swallowed into "0 of N" with no
                # explanation, the exact bug that made the equity leg's
                # own real failure undiagnosable before this).
                fo_prices = {}
                fo_error = str(exc)
            snapshot_repo.upsert_user_live_fo_prices(client, user_id, fo_prices)
            fo_quoted = len(fo_prices)

    return {
        "broker": broker,
        "quoted": len(prices),
        "total": len(symbols),
        "fo_quoted": fo_quoted,
        "fo_total": fo_total,
        "fo_error": fo_error,
    }


def _run_all(client, user_id: str, data_provider: str) -> None:
    access_token = st.session_state["sb_access_token"]
    tasks = {
        "stock": lambda: edge_refresh.trigger_manual_refresh(access_token),
        "nse_fo": lambda: edge_refresh.trigger_fo_refresh(access_token, "NSE"),
        "bse_fo": lambda: edge_refresh.trigger_fo_refresh(access_token, "BSE"),
    }
    broker = _BROKER_BY_PROVIDER.get(data_provider)
    if broker:
        tasks["live_prices"] = lambda: _refresh_user_live_prices(client, user_id, broker)

    with st.spinner("Refreshing market data -- stocks, fundamentals, and NSE + BSE F&O, all at once. This can take a few minutes..."):
        with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
            futures = {key: pool.submit(action) for key, action in tasks.items()}
            for key, future in futures.items():
                try:
                    st.session_state[f"_refresh_bar_{key}"] = future.result()
                except edge_refresh.ManualRefreshError as exc:
                    st.session_state[f"_refresh_bar_{key}"] = {"error": str(exc)}
    st.cache_data.clear()
    st.rerun()


def _render_stock_summary(client) -> None:
    # Shown once, right after the rerun _run_all() triggers -- a message
    # set and then immediately st.rerun()-ed away would never actually
    # render, so this is stashed in session_state and displayed on the
    # next script run instead (same pattern for every summary below).
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


def render_global_refresh_bar(client) -> None:
    """Renders the caption(s) + single Market Data Refresh button, plus
    (once, right after a click) each fetch's own result message. Reads
    the signed-in user's access token from
    `st.session_state["sb_access_token"]` -- every page that calls this
    has already gone through `require_login()`, which sets it."""
    user_id = current_user_id()
    user_settings = settings_repo.get_user_settings(client, user_id)

    st.caption(_last_fetch_caption(client, "Last stock refresh", ["intraday_price", "all"]))
    st.caption(_last_fetch_caption(client, "Last NSE F&O refresh", "fo", _NSE_FO_PROVIDER))
    st.caption(_last_fetch_caption(client, "Last BSE F&O refresh", "fo", _BSE_FO_PROVIDER))
    broker = _BROKER_BY_PROVIDER.get(user_settings.data_provider)
    if broker:
        st.caption(f"Stock LTP source: live {broker} quotes (Data Provider setting, in Settings).")

    if st.button("🔄 Market Data Refresh", key="refresh_bar_market_data_btn"):
        _run_all(client, user_id, user_settings.data_provider)

    _render_stock_summary(client)
    _render_fo_summary("nse_fo", "NSE")
    _render_fo_summary("bse_fo", "BSE")
    _render_live_prices_summary()
