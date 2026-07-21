import types
from datetime import date

from src.data_providers.mock_provider import MockFOProvider
from src.services import fo_service


# ---------------------------------------------------------------------
# Minimal fake Supabase client: records upsert payloads per table so the
# ingest path can be exercised without a live database (matches how the
# rest of the suite avoids network).
# ---------------------------------------------------------------------
class _FakeTable:
    def __init__(self, store, name):
        self.store = store
        self.name = name

    def upsert(self, payload, on_conflict=None):
        self.store.setdefault(self.name, []).extend(payload)
        return self

    def update(self, values):
        return self

    def gte(self, *args, **kwargs):
        return self

    def lt(self, *args, **kwargs):
        return self

    def execute(self):
        return types.SimpleNamespace(data=[])


class _FakeClient:
    def __init__(self):
        self.store = {}

    def table(self, name):
        return _FakeTable(self.store, name)


class TestIngestFoDay:
    def test_ingests_all_four_tables(self):
        book = MockFOProvider().fetch_day(date(2026, 7, 16), universe={"RELIANCE", "TCS"})
        client = _FakeClient()
        counts = fo_service.ingest_fo_day(client, book)

        assert counts["futures_prices"] == len(book.futures_prices)
        assert counts["option_prices"] == len(book.option_prices)
        assert len(client.store["futures_daily_prices"]) == len(book.futures_prices)
        assert len(client.store["option_daily_prices"]) == len(book.option_prices)
        assert len(client.store["futures_contracts"]) == len(book.futures_contracts)
        assert len(client.store["option_contracts"]) == len(book.option_contracts)

    def test_two_symbols_three_expiries_of_futures(self):
        book = MockFOProvider().fetch_day(date(2026, 7, 16), universe={"RELIANCE", "TCS"})
        # 2 symbols * 3 monthly expiries
        assert len(book.futures_prices) == 6


class TestShapeOptionChain:
    def _rows(self):
        return [
            {"strike_price": 100.0, "option_type": "CE", "last_price": 12.0, "open_interest": 500, "change_in_oi": 50, "volume": 30, "underlying_price": 101.0, "trade_date": date(2026, 7, 16)},
            {"strike_price": 100.0, "option_type": "PE", "last_price": 8.0, "open_interest": 700, "change_in_oi": -20, "volume": 40, "underlying_price": 101.0, "trade_date": date(2026, 7, 16)},
            {"strike_price": 110.0, "option_type": "CE", "last_price": 5.0, "open_interest": 300, "change_in_oi": 10, "volume": 15, "underlying_price": 101.0, "trade_date": date(2026, 7, 16)},
            {"strike_price": 110.0, "option_type": "PE", "last_price": 14.0, "open_interest": 200, "change_in_oi": 5, "volume": 12, "underlying_price": 101.0, "trade_date": date(2026, 7, 16)},
        ]

    def test_pivots_ce_pe_per_strike(self):
        shaped = fo_service.shape_option_chain(self._rows())
        assert [r["strike"] for r in shaped] == [100.0, 110.0]
        row = shaped[0]
        assert row["ce_last"] == 12.0
        assert row["ce_oi"] == 500
        assert row["pe_last"] == 8.0
        assert row["pe_oi"] == 700

    def test_sorted_ascending_by_strike(self):
        rows = list(reversed(self._rows()))
        shaped = fo_service.shape_option_chain(rows)
        assert [r["strike"] for r in shaped] == [100.0, 110.0]

    def test_summary_spot_atm_pcr(self):
        summary = fo_service.option_chain_summary(self._rows())
        assert summary["spot"] == 101.0
        assert summary["atm_strike"] == 100.0  # closest strike to spot 101
        assert summary["total_ce_oi"] == 800  # 500 + 300
        assert summary["total_pe_oi"] == 900  # 700 + 200
        assert abs(summary["pcr"] - (900 / 800)) < 1e-9

    def test_summary_empty(self):
        assert fo_service.option_chain_summary([]) == {}

    def test_summary_ignores_stale_leg_for_date_and_spot(self):
        # Reproduces the real HDFCBANK bug: a deep-ITM/zero-OI contract
        # (620 CE) stopped appearing in NSE's bhavcopy after 2026-07-01,
        # while every other strike kept updating through 2026-07-17. The
        # "as of" date and spot must reflect the freshest data in the
        # chain, not whichever row happens to sort first by strike.
        rows = [
            {"strike_price": 620.0, "option_type": "CE", "open_interest": 0, "underlying_price": 700.0, "trade_date": date(2026, 7, 1)},
            {"strike_price": 650.0, "option_type": "CE", "open_interest": 500, "underlying_price": 796.15, "trade_date": date(2026, 7, 17)},
            {"strike_price": 650.0, "option_type": "PE", "open_interest": 300, "underlying_price": 796.15, "trade_date": date(2026, 7, 17)},
        ]
        summary = fo_service.option_chain_summary(rows)
        assert summary["trade_date"] == date(2026, 7, 17)
        assert summary["spot"] == 796.15
        # Stale leg's OI still counts toward the aggregate totals -- it's
        # the last known open interest for that contract, which is correct.
        assert summary["total_ce_oi"] == 500
        assert summary["total_pe_oi"] == 300


