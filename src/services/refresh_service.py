"""Fetch (via a PriceDataProvider/FundamentalsDataProvider) -> normalize ->
persist raw/normalized records, with retry + provider_fetch_log auditing.

`price_history` intraday upserts only include the columns actually fetched
(close/adjusted_close as the live quote) -- PostgREST's upsert only sets
columns present in the request body, so a same-day EOD upsert later filling
open/high/low is not clobbered by an earlier intraday partial upsert, and
vice versa.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from supabase import Client
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.data_providers.base import FundamentalsDataProvider, PriceDataProvider, ProviderError
from src.models.enums import FetchStatus, FetchType
from src.models.fetch_log import ProviderFetchLog
from src.models.market_data import PricePoint
from src.repositories import dividends_repo, fetch_log_repo, fundamentals_repo, price_repo
from src.services.market_calendar import IST

EOD_LOOKBACK_DAYS = 90
DIVIDEND_LOOKBACK_DAYS = 400

_retry_provider_call = retry(
    retry=retry_if_exception_type(ProviderError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=15),
    reraise=True,
)


def _log(client: Client, provider_name: str, fetch_type: FetchType, symbol: str | None, started: datetime, error: str | None) -> None:
    fetch_log_repo.log_fetch(
        client,
        ProviderFetchLog(
            provider_name=provider_name,
            fetch_type=fetch_type,
            symbol=symbol,
            status=FetchStatus.FAILURE if error else FetchStatus.SUCCESS,
            error_message=error,
            started_at=started,
            finished_at=datetime.now(IST),
        ),
    )


def refresh_intraday_prices(client: Client, symbols: list[str], provider: PriceDataProvider) -> list[str]:
    """Updates today's price_history row with the latest quote. Returns the
    list of symbols that failed (already logged to provider_fetch_log)."""
    failed: list[str] = []
    started = datetime.now(IST)
    try:
        quotes = _retry_provider_call(provider.get_quotes)(symbols)
    except ProviderError as exc:
        for symbol in symbols:
            _log(client, provider.name, FetchType.INTRADAY_PRICE, symbol, started, str(exc))
        return list(symbols)

    for symbol in symbols:
        quote = quotes.get(symbol)
        if quote is None:
            _log(client, provider.name, FetchType.INTRADAY_PRICE, symbol, started, "no quote returned")
            failed.append(symbol)
            continue
        point = PricePoint(
            symbol=symbol,
            trade_date=quote.as_of.astimezone(IST).date(),
            close=quote.latest_price,
            adjusted_close=quote.latest_price,
            source=quote.source,
        )
        price_repo.upsert_price_history(client, [point])
        _log(client, provider.name, FetchType.INTRADAY_PRICE, symbol, started, None)
    return failed


def refresh_eod_prices(
    client: Client,
    symbols: list[str],
    provider: PriceDataProvider,
    lookback_days: int = EOD_LOOKBACK_DAYS,
    as_of: date | None = None,
) -> list[str]:
    as_of = as_of or datetime.now(IST).date()
    from_date = as_of - timedelta(days=lookback_days)
    failed: list[str] = []
    for symbol in symbols:
        started = datetime.now(IST)
        try:
            points = _retry_provider_call(provider.get_historical_daily)(symbol, from_date, as_of)
            price_repo.upsert_price_history(client, points)
            _log(client, provider.name, FetchType.PRICE, symbol, started, None)
        except ProviderError as exc:
            _log(client, provider.name, FetchType.PRICE, symbol, started, str(exc))
            failed.append(symbol)
    return failed


def refresh_fundamentals(
    client: Client,
    symbols: list[str],
    provider: FundamentalsDataProvider,
    as_of: date | None = None,
) -> list[str]:
    as_of = as_of or datetime.now(IST).date()
    dividend_from = as_of - timedelta(days=DIVIDEND_LOOKBACK_DAYS)
    failed: list[str] = []
    for symbol in symbols:
        started = datetime.now(IST)
        try:
            snapshot = _retry_provider_call(provider.get_fundamentals)(symbol, as_of)
            if snapshot is not None:
                fundamentals_repo.upsert_fundamental_snapshot(client, snapshot)
            _log(client, provider.name, FetchType.FUNDAMENTALS, symbol, started, None)
        except ProviderError as exc:
            _log(client, provider.name, FetchType.FUNDAMENTALS, symbol, started, str(exc))
            failed.append(symbol)
            continue

        started = datetime.now(IST)
        try:
            events = _retry_provider_call(provider.get_dividend_history)(symbol, dividend_from, as_of)
            dividends_repo.upsert_dividend_events(client, events)
            _log(client, provider.name, FetchType.DIVIDEND, symbol, started, None)
        except ProviderError as exc:
            _log(client, provider.name, FetchType.DIVIDEND, symbol, started, str(exc))
            failed.append(symbol)
    return failed
