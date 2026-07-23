#!/usr/bin/env python
"""CLI entrypoint for scheduled data refresh.

Usage:
    python scripts/run_refresh.py --mode=intraday
    python scripts/run_refresh.py --mode=eod
    python scripts/run_refresh.py --mode=fundamentals
    python scripts/run_refresh.py --mode=screener
    python scripts/run_refresh.py --mode=all
    python scripts/run_refresh.py --mode=intraday --daemon   # APScheduler loop

Invoked by GitHub Actions cron (.github/workflows/refresh_prices.yml) as a
one-shot process per mode, or run with --daemon for a standalone
long-running APScheduler process (e.g. inside the Docker container).

Requires SUPABASE_SERVICE_ROLE_KEY -- this script bypasses RLS to write
shared market data on behalf of all users.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from postgrest.exceptions import APIError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import get_settings  # noqa: E402
from src.data_providers.factory import get_fundamentals_provider, get_price_provider  # noqa: E402
from src.repositories import companies_repo  # noqa: E402
from src.repositories.supabase_client import get_service_client  # noqa: E402
from src.services import fo_service, portfolio_service, refresh_service, screener_service  # noqa: E402
from src.services.market_calendar import is_trading_day  # noqa: E402
from src.utils.logging import get_logger  # noqa: E402
from src.utils.timezones import now_ist  # noqa: E402

logger = get_logger(__name__)


def run_once(mode: str) -> None:
    settings = get_settings()
    client = get_service_client()
    symbols = [c.symbol for c in companies_repo.list_current_constituents(client)]
    if not symbols:
        logger.warning("No current Nifty 50 constituents found -- apply supabase/seed.sql first")
        return

    # Also track any symbols referenced by uploaded portfolios (ETFs,
    # gilt/liquid funds, non-Nifty50 stocks) so their LTP gets refreshed
    # too -- registers a minimal companies row for any not seen before.
    # nifty50_constituents is never touched, so these stay excluded from
    # the Dashboard's screener view (its latest_screener_view inner-joins
    # on is_current). Tolerant of the portfolio_holdings migration not
    # being applied yet, same as the F&O cache recompute below.
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
            companies_repo.upsert_companies(client, new_companies)
            logger.info("portfolio tracking: registered %d new symbol(s)", len(new_companies))
        symbols = sorted(set(symbols) | set(portfolio_symbols))

    if mode in ("intraday", "all"):
        if is_trading_day(now_ist().date()):
            provider = get_price_provider(settings)
            failed = refresh_service.refresh_intraday_prices(client, symbols, provider)
            logger.info("intraday refresh: %d/%d symbols failed", len(failed), len(symbols))
        else:
            logger.info("intraday refresh skipped -- not a trading day")

    if mode in ("eod", "all"):
        provider = get_price_provider(settings)
        failed = refresh_service.refresh_eod_prices(client, symbols, provider)
        logger.info("eod refresh: %d/%d symbols failed", len(failed), len(symbols))

    if mode in ("fundamentals", "all"):
        provider = get_fundamentals_provider(settings)
        failed = refresh_service.refresh_fundamentals(client, symbols, provider)
        logger.info("fundamentals refresh: %d/%d symbols failed", len(failed), len(symbols))

    if mode in ("screener", "all"):
        rows = screener_service.refresh_all_screener_rows(
            client,
            symbols,
            dividend_yield_threshold=settings.default_dividend_yield_threshold,
            peg_threshold=settings.default_peg_threshold,
            stale_threshold_minutes=settings.default_stale_data_threshold_minutes,
        )
        logger.info("screener refresh: computed %d rows", len(rows))

        # Spot price just changed for every symbol, which feeds the
        # Dashboard's precomputed 5% CSP / 5% CC cache -- recompute it
        # here too. Tolerant of the dashboard_fo_metrics migration not
        # being applied yet (mirrors the Dashboard's own
        # APIError-catching degrade-to-N/A for F&O data), so an older
        # deployment's cron doesn't break.
        try:
            metrics_count = fo_service.recompute_dashboard_metrics(client)
            logger.info("dashboard F&O metrics cache: recomputed %d rows", metrics_count)
        except APIError as exc:
            logger.warning("dashboard F&O metrics cache recompute skipped: %s", exc)


def run_daemon(mode: str) -> None:
    from apscheduler.schedulers.blocking import BlockingScheduler

    settings = get_settings()
    scheduler = BlockingScheduler(timezone="Asia/Kolkata")
    scheduler.add_job(
        run_once, "interval", args=[mode], minutes=settings.intraday_refresh_interval_minutes, next_run_time=None
    )
    logger.info("starting APScheduler daemon: mode=%s every %d min", mode, settings.intraday_refresh_interval_minutes)
    scheduler.start()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["intraday", "eod", "fundamentals", "screener", "all"], required=True)
    parser.add_argument("--daemon", action="store_true", help="run forever on an APScheduler interval")
    args = parser.parse_args()

    if args.daemon:
        run_daemon(args.mode)
    else:
        run_once(args.mode)


if __name__ == "__main__":
    main()
