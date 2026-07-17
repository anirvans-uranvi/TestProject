#!/usr/bin/env python
"""Populates Supabase with synthetic (mock) market data for local
development, regardless of the configured MARKET_DATA_PROVIDER /
FUNDAMENTALS_PROVIDER env vars -- this always uses the mock providers so
the Dashboard/Stock Detail pages have something realistic to render
without any paid credentials.

Run supabase/migrations + supabase/seed.sql first (constituents/companies
must already exist), then:

    python scripts/seed_mock_data.py
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data_providers.mock_provider import (  # noqa: E402
    MockFOProvider,
    MockFundamentalsProvider,
    MockPriceProvider,
)
from src.repositories import (  # noqa: E402
    companies_repo,
    dividends_repo,
    fo_repo,
    fundamentals_repo,
    price_repo,
    snapshot_repo,
)
from src.repositories.supabase_client import get_service_client  # noqa: E402
from src.services.fo_service import ingest_fo_day  # noqa: E402
from src.services.screener_service import compute_screener_row  # noqa: E402
from src.utils.logging import get_logger  # noqa: E402

logger = get_logger(__name__)

PRICE_LOOKBACK_DAYS = 400
DIVIDEND_LOOKBACK_DAYS = 400
SNAPSHOT_BACKFILL_DAYS = 60
FO_BACKFILL_DAYS = 30


def seed_mock_fo(client, symbols: list[str], today: date, days: int = FO_BACKFILL_DAYS) -> None:
    """Seed synthetic futures + option-chain history so the Options screen has
    data locally without hitting NSE."""
    provider = MockFOProvider()
    universe = set(symbols)
    seeded = 0
    d = today
    while seeded < days:
        if d.weekday() < 5:  # Mon-Fri
            book = provider.fetch_day(d, universe=universe)
            ingest_fo_day(client, book)
            seeded += 1
        d -= timedelta(days=1)
    fo_repo.refresh_open_flags(client, today)
    logger.info("seeded mock F&O: %d trading days for %d symbols", days, len(symbols))


def main() -> None:
    client = get_service_client()
    price_provider = MockPriceProvider()
    fundamentals_provider = MockFundamentalsProvider()

    symbols = [c.symbol for c in companies_repo.list_current_constituents(client)]
    if not symbols:
        logger.warning("No constituents found -- apply supabase/seed.sql before seeding mock data")
        return

    today = date.today()
    history_start = today - timedelta(days=PRICE_LOOKBACK_DAYS)
    dividend_start = today - timedelta(days=DIVIDEND_LOOKBACK_DAYS)
    backfill_start = today - timedelta(days=SNAPSHOT_BACKFILL_DAYS)

    for symbol in symbols:
        points = price_provider.get_historical_daily(symbol, history_start, today)
        price_repo.upsert_price_history(client, points)

        fundamentals = fundamentals_provider.get_fundamentals(symbol, today)
        if fundamentals is not None:
            fundamentals_repo.upsert_fundamental_snapshot(client, fundamentals)

        dividends = fundamentals_provider.get_dividend_history(symbol, dividend_start, today)
        dividends_repo.upsert_dividend_events(client, dividends)

        closes_by_date = {p.trade_date: p.effective_close for p in points}
        sorted_dates = sorted(closes_by_date)
        date_index = {d: i for i, d in enumerate(sorted_dates)}

        snapshot_count = 0
        for d in sorted_dates:
            if d < backfill_start:
                continue
            idx = date_index[d]
            historical_closes = [closes_by_date[sorted_dates[i]] for i in range(idx)]
            row = compute_screener_row(
                symbol=symbol,
                latest_price=closes_by_date[d],
                historical_closes=historical_closes,
                dividend_events=[e for e in dividends if e.ex_date <= d],
                pe_ratio=fundamentals.pe_ratio if fundamentals else None,
                peg_ratio=fundamentals.peg_ratio if fundamentals else None,
                as_of_date=d,
            )
            snapshot_repo.upsert_daily_snapshot(client, row)
            snapshot_count += 1

        logger.info("seeded %s: %d price points, %d dividends, %d daily snapshots", symbol, len(points), len(dividends), snapshot_count)

    seed_mock_fo(client, symbols, today)

    logger.info("mock data seeding complete for %d symbols", len(symbols))


if __name__ == "__main__":
    main()
