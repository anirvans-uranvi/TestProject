"""Orchestrates fetch (via repos, which hold already-normalized data) ->
calculate (pure functions in src.calculations) -> persist daily snapshot.

Raw provider records are written to price_history / fundamental_snapshots /
dividend_events by refresh_service; this module reads that normalized data
back out and turns it into the calculated audit-trail row.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from supabase import Client

from src.calculations.classification import build_classification
from src.calculations.dividends import ttm_dividend_yield
from src.calculations.returns import return_1d, return_5d, return_20d
from src.models.market_data import DividendEvent
from src.models.screener import DailyScreenerSnapshot
from src.repositories import dividends_repo, fundamentals_repo, price_repo, snapshot_repo
from src.services.market_calendar import IST

HISTORY_LOOKBACK_DAYS = 45  # comfortably covers 20 trading days + weekends/holidays
DIVIDEND_LOOKBACK_DAYS = 400  # >365 so the TTM window is always fully covered


def compute_screener_row(
    symbol: str,
    latest_price: float | None,
    historical_closes: list[float | None],
    dividend_events: list[DividendEvent],
    pe_ratio: float | None,
    peg_ratio: float | None,
    as_of_date: date,
    dividend_yield_threshold: float = 3.0,
    peg_threshold: float = 1.0,
    is_stale: bool = False,
    stale_minutes: float | None = None,
) -> DailyScreenerSnapshot:
    """Pure calculation step -- no I/O, fully unit-testable."""
    r1 = return_1d(latest_price, historical_closes)
    r5 = return_5d(latest_price, historical_closes)
    r20 = return_20d(latest_price, historical_closes)
    ttm_yield = ttm_dividend_yield(dividend_events, as_of_date, latest_price)

    classification = build_classification(
        ttm_dividend_yield=ttm_yield,
        return_1d=r1,
        return_5d=r5,
        return_20d=r20,
        peg_ratio=peg_ratio,
        is_stale=is_stale,
        stale_minutes=stale_minutes,
        dividend_yield_threshold=dividend_yield_threshold,
        peg_threshold=peg_threshold,
        latest_price=latest_price,
        pe_ratio=pe_ratio,
    )

    return DailyScreenerSnapshot(
        symbol=symbol,
        snapshot_date=as_of_date,
        latest_price=latest_price,
        return_1d=r1,
        return_5d=r5,
        return_20d=r20,
        ttm_dividend_yield=ttm_yield,
        pe_ratio=pe_ratio,
        peg_ratio=peg_ratio,
        criterion_a=classification.criterion_a,
        criterion_b=classification.criterion_b,
        criterion_c=classification.criterion_c,
        status=classification.status,
        data_quality=classification.data_quality,
    )


def refresh_screener_row_for_symbol(
    client: Client,
    symbol: str,
    dividend_yield_threshold: float = 3.0,
    peg_threshold: float = 1.0,
    stale_threshold_minutes: int = 30,
    as_of: datetime | None = None,
) -> DailyScreenerSnapshot:
    """Reads normalized data already in Supabase for `symbol`, computes the
    row, persists it to daily_screener_snapshots, and returns it."""
    as_of = as_of.astimezone(IST) if as_of else datetime.now(IST)
    as_of_date = as_of.date()

    latest_point = price_repo.get_latest_close(client, symbol)
    latest_price = latest_point.effective_close if latest_point else None

    is_stale = False
    stale_minutes = None
    if latest_point:
        # Treat the price as stale if its trade_date is older than the
        # threshold would imply for an intraday feed (EOD data on its own
        # trade_date is never "stale" by this measure; a live quote that
        # hasn't refreshed within stale_threshold_minutes is).
        fetched_at = datetime.combine(latest_point.trade_date, datetime.min.time(), tzinfo=IST)
        stale_minutes = (as_of - fetched_at).total_seconds() / 60
        is_stale = latest_point.trade_date < as_of_date - timedelta(days=5)

    history_start = as_of_date - timedelta(days=HISTORY_LOOKBACK_DAYS)
    history = price_repo.get_price_history(client, symbol, history_start, as_of_date - timedelta(days=1))
    historical_closes = [p.effective_close for p in history]

    fundamentals = fundamentals_repo.get_latest_fundamentals(client, symbol)
    pe_ratio = fundamentals.pe_ratio if fundamentals else None
    peg_ratio = fundamentals.peg_ratio if fundamentals else None
    if fundamentals and fundamentals.is_stale:
        is_stale = True

    dividend_start = as_of_date - timedelta(days=DIVIDEND_LOOKBACK_DAYS)
    dividend_events = dividends_repo.get_dividend_events(client, symbol, dividend_start, as_of_date)

    row = compute_screener_row(
        symbol=symbol,
        latest_price=latest_price,
        historical_closes=historical_closes,
        dividend_events=dividend_events,
        pe_ratio=pe_ratio,
        peg_ratio=peg_ratio,
        as_of_date=as_of_date,
        dividend_yield_threshold=dividend_yield_threshold,
        peg_threshold=peg_threshold,
        is_stale=is_stale,
        stale_minutes=stale_minutes,
    )
    snapshot_repo.upsert_daily_snapshot(client, row)
    return row


def refresh_all_screener_rows(
    client: Client,
    symbols: list[str],
    dividend_yield_threshold: float = 3.0,
    peg_threshold: float = 1.0,
    stale_threshold_minutes: int = 30,
    as_of: datetime | None = None,
) -> list[DailyScreenerSnapshot]:
    return [
        refresh_screener_row_for_symbol(
            client, symbol, dividend_yield_threshold, peg_threshold, stale_threshold_minutes, as_of
        )
        for symbol in symbols
    ]