class TestFuturesTermStructure:
    def test_basis_and_sort(self):
        rows = [
            {"expiry_date": "2026-08-25", "last_price": 105.0, "settlement_price": 105.0, "underlying_price": 100.0, "open_interest": 10, "change_in_oi": 1, "volume": 5, "lot_size": 50},
            {"expiry_date": "2026-07-28", "last_price": 102.0, "settlement_price": 102.0, "underlying_price": 100.0, "open_interest": 20, "change_in_oi": 2, "volume": 8, "lot_size": 50},
        ]
        term = fo_service.futures_term_structure(rows)
        # sorted by expiry ascending
        assert [r["expiry_date"] for r in term] == ["2026-07-28", "2026-08-25"]
        assert term[0]["basis"] == 2.0  # 102 - 100
        assert term[1]["basis"] == 5.0  # 105 - 100


class TestCsp5PctMap:
    def _rows(self):
        return [
            {"symbol": "RELIANCE", "option_type": "PE", "strike_price": 900.0, "expiry_date": "2026-07-28", "last_price": 5.0},
            {"symbol": "RELIANCE", "option_type": "PE", "strike_price": 950.0, "expiry_date": "2026-07-28", "last_price": 25.0},
            {"symbol": "RELIANCE", "option_type": "PE", "strike_price": 1000.0, "expiry_date": "2026-07-28", "last_price": 60.0},
            # a farther expiry that must NOT be used even though it's closer in strike terms
            {"symbol": "RELIANCE", "option_type": "PE", "strike_price": 950.0, "expiry_date": "2026-08-25", "last_price": 40.0},
        ]

    def test_finds_strike_nearest_5pct_below_spot(self):
        # spot 1000 -> target 950 -> strike 950 is an exact match
        result = fo_service.csp_5pct_map(self._rows(), {"RELIANCE": 1000.0})
        assert result["RELIANCE"]["strike"] == 950.0

    def test_computes_premium_over_strike_percentage(self):
        result = fo_service.csp_5pct_map(self._rows(), {"RELIANCE": 1000.0})
        assert abs(result["RELIANCE"]["put_price"] - 25.0) < 1e-9
        assert abs(result["RELIANCE"]["csp_pct"] - (25.0 / 950.0 * 100)) < 1e-9

    def test_restricts_to_nearest_expiry_only(self):
        # the near (July) expiry's 950 strike (premium 25) must win, not the
        # farther (August) expiry's 950 strike (premium 40)
        result = fo_service.csp_5pct_map(self._rows(), {"RELIANCE": 1000.0})
        assert result["RELIANCE"]["put_price"] == 25.0

    def test_symbol_without_spot_is_excluded(self):
        result = fo_service.csp_5pct_map(self._rows(), {})
        assert "RELIANCE" not in result

    def test_ce_rows_are_ignored_even_if_mixed_in(self):
        rows = self._rows() + [{"symbol": "RELIANCE", "option_type": "CE", "strike_price": 950.0, "expiry_date": "2026-07-28", "last_price": 999.0}]
        result = fo_service.csp_5pct_map(rows, {"RELIANCE": 1000.0})
        assert result["RELIANCE"]["put_price"] == 25.0  # unaffected by the CE row


