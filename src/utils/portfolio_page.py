"""Cross-page helpers for the Portfolio feature's five pages -- My Broker
(pages/6_My_Broker.py), My Trades (7), My Holdings (8), My Positions (9),
and Analyse Trade (10, hidden from the sidebar). Every page needs the same
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
def load_positions(_client, _user_id: str, _cache_bust: int):
    return portfolio_repo.list_positions(_client, _user_id)


@st.cache_data(ttl=60, show_spinner=False)
def load_broker_connection(_client, _user_id: str, portfolio_name: str, broker: str, _cache_bust: int):
    return portfolio_repo.get_broker_connection(_client, _user_id, portfolio_name, broker)


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
def load_option_expiries(_client, symbols: tuple[str, ...], _cache_bust: int) -> dict[str, list[str]]:
    return {symbol: [d.isoformat() for d in fo_repo.list_option_expiries(_client, symbol)] for symbol in symbols}


@st.cache_data(ttl=60, show_spinner=False)
def load_option_chain(_client, symbol: str, expiry_iso: str, _cache_bust: int) -> list[dict]:
    return fo_repo.get_option_chain(_client, symbol, date.fromisoformat(expiry_iso))


def build_trade_legs(client, cache_bust: int, holdings_for_portfolio: list, positions_for_portfolio: list) -> list[dict]:
    """Unmerged per-broker leg list for one portfolio, ready for
    portfolio_service.group_into_trades -- shared by My Trades (the list
    view) and Analyse Trade (one trade's detail view) so bucket/label
    logic never drifts between the two. Unlike My Holdings' merge_holdings
    (which combines a symbol's rows across brokers into one row), Trades
    need per-leg identity: that's the same natural key (broker, raw_name)
    portfolio_trade_groups overrides are keyed by, and what a merge/split
    in Analyse Trade actually reassigns."""
    holding_dicts = [
        {"raw_name": h.raw_name, "symbol": h.symbol, "qty": h.qty, "avg_price": h.avg_price, "investment": h.investment}
        for h in holdings_for_portfolio
    ]
    holding_symbols = tuple(sorted({d["symbol"] for d in holding_dicts if d["symbol"]}))
    ltp_by_symbol = load_latest_prices(client, holding_symbols, cache_bust)
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
