from datetime import date, timedelta

from src.data_providers.mock_provider import MockFundamentalsProvider, MockPriceProvider


class TestMockPriceProvider:
    def setup_method(self):
        self.provider = MockPriceProvider()

    def test_historical_daily_covers_trading_days_only(self):
        from_date = date(2026, 7, 1)  # Wed
        to_date = date(2026, 7, 7)  # Tue (spans a weekend)
        points = self.provider.get_historical_daily("TCS", from_date, to_date)
        weekdays = {p.trade_date.weekday() for p in points}
        assert weekdays.issubset({0, 1, 2, 3, 4})
        assert len(points) == 5  # Wed,Thu,Fri,Mon,Tue

    def test_historical_daily_deterministic_for_same_symbol(self):
        p1 = self.provider.get_historical_daily("INFY", date(2026, 1, 1), date(2026, 1, 31))
        p2 = self.provider.get_historical_daily("INFY", date(2026, 1, 1), date(2026, 1, 31))
        assert [p.close for p in p1] == [p.close for p in p2]

    def test_different_symbols_produce_different_series(self):
        p1 = self.provider.get_historical_daily("TCS", date(2026, 1, 1), date(2026, 1, 31))
        p2 = self.provider.get_historical_daily("INFY", date(2026, 1, 1), date(2026, 1, 31))
        assert [p.close for p in p1] != [p.close for p in p2]

    def test_prices_are_positive(self):
        points = self.provider.get_historical_daily("RELIANCE", date(2025, 1, 1), date(2026, 1, 1))
        assert all(p.close > 0 for p in points)

    def test_get_quote_returns_symbol_and_price(self):
        quote = self.provider.get_quote("HDFCBANK")
        assert quote.symbol == "HDFCBANK"
        assert quote.latest_price > 0

    def test_get_quotes_batch(self):
        quotes = self.provider.get_quotes(["TCS", "INFY", "WIPRO"])
        assert set(quotes.keys()) == {"TCS", "INFY", "WIPRO"}


class TestMockFundamentalsProvider:
    def setup_method(self):
        self.provider = MockFundamentalsProvider()

    def test_get_fundamentals_returns_positive_values(self):
        snap = self.provider.get_fundamentals("TCS", date(2026, 7, 11))
        assert snap is not None
        assert snap.pe_ratio > 0
        assert snap.peg_ratio > 0
        assert snap.is_stale is False

    def test_get_dividend_history_within_window(self):
        events = self.provider.get_dividend_history("TCS", date(2025, 1, 1), date(2026, 1, 1))
        assert all(date(2025, 1, 1) <= e.ex_date <= date(2026, 1, 1) for e in events)