class TestItmPmcc5PctMap:
    EXPIRY = "2026-07-28"

    def _rows(self, symbol="RELIANCE"):
        # spot 1000 -> ITM CE closest to spot (strike < 1000) is 950;
        # 5% below 950 (902.5) is closest to strike 900.
        return [
            {"symbol": symbol, "option_type": "CE", "strike_price": 900.0, "expiry_date": self.EXPIRY, "last_price": 110.0},
            {"symbol": symbol, "option_type": "CE", "strike_price": 950.0, "expiry_date": self.EXPIRY, "last_price": 60.0},
            {"symbol": symbol, "option_type": "CE", "strike_price": 1000.0, "expiry_date": self.EXPIRY, "last_price": 20.0},
            {"symbol": symbol, "option_type": "PE", "strike_price": 900.0, "expiry_date": self.EXPIRY, "last_price": 5.0},
            {"symbol": symbol, "option_type": "PE", "strike_price": 950.0, "expiry_date": self.EXPIRY, "last_price": 25.0},
            {"symbol": symbol, "option_type": "PE", "strike_price": 1000.0, "expiry_date": self.EXPIRY, "last_price": 60.0},
            # a farther expiry that must NOT be used even for the same strikes
            {"symbol": symbol, "option_type": "CE", "strike_price": 950.0, "expiry_date": "2026-08-25", "last_price": 999.0},
            {"symbol": symbol, "option_type": "PE", "strike_price": 950.0, "expiry_date": "2026-08-25", "last_price": 999.0},
        ]

    def test_picks_itm_ce_closest_to_spot(self):
        result = fo_service.itm_pmcc_5pct_map(self._rows(), {"RELIANCE": 1000.0})
        assert result["RELIANCE"]["itm_ce_strike"] == 950.0

    def test_picks_ce_strike_nearest_5pct_below_the_itm_ce(self):
        # 5% below 950 is 902.5 -> nearest available CE strike is 900
        result = fo_service.itm_pmcc_5pct_map(self._rows(), {"RELIANCE": 1000.0})
        assert result["RELIANCE"]["otm_ce_strike"] == 900.0

    def test_net_credit_and_percentage(self):
        # net credit = PE(950) sell 25 + CE(900) sell 110 - CE(950) buy 60 = 75
        # pct = 75 / 950 * 100
        result = fo_service.itm_pmcc_5pct_map(self._rows(), {"RELIANCE": 1000.0})
        assert abs(result["RELIANCE"]["net_credit"] - 75.0) < 1e-9
        assert abs(result["RELIANCE"]["pmcc_pct"] - (75.0 / 950.0 * 100)) < 1e-9

    def test_restricts_to_nearest_expiry_only(self):
        # far-expiry legs are priced at 999 -- if they leaked in, net credit
        # would be wildly different
        result = fo_service.itm_pmcc_5pct_map(self._rows(), {"RELIANCE": 1000.0})
        assert abs(result["RELIANCE"]["net_credit"] - 75.0) < 1e-9

    def test_symbol_without_spot_is_excluded(self):
        result = fo_service.itm_pmcc_5pct_map(self._rows(), {})
        assert "RELIANCE" not in result

    def test_no_itm_ce_excludes_symbol(self):
        # spot below every CE strike -> no CE is ITM
        result = fo_service.itm_pmcc_5pct_map(self._rows(), {"RELIANCE": 850.0})
        assert "RELIANCE" not in result

    def test_missing_pe_at_itm_strike_excludes_symbol(self):
        rows = [r for r in self._rows() if not (r["option_type"] == "PE" and r["strike_price"] == 950.0 and r["expiry_date"] == self.EXPIRY)]
        result = fo_service.itm_pmcc_5pct_map(rows, {"RELIANCE": 1000.0})
        assert "RELIANCE" not in result
