from __future__ import annotations

from src.config import Settings, get_settings
from src.data_providers.base import FundamentalsDataProvider, PriceDataProvider
from src.data_providers.dhan_provider import DhanProvider
from src.data_providers.manual_fundamentals_provider import ManualFundamentalsProvider
from src.data_providers.mock_provider import MockFundamentalsProvider, MockPriceProvider


def get_price_provider(settings: Settings | None = None) -> PriceDataProvider:
    settings = settings or get_settings()
    if settings.market_data_provider == "dhan":
        return DhanProvider(client_id=settings.dhan_client_id, access_token=settings.dhan_access_token)
    return MockPriceProvider()


def get_fundamentals_provider(settings: Settings | None = None) -> FundamentalsDataProvider:
    settings = settings or get_settings()
    if settings.fundamentals_provider == "manual":
        return ManualFundamentalsProvider()
    return MockFundamentalsProvider()
