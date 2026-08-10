#!/usr/bin/env python
"""Fetch NSE F&O (futures + options) end-of-day data into Supabase.

Source: the NSE F&O UDiFF bhavcopy, one zip per trading day. This is EOD
data (published ~6pm IST after close), so "latest price" here is the most
recent close/settlement, not a live quote.

Usage:
    python scripts/fetch_fo_data.py                 # backfill last 60 trading days
    python scripts/fetch_fo_data.py --days 20       # fewer days
    python scripts/fetch_fo_data.py --date 2026-07-16   # one specific day
    python scripts/fetch_fo_data.py --mock          # synthetic data, no network

Requires SUPABASE_SERVICE_ROLE_KEY -- writes shared market data (bypasses
RLS) on behalf of all users, like the other scripts/ jobs.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import requests
from postgrest.exceptions import APIError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data_providers import nse_fo_provider  # noqa: E402
from src.data_providers.mock_provider import MockFOProvider  # noqa: E402
from src.data_providers.nse_fo_provider import FOBhavcopy  # noqa: E402
from src.data_providers.yfinance_provider import fetch_display_name  # noqa: E402
from src.models.enums import CompanyType  # noqa: E402
from src.repositories import companies_repo, fo_repo  # noqa: E402
from src.repositories.supabase_client import get_service_client  # noqa: E402
from src.services import portfolio_service  # noqa: E402
from src.services.fo_service import ingest_fo_day, recompute_dashboard_metrics  # noqa: E402
from src.utils.logging import get_logger  # noqa: E402

logger = get_logger(__name__)

DEFAULT_DAYS = 60


def _mock_books(universe: set[str], days: int, end: date) -> list[FOBhavcopy]:
    provider = MockFOProvider()
    books: list[FOBhavcopy] = []
    d = end
    while len(books) < days:
        if d.weekday() < 5:  # Mon-Fri
            books.append(provider.fetch_day(d, universe=universe))
        d -= timedelta(days=1)
    return sorted(books, key=lambda b: b.trade_date)


def _live_books(universe: set[str], days: int, end: date) -> list[FOBhavcopy]:
    session = requests.Session()
    books: list[FOBhavcopy] = []
    d = end
    # Walk back generously (holidays + weekends) until we collect `days` files.
    horizon = d - timedelta(days=days * 3 + 15)
    while len(books) < days and d >= horizon:
        book = nse_fo_provider.fetch_fo_bhavcopy(d, universe=universe, session=session)
        if book is not None and not book.is_empty:
            books.append(book)
            logger.info(
                "fetched F&O bhavcopy %s: %d futures / %d option rows",
                d, len(book.futures_prices), len(book.option_prices),
            )
        d -= timedelta(days=1)
    return sorted(books, key=lambda b: b.trade_date)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch NSE F&O EOD data into Supabase")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="trading days to backfill (default 60)")
    parser.add_argument("--date", type=str, help="a single trading day YYYY-MM-DD")
    parser.add_argument("--mock", action="store_true", help="use synthetic data, no network")
    args = parser.parse_args()

    client = get_service_client()
    universe = {c.symbol for c in companies_repo.list_current_constituents(client)}
    if not universe:
        logger.warning("No constituents found -- apply supabase/seed.sql first")
        return

    # Index rows (NIFTY, BANKNIFTY, SENSEX -- migration 0018) so index
    # options actually get ingested, not just stock options. nse_fo_provider
    # only keeps IDO (index option) rows for symbols in this universe --
    # SENSEX is seeded too but is BSE-listed, so it will never actually
    # match a row in NSE's bhavcopy.
    index_symbols = {c.symbol for c in companies_repo.list_all_companies(client) if c.company_type == CompanyType.INDEX}
    universe |= index_symbols

    # Also fetch F&O for any symbol referenced by uploaded portfolios (ETFs,
    # non-Nifty50 stocks) that has derivatives -- same widening
    # scripts/run_refresh.py already does for cash-market data, applied
    # here so a portfolio stock like Hindustan Zinc actually gets its
    # futures/options ingested too, not just its equity LTP. Registers a
    # minimal companies row for any not seen before. Tolerant of the
    # portfolio_holdings migration not being applied yet.
    try:
        portfolio_rows = (
            client.table("portfolio_holdings").select("symbol, raw_name").not_.is_("symbol", "null").execute().data
            or []
        )
    except APIError:
        portfolio_rows = []
    if portfolio_rows:
        known_symbols = {c.symbol for c in companies_repo.list_all_companies(client)}
        raw_name_by_symbol = {r["symbol"]: r["raw_name"] for r in portfolio_rows}
        portfolio_symbols = [r["symbol"] for r in portfolio_rows]
        new_companies = portfolio_service.resolve_tracked_symbols(portfolio_symbols, known_symbols, raw_name_by_symbol)
        if new_companies:
            # Same best-effort ETF/fund classification as
            # scripts/run_refresh.py -- see looks_like_etf_name()'s
            # docstring. Registration can happen here first (this script
            # and run_refresh.py run independently on cron), so both
            # paths need to classify, not just one.
            for company in new_companies:
                display_name = fetch_display_name(company.symbol)
                if display_name and portfolio_service.looks_like_etf_name(display_name):
                    company.company_type = CompanyType.ETF
            companies_repo.upsert_companies(client, new_companies)
            logger.info("portfolio tracking: registered %d new symbol(s)", len(new_companies))
        universe = universe | set(portfolio_symbols)

    end = date.fromisoformat(args.date) if args.date else date.today()
    days = 1 if args.date else args.days

    if args.mock:
        books = _mock_books(universe, days, end)
    elif args.date:
        book = nse_fo_provider.fetch_fo_bhavcopy(end, universe=universe)
        books = [book] if book and not book.is_empty else []
    else:
        books = _live_books(universe, days, end)

    if not books:
        logger.warning("No F&O bhavcopy data found for the requested range")
        return

    totals = {"futures_prices": 0, "option_prices": 0}
    for book in books:
        counts = ingest_fo_day(client, book)
        totals["futures_prices"] += counts["futures_prices"]
        totals["option_prices"] += counts["option_prices"]
        logger.info("ingested %s: %d futures / %d option rows", book.trade_date, counts["futures_prices"], counts["option_prices"])

    # Finalize open/closed flags against the real calendar (contracts appear
    # in the file only while live, so is_open can't be derived per-file).
    fo_repo.refresh_open_flags(client, date.today())

    logger.info(
        "F&O ingest complete: %d days, %d futures + %d option daily rows for %d symbols",
        len(books), totals["futures_prices"], totals["option_prices"], len(universe),
    )

    # Option data just changed, which feeds the Dashboard's precomputed 5%
    # CSP / 5% CC cache -- recompute once at the end (not per day inside
    # the backfill loop above, since only the final state matters).
    metrics_count = recompute_dashboard_metrics(client)
    logger.info("dashboard F&O metrics cache: recomputed %d rows", metrics_count)


if __name__ == "__main__":
    main()
