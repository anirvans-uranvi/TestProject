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


class TestOptionChainSummary:
    def _rows(self):
        return [
            {"strike_price": 100.0, "option_type": "CE", "last_price": 12.0, "open_interest": 500, "change_in_oi": 50, "volume": 30, "underlying_price": 101.0, "trade_date": date(2026, 7, 16)},
            {"strike_price": 100.0, "option_type": "PE", "last_price": 8.0, "open_interest": 700, "change_in_oi": -20, "volume": 40, "underlying_price": 101.0, "trade_date": date(2026, 7, 16)},
            {"strike_price": 110.0, "option_type": "CE", "last_price": 5.0, "open_interest": 300, "change_in_oi": 10, "volume": 15, "underlying_price": 101.0, "trade_date": date(2026, 7, 16)},
            {"strike_price": 110.0, "option_type": "PE", "last_price": 14.0, "open_interest": 200, "change_in_oi": 5, "volume": 12, "underlying_price": 101.0, "trade_date": date(2026, 7, 16)},
        ]

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

    def test_prefers_freshest_trade_date_over_pure_nearest_strike(self):
        # spot 1000 -> target 950. Strike 950 is the literal nearest
        # match but hasn't traded since 2026-07-01 (illiquid); strike 900
        # is farther from target but is the only strike from the
        # freshest trade_date (2026-07-20), so it must win instead.
        rows = [
            {"symbol": "RELIANCE", "option_type": "PE", "strike_price": 900.0, "expiry_date": "2026-07-28", "last_price": 5.0, "trade_date": "2026-07-20"},
            {"symbol": "RELIANCE", "option_type": "PE", "strike_price": 950.0, "expiry_date": "2026-07-28", "last_price": 25.0, "trade_date": "2026-07-01"},
        ]
        result = fo_service.csp_5pct_map(rows, {"RELIANCE": 1000.0})
        assert result["RELIANCE"]["strike"] == 900.0
        assert result["RELIANCE"]["put_price"] == 5.0

    def test_no_trade_date_at_all_falls_back_to_pure_nearest_strike(self):
        # self._rows() carries no trade_date key on any row -- with no
        # staleness signal to go on, behavior is unchanged from before
        # freshness preference existed.
        result = fo_service.csp_5pct_map(self._rows(), {"RELIANCE": 1000.0})
        assert result["RELIANCE"]["strike"] == 950.0


class TestCsp5PctForRows:
    """The single-expiry core csp_5pct_map delegates to -- used directly
    by the Options screen for its near/next/far month CSP rows."""

    def test_matches_csp_5pct_map_for_the_same_single_expiry(self):
        rows = [
            {"symbol": "RELIANCE", "option_type": "PE", "strike_price": 900.0, "expiry_date": "2026-07-28", "last_price": 5.0},
            {"symbol": "RELIANCE", "option_type": "PE", "strike_price": 950.0, "expiry_date": "2026-07-28", "last_price": 25.0},
        ]
        result = fo_service.csp_5pct_for_rows(rows, spot=1000.0, expiry_date="2026-07-28")
        assert result["strike"] == 950.0
        assert result["put_price"] == 25.0
        assert abs(result["csp_pct"] - (25.0 / 950.0 * 100)) < 1e-9
        assert result["spot"] == 1000.0
        assert result["expiry_date"] == "2026-07-28"

    def test_echoes_back_the_expiry_date_argument_not_a_row_field(self):
        # a caller passes rows already filtered to one expiry -- the
        # returned expiry_date is exactly what was passed in, not
        # inferred from the rows themselves.
        rows = [{"symbol": "RELIANCE", "option_type": "PE", "strike_price": 950.0, "expiry_date": "2026-08-25", "last_price": 25.0}]
        result = fo_service.csp_5pct_for_rows(rows, spot=1000.0, expiry_date="2026-08-25")
        assert result["expiry_date"] == "2026-08-25"

    def test_no_pe_rows_returns_none(self):
        rows = [{"symbol": "RELIANCE", "option_type": "CE", "strike_price": 950.0, "expiry_date": "2026-07-28", "last_price": 60.0}]
        assert fo_service.csp_5pct_for_rows(rows, spot=1000.0, expiry_date="2026-07-28") is None

    def test_empty_rows_returns_none(self):
        assert fo_service.csp_5pct_for_rows([], spot=1000.0, expiry_date="2026-07-28") is None

    def test_prefers_freshest_trade_date(self):
        rows = [
            {"symbol": "RELIANCE", "option_type": "PE", "strike_price": 900.0, "expiry_date": "2026-07-28", "last_price": 5.0, "trade_date": "2026-07-20"},
            {"symbol": "RELIANCE", "option_type": "PE", "strike_price": 950.0, "expiry_date": "2026-07-28", "last_price": 25.0, "trade_date": "2026-07-01"},
        ]
        result = fo_service.csp_5pct_for_rows(rows, spot=1000.0, expiry_date="2026-07-28")
        assert result["strike"] == 900.0
        assert result["put_trade_date"] == "2026-07-20"


class TestCc5PctMap:
    EXPIRY = "2026-07-28"

    def _rows(self, symbol="RELIANCE"):
        # spot 1000 -> target 1050 (5% above) -> strike 1050 is an exact match
        return [
            {"symbol": symbol, "option_type": "CE", "strike_price": 1000.0, "expiry_date": self.EXPIRY, "last_price": 30.0},
            {"symbol": symbol, "option_type": "CE", "strike_price": 1050.0, "expiry_date": self.EXPIRY, "last_price": 15.0},
            {"symbol": symbol, "option_type": "CE", "strike_price": 1100.0, "expiry_date": self.EXPIRY, "last_price": 5.0},
            # a farther expiry that must NOT be used even for the same strike
            {"symbol": symbol, "option_type": "CE", "strike_price": 1050.0, "expiry_date": "2026-08-25", "last_price": 999.0},
        ]

    def test_finds_strike_nearest_5pct_above_spot(self):
        result = fo_service.cc_5pct_map(self._rows(), {"RELIANCE": 1000.0})
        assert result["RELIANCE"]["strike"] == 1050.0

    def test_computes_cc_pct_and_assignment_profit_pct(self):
        result = fo_service.cc_5pct_map(self._rows(), {"RELIANCE": 1000.0})
        assert abs(result["RELIANCE"]["premium"] - 15.0) < 1e-9
        assert abs(result["RELIANCE"]["gross_investment"] - 1000.0) < 1e-9
        assert abs(result["RELIANCE"]["net_investment"] - 985.0) < 1e-9  # 1000 - 15
        assert abs(result["RELIANCE"]["cc_pct"] - (15.0 / 1000.0 * 100)) < 1e-9
        assert abs(result["RELIANCE"]["assignment_profit_pct"] - ((1050.0 / 985.0 - 1) * 100)) < 1e-9

    def test_restricts_to_nearest_expiry_only(self):
        # far-expiry leg is priced at 999 -- if it leaked in, premium
        # would be wildly different
        result = fo_service.cc_5pct_map(self._rows(), {"RELIANCE": 1000.0})
        assert result["RELIANCE"]["premium"] == 15.0

    def test_symbol_without_spot_is_excluded(self):
        result = fo_service.cc_5pct_map(self._rows(), {})
        assert "RELIANCE" not in result

    def test_pe_rows_are_ignored_even_if_mixed_in(self):
        rows = self._rows() + [{"symbol": "RELIANCE", "option_type": "PE", "strike_price": 1050.0, "expiry_date": self.EXPIRY, "last_price": 999.0}]
        result = fo_service.cc_5pct_map(rows, {"RELIANCE": 1000.0})
        assert result["RELIANCE"]["premium"] == 15.0  # unaffected by the PE row

    def test_prefers_freshest_trade_date_over_pure_nearest_strike(self):
        # spot 1000 -> target 1050. Strike 1050 is the literal nearest
        # match but hasn't traded since 2026-07-01 (illiquid); strike
        # 1100 is farther from target but is the only strike from the
        # freshest trade_date (2026-07-20), so it must win instead.
        rows = [
            {"symbol": "RELIANCE", "option_type": "CE", "strike_price": 1050.0, "expiry_date": self.EXPIRY, "last_price": 15.0, "trade_date": "2026-07-01"},
            {"symbol": "RELIANCE", "option_type": "CE", "strike_price": 1100.0, "expiry_date": self.EXPIRY, "last_price": 5.0, "trade_date": "2026-07-20"},
        ]
        result = fo_service.cc_5pct_map(rows, {"RELIANCE": 1000.0})
        assert result["RELIANCE"]["strike"] == 1100.0
        assert result["RELIANCE"]["premium"] == 5.0

    def test_no_trade_date_at_all_falls_back_to_pure_nearest_strike(self):
        # self._rows() carries no trade_date key on any row -- with no
        # staleness signal to go on, behavior is unchanged from before
        # freshness preference existed.
        result = fo_service.cc_5pct_map(self._rows(), {"RELIANCE": 1000.0})
        assert result["RELIANCE"]["strike"] == 1050.0


class TestCc5PctForRows:
    """The single-expiry core cc_5pct_map delegates to -- used by the
    Dashboard's precomputed cache to store a near/next/far row per
    symbol (see TestDashboardMetricsRows below), and by the Options
    screen for its live "5% CC" breakdown."""

    def test_matches_cc_5pct_map_for_the_same_single_expiry(self):
        rows = [
            {"symbol": "RELIANCE", "option_type": "CE", "strike_price": 1000.0, "expiry_date": "2026-07-28", "last_price": 30.0},
            {"symbol": "RELIANCE", "option_type": "CE", "strike_price": 1050.0, "expiry_date": "2026-07-28", "last_price": 15.0},
        ]
        result = fo_service.cc_5pct_for_rows(rows, spot=1000.0, expiry_date="2026-07-28")
        assert result["strike"] == 1050.0
        assert result["premium"] == 15.0
        assert abs(result["gross_investment"] - 1000.0) < 1e-9
        assert abs(result["net_investment"] - 985.0) < 1e-9  # 1000 - 15
        assert abs(result["cc_pct"] - (15.0 / 1000.0 * 100)) < 1e-9
        assert abs(result["assignment_profit_pct"] - ((1050.0 / 985.0 - 1) * 100)) < 1e-9
        assert result["spot"] == 1000.0
        assert result["expiry_date"] == "2026-07-28"

    def test_picks_lowest_strike_at_or_above_5pct_otm_not_merely_nearest(self):
        # spot 1000 -> target 1050. 1040 is nearer to target in absolute
        # distance (10 vs 30) but falls BELOW the 5% OTM line, so it must
        # lose to 1080 -- the lowest strike that actually clears "5% or
        # more OTM".
        rows = [
            {"symbol": "RELIANCE", "option_type": "CE", "strike_price": 1040.0, "expiry_date": "2026-07-28", "last_price": 20.0},
            {"symbol": "RELIANCE", "option_type": "CE", "strike_price": 1080.0, "expiry_date": "2026-07-28", "last_price": 8.0},
        ]
        result = fo_service.cc_5pct_for_rows(rows, spot=1000.0, expiry_date="2026-07-28")
        assert result["strike"] == 1080.0
        assert result["premium"] == 8.0

    def test_echoes_back_the_expiry_date_argument_not_a_row_field(self):
        rows = [{"symbol": "RELIANCE", "option_type": "CE", "strike_price": 1050.0, "expiry_date": "2026-08-25", "last_price": 15.0}]
        result = fo_service.cc_5pct_for_rows(rows, spot=1000.0, expiry_date="2026-08-25")
        assert result["expiry_date"] == "2026-08-25"

    def test_no_call_rows_returns_none(self):
        rows = [{"symbol": "RELIANCE", "option_type": "PE", "strike_price": 1050.0, "expiry_date": "2026-07-28", "last_price": 15.0}]
        assert fo_service.cc_5pct_for_rows(rows, spot=1000.0, expiry_date="2026-07-28") is None

    def test_empty_rows_returns_none(self):
        assert fo_service.cc_5pct_for_rows([], spot=1000.0, expiry_date="2026-07-28") is None

    def test_falls_back_to_highest_available_strike_when_none_reach_5pct_otm(self):
        # spot 1000 -> target 1050, but the highest strike listed is only
        # 1000 (0% OTM) -- no strike genuinely satisfies "5% OTM or more",
        # so the highest available strike is used instead of picking
        # whichever is merely nearest to the unreachable target.
        rows = [
            {"symbol": "RELIANCE", "option_type": "CE", "strike_price": 950.0, "expiry_date": "2026-07-28", "last_price": 40.0},
            {"symbol": "RELIANCE", "option_type": "CE", "strike_price": 1000.0, "expiry_date": "2026-07-28", "last_price": 30.0},
        ]
        result = fo_service.cc_5pct_for_rows(rows, spot=1000.0, expiry_date="2026-07-28")
        assert result["strike"] == 1000.0

    def test_assignment_profit_pct_is_none_when_premium_at_least_covers_spot(self):
        # net_investment = gross_investment (spot) - premium; a premium
        # this large would make net_investment <= 0, which is
        # mathematically undefined for the assignment-profit ratio.
        rows = [{"symbol": "RELIANCE", "option_type": "CE", "strike_price": 1050.0, "expiry_date": "2026-07-28", "last_price": 1000.0}]
        result = fo_service.cc_5pct_for_rows(rows, spot=1000.0, expiry_date="2026-07-28")
        assert result["net_investment"] == 0.0
        assert result["assignment_profit_pct"] is None

    def test_prefers_freshest_trade_date(self):
        rows = [
            {"symbol": "RELIANCE", "option_type": "CE", "strike_price": 1050.0, "expiry_date": "2026-07-28", "last_price": 15.0, "trade_date": "2026-07-01"},
            {"symbol": "RELIANCE", "option_type": "CE", "strike_price": 1100.0, "expiry_date": "2026-07-28", "last_price": 5.0, "trade_date": "2026-07-20"},
        ]
        result = fo_service.cc_5pct_for_rows(rows, spot=1000.0, expiry_date="2026-07-28")
        assert result["strike"] == 1100.0
        assert result["trade_date"] == "2026-07-20"


class TestClassifyIndexExpiryTerms:
    def test_weekly_and_monthly_cadence_gives_three_distinct_terms(self):
        # NIFTY/SENSEX-shaped: weekly expiries plus a further-out monthly
        # one -- all three land on distinct dates.
        expiries = [date(2026, 9, 8), date(2026, 9, 15), date(2026, 9, 22), date(2026, 9, 29), date(2026, 10, 27)]
        result = fo_service.classify_index_expiry_terms(expiries, today=date(2026, 9, 2))
        assert result == {
            "current_week": date(2026, 9, 8),
            "next_week": date(2026, 9, 15),
            "current_month": date(2026, 9, 29),
        }

    def test_monthly_only_cadence_current_week_and_month_coincide(self):
        # BANKNIFTY/FINNIFTY-shaped: no weekly cadence at all -- the
        # soonest expiry is both "current week" and "current month".
        expiries = [date(2026, 9, 29), date(2026, 10, 27), date(2026, 11, 23)]
        result = fo_service.classify_index_expiry_terms(expiries, today=date(2026, 9, 2))
        assert result["current_week"] == date(2026, 9, 29)
        assert result["current_month"] == date(2026, 9, 29)
        assert result["next_week"] == date(2026, 10, 27)

    def test_already_past_expiry_is_excluded(self):
        # Confirmed live: option_contracts.is_open doesn't reliably flip
        # the day after an expiry passes -- classify_index_expiry_terms
        # must filter to >= today itself regardless of what the caller's
        # own "open" list already excluded (or didn't).
        expiries = [date(2026, 9, 1), date(2026, 9, 8), date(2026, 9, 15)]
        result = fo_service.classify_index_expiry_terms(expiries, today=date(2026, 9, 2))
        assert result["current_week"] == date(2026, 9, 8)

    def test_no_future_expiries_returns_empty(self):
        expiries = [date(2026, 9, 1)]
        assert fo_service.classify_index_expiry_terms(expiries, today=date(2026, 9, 2)) == {}

    def test_empty_input_returns_empty(self):
        assert fo_service.classify_index_expiry_terms([], today=date(2026, 9, 2)) == {}

    def test_single_remaining_expiry_has_no_next_week(self):
        expiries = [date(2026, 9, 29)]
        result = fo_service.classify_index_expiry_terms(expiries, today=date(2026, 9, 2))
        assert result == {"current_week": date(2026, 9, 29), "current_month": date(2026, 9, 29)}

    def test_current_month_rolls_forward_once_this_months_expiry_has_passed(self):
        # today is already past every September expiry -- "current month"
        # rolls to October, the soonest remaining expiry's own month.
        expiries = [date(2026, 10, 27), date(2026, 11, 23)]
        result = fo_service.classify_index_expiry_terms(expiries, today=date(2026, 9, 30))
        assert result["current_month"] == date(2026, 10, 27)


class TestIndexStrangleForExpiry:
    EXPIRY = "2026-09-29"

    def _rows(self, spot=1000.0, lot_size=50):
        return [
            {"option_type": "PE", "strike_price": 950.0, "expiry_date": self.EXPIRY, "last_price": 10.0, "underlying_price": spot, "lot_size": lot_size},
            {"option_type": "PE", "strike_price": 900.0, "expiry_date": self.EXPIRY, "last_price": 5.0, "underlying_price": spot, "lot_size": lot_size},
            {"option_type": "CE", "strike_price": 1050.0, "expiry_date": self.EXPIRY, "last_price": 12.0, "underlying_price": spot, "lot_size": lot_size},
            {"option_type": "CE", "strike_price": 1100.0, "expiry_date": self.EXPIRY, "last_price": 6.0, "underlying_price": spot, "lot_size": lot_size},
        ]

    def test_picks_nearest_strike_to_target_on_each_side(self):
        # spot 1000, pct=5 -> PE target 950 (exact match), CE target 1050 (exact match)
        result = fo_service.index_strangle_for_expiry(self._rows(), pct=5.0)
        assert result["pe_strike"] == 950.0
        assert result["pe_premium"] == 10.0
        assert result["ce_strike"] == 1050.0
        assert result["ce_premium"] == 12.0

    def test_nearest_is_not_a_floor_filter_unlike_cc_5pct(self):
        # target 1050 sits exactly between no strikes here, but confirms
        # "nearest" picks the closer one on either side, not "lowest
        # strike still at or above target" the way cc_5pct_for_rows does.
        rows = [
            {"option_type": "PE", "strike_price": 940.0, "expiry_date": self.EXPIRY, "last_price": 8.0, "underlying_price": 1000.0, "lot_size": 50},
            {"option_type": "CE", "strike_price": 1040.0, "expiry_date": self.EXPIRY, "last_price": 14.0, "underlying_price": 1000.0, "lot_size": 50},
            {"option_type": "CE", "strike_price": 1200.0, "expiry_date": self.EXPIRY, "last_price": 1.0, "underlying_price": 1000.0, "lot_size": 50},
        ]
        # pct=4 -> CE target 1040 -- nearer strike (1040, exact) must win
        # over the far one (1200), even though 1200 is "more OTM."
        result = fo_service.index_strangle_for_expiry(rows, pct=4.0)
        assert result["ce_strike"] == 1040.0

    def test_credit_per_unit_and_total(self):
        result = fo_service.index_strangle_for_expiry(self._rows(), pct=5.0)
        assert result["credit_per_unit"] == 22.0  # 10.0 + 12.0
        assert result["credit_total"] == 1100.0  # 22.0 * 50

    def test_spot_is_echoed_back(self):
        result = fo_service.index_strangle_for_expiry(self._rows(spot=24000.0), pct=3.0)
        assert result["spot"] == 24000.0

    def test_no_pe_rows_returns_none(self):
        rows = [r for r in self._rows() if r["option_type"] == "CE"]
        assert fo_service.index_strangle_for_expiry(rows, pct=5.0) is None

    def test_no_ce_rows_returns_none(self):
        rows = [r for r in self._rows() if r["option_type"] == "PE"]
        assert fo_service.index_strangle_for_expiry(rows, pct=5.0) is None

    def test_no_spot_returns_none(self):
        rows = [{**r, "underlying_price": None} for r in self._rows()]
        assert fo_service.index_strangle_for_expiry(rows, pct=5.0) is None

    def test_empty_rows_returns_none(self):
        assert fo_service.index_strangle_for_expiry([], pct=5.0) is None

    def test_missing_lot_size_makes_credit_total_none_but_not_credit_per_unit(self):
        rows = [{**r, "lot_size": None} for r in self._rows()]
        result = fo_service.index_strangle_for_expiry(rows, pct=5.0)
        assert result["credit_per_unit"] == 22.0
        assert result["credit_total"] is None

    def test_prefers_freshest_trade_date_over_pure_nearest_strike(self):
        # spot 1000, pct=5 -> PE target 950. Strike 950 is the literal
        # nearest match but hasn't traded since 2026-08-01 (illiquid);
        # strike 940 is farther from target but is the only PE strike
        # from the freshest trade_date (2026-09-01), so it must win
        # instead -- mirrors cc_5pct_for_rows' own documented behavior.
        rows = [
            {"option_type": "PE", "strike_price": 950.0, "expiry_date": self.EXPIRY, "last_price": 999.0, "underlying_price": 1000.0, "lot_size": 50, "trade_date": "2026-08-01"},
            {"option_type": "PE", "strike_price": 940.0, "expiry_date": self.EXPIRY, "last_price": 8.0, "underlying_price": 1000.0, "lot_size": 50, "trade_date": "2026-09-01"},
            {"option_type": "CE", "strike_price": 1050.0, "expiry_date": self.EXPIRY, "last_price": 12.0, "underlying_price": 1000.0, "lot_size": 50, "trade_date": "2026-09-01"},
        ]
        result = fo_service.index_strangle_for_expiry(rows, pct=5.0)
        assert result["pe_strike"] == 940.0
        assert result["pe_premium"] == 8.0


class TestCoveredCallForHolding:
    """The Portfolio page's per-holding covered-call suggestion --
    distinct from TestCc5PctForRows above (spot-based, fixed 5% OTM,
    floor-filtered strike). Here the target depends on whether the
    position is under water, and the strike is nearest-match, not
    floor-filtered."""

    def test_loss_position_targets_3pct_above_avg_price(self):
        # avg_price 100 > ltp 90 (a loss) -> target = 100 * 1.03 = 103.
        # 105 (distance 2) beats 100 (distance 3).
        rows = [
            {"symbol": "RELIANCE", "option_type": "CE", "strike_price": 100.0, "expiry_date": "2026-07-28", "last_price": 6.0, "lot_size": 10},
            {"symbol": "RELIANCE", "option_type": "CE", "strike_price": 105.0, "expiry_date": "2026-07-28", "last_price": 4.0, "lot_size": 10},
        ]
        result = fo_service.covered_call_for_holding(rows, avg_price=100.0, ltp=90.0, qty=50.0, expiry_date="2026-07-28")
        assert result["strike"] == 105.0
        assert result["target_base"] == 100.0
        assert result["target_pct"] == 0.03

    def test_profit_or_breakeven_position_targets_5pct_above_ltp(self):
        # ltp 100 >= avg_price 90 -> target = 100 * 1.05 = 105 (exact match).
        rows = [
            {"symbol": "RELIANCE", "option_type": "CE", "strike_price": 100.0, "expiry_date": "2026-07-28", "last_price": 5.0, "lot_size": 10},
            {"symbol": "RELIANCE", "option_type": "CE", "strike_price": 105.0, "expiry_date": "2026-07-28", "last_price": 3.0, "lot_size": 10},
        ]
        result = fo_service.covered_call_for_holding(rows, avg_price=90.0, ltp=100.0, qty=20.0, expiry_date="2026-07-28")
        assert result["strike"] == 105.0
        assert result["target_base"] == 100.0
        assert result["target_pct"] == 0.05

    def test_computes_invested_amount_premium_collected_and_both_rois(self):
        rows = [{"symbol": "RELIANCE", "option_type": "CE", "strike_price": 105.0, "expiry_date": "2026-07-28", "last_price": 3.0, "lot_size": 10}]
        result = fo_service.covered_call_for_holding(rows, avg_price=90.0, ltp=100.0, qty=20.0, expiry_date="2026-07-28")
        assert result["invested_amount"] == 1800.0  # 90 * 20
        assert result["premium_collected"] == 30.0  # 3 * 10
        assert abs(result["cc_roi_pct"] - (30.0 / 1800.0 * 100)) < 1e-9
        # (30 + 105*20 - 1800) / 1800 * 100 == (30 + 2100 - 1800) / 1800 * 100
        assert abs(result["assignment_roi_pct"] - (330.0 / 1800.0 * 100)) < 1e-9

    def test_assignment_roi_can_be_negative(self):
        # avg_price 100 > ltp 50 (a big loss) -> target = 103. Even
        # getting called away at 105 barely offsets the loss.
        rows = [{"symbol": "RELIANCE", "option_type": "CE", "strike_price": 105.0, "expiry_date": "2026-07-28", "last_price": 1.0, "lot_size": 10}]
        result = fo_service.covered_call_for_holding(rows, avg_price=100.0, ltp=50.0, qty=100.0, expiry_date="2026-07-28")
        # invested = 100*100 = 10000; premium = 1*10 = 10; sale = 105*100 = 10500
        # assignment_roi = (10 + 10500 - 10000) / 10000 * 100 = 5.1%
        assert result["assignment_roi_pct"] > 0
        # but a deeper loss should be able to go negative
        result2 = fo_service.covered_call_for_holding(rows, avg_price=200.0, ltp=50.0, qty=100.0, expiry_date="2026-07-28")
        assert result2["assignment_roi_pct"] < 0

    def test_none_when_qty_is_zero(self):
        rows = [{"symbol": "RELIANCE", "option_type": "CE", "strike_price": 105.0, "expiry_date": "2026-07-28", "last_price": 3.0, "lot_size": 10}]
        assert fo_service.covered_call_for_holding(rows, avg_price=90.0, ltp=100.0, qty=0.0, expiry_date="2026-07-28") is None

    def test_none_when_ltp_is_none_or_not_positive(self):
        rows = [{"symbol": "RELIANCE", "option_type": "CE", "strike_price": 105.0, "expiry_date": "2026-07-28", "last_price": 3.0, "lot_size": 10}]
        assert fo_service.covered_call_for_holding(rows, avg_price=90.0, ltp=None, qty=20.0, expiry_date="2026-07-28") is None
        assert fo_service.covered_call_for_holding(rows, avg_price=90.0, ltp=0.0, qty=20.0, expiry_date="2026-07-28") is None

    def test_none_when_no_ce_rows(self):
        rows = [{"symbol": "RELIANCE", "option_type": "PE", "strike_price": 105.0, "expiry_date": "2026-07-28", "last_price": 3.0, "lot_size": 10}]
        assert fo_service.covered_call_for_holding(rows, avg_price=90.0, ltp=100.0, qty=20.0, expiry_date="2026-07-28") is None

    def test_prefers_freshest_trade_date_over_pure_nearest_strike(self):
        # target 105 (ltp 100 * 1.05). Strike 105 is the literal nearest
        # match but hasn't traded since 2026-07-01 (illiquid); strike 110
        # is farther but is the only strike from the freshest trade_date.
        rows = [
            {"symbol": "RELIANCE", "option_type": "CE", "strike_price": 105.0, "expiry_date": "2026-07-28", "last_price": 3.0, "lot_size": 10, "trade_date": "2026-07-01"},
            {"symbol": "RELIANCE", "option_type": "CE", "strike_price": 110.0, "expiry_date": "2026-07-28", "last_price": 1.0, "lot_size": 10, "trade_date": "2026-07-20"},
        ]
        result = fo_service.covered_call_for_holding(rows, avg_price=90.0, ltp=100.0, qty=20.0, expiry_date="2026-07-28")
        assert result["strike"] == 110.0
        assert result["trade_date"] == "2026-07-20"


class TestDashboardMetricsRows:
    """dashboard_metrics_rows fans out per symbol over its up to 3
    nearest distinct expiries (near/next/far), computing
    csp_5pct_for_rows + cc_5pct_for_rows (both already covered above)
    once per expiry, and returns one flat row per (symbol, expiry) --
    the shape dashboard_fo_metrics (migration 0011) stores. These tests
    focus on the fan-out/merge, not re-deriving the underlying
    strike-selection math."""

    def _rows_for_expiry(self, symbol, expiry):
        return [
            {"symbol": symbol, "option_type": "CE", "strike_price": 1000.0, "expiry_date": expiry, "last_price": 30.0},
            {"symbol": symbol, "option_type": "CE", "strike_price": 1050.0, "expiry_date": expiry, "last_price": 15.0},
            {"symbol": symbol, "option_type": "PE", "strike_price": 900.0, "expiry_date": expiry, "last_price": 5.0},
            {"symbol": symbol, "option_type": "PE", "strike_price": 950.0, "expiry_date": expiry, "last_price": 25.0},
        ]

    def test_merges_csp_and_cc_for_one_expiry(self):
        rows = fo_service.dashboard_metrics_rows(self._rows_for_expiry("RELIANCE", "2026-07-28"), {"RELIANCE": 1000.0})
        assert len(rows) == 1
        row = rows[0]
        assert row["symbol"] == "RELIANCE"
        assert row["expiry_date"] == "2026-07-28"
        assert row["spot"] == 1000.0
        assert row["csp_strike"] == 950.0
        assert row["csp_put_price"] == 25.0
        assert abs(row["csp_pct"] - (25.0 / 950.0 * 100)) < 1e-9
        assert row["cc_strike"] == 1050.0
        assert row["cc_premium"] == 15.0
        assert abs(row["cc_pct"] - (15.0 / 1000.0 * 100)) < 1e-9

    def test_up_to_three_nearest_expiries_get_a_row_each(self):
        rows = (
            self._rows_for_expiry("RELIANCE", "2026-07-28")
            + self._rows_for_expiry("RELIANCE", "2026-08-25")
            + self._rows_for_expiry("RELIANCE", "2026-09-29")
        )
        result = fo_service.dashboard_metrics_rows(rows, {"RELIANCE": 1000.0})
        assert len(result) == 3
        assert {r["expiry_date"] for r in result} == {"2026-07-28", "2026-08-25", "2026-09-29"}

    def test_a_fourth_farther_expiry_does_not_get_a_row(self):
        rows = (
            self._rows_for_expiry("RELIANCE", "2026-07-28")
            + self._rows_for_expiry("RELIANCE", "2026-08-25")
            + self._rows_for_expiry("RELIANCE", "2026-09-29")
            + self._rows_for_expiry("RELIANCE", "2026-10-27")
        )
        result = fo_service.dashboard_metrics_rows(rows, {"RELIANCE": 1000.0})
        assert len(result) == 3
        assert "2026-10-27" not in {r["expiry_date"] for r in result}

    def test_symbol_with_no_option_data_gets_zero_rows(self):
        assert fo_service.dashboard_metrics_rows([], {"RELIANCE": 1000.0}) == []

    def test_symbol_without_spot_gets_zero_rows_even_with_option_data(self):
        rows = fo_service.dashboard_metrics_rows(self._rows_for_expiry("RELIANCE", "2026-07-28"), {"RELIANCE": None})
        assert rows == []

    def test_csp_and_cc_degrade_independently_within_a_row(self):
        # no PE row at all -> csp_5pct_for_rows returns None, but
        # cc_5pct_for_rows only needs CE legs, so it still succeeds --
        # this documents that a missing half degrades independently
        # rather than one missing leg blanking the whole row.
        rows = [r for r in self._rows_for_expiry("RELIANCE", "2026-07-28") if r["option_type"] != "PE"]
        result = fo_service.dashboard_metrics_rows(rows, {"RELIANCE": 1000.0})
        assert len(result) == 1
        assert result[0]["csp_pct"] is None
        assert result[0]["cc_pct"] is not None

    def test_multiple_symbols_each_get_their_own_rows(self):
        rows = self._rows_for_expiry("RELIANCE", "2026-07-28") + self._rows_for_expiry("TCS", "2026-07-28")
        result = fo_service.dashboard_metrics_rows(rows, {"RELIANCE": 1000.0, "TCS": 1000.0})
        symbols = {r["symbol"] for r in result}
        assert symbols == {"RELIANCE", "TCS"}

    def test_bse_sourced_legs_are_excluded(self):
        # A stale, pre-restriction BSE contract for a real stock symbol,
        # a few days off its real NSE monthly expiry -- BSE's F&O feed is
        # index-options only, so this leg should never produce a row (it
        # was the cause of a duplicate same-month dropdown entry).
        bse_rows = [{**r, "source": "bse_fo_bhavcopy"} for r in self._rows_for_expiry("RELIANCE", "2026-07-31")]
        nse_rows = [{**r, "source": "nse_fo_bhavcopy"} for r in self._rows_for_expiry("RELIANCE", "2026-07-28")]
        result = fo_service.dashboard_metrics_rows(bse_rows + nse_rows, {"RELIANCE": 1000.0})
        assert {r["expiry_date"] for r in result} == {"2026-07-28"}

    def test_bse_sourced_legs_excluded_even_with_edge_suffix(self):
        bse_rows = [{**r, "source": "bse_fo_bhavcopy_edge"} for r in self._rows_for_expiry("RELIANCE", "2026-07-28")]
        assert fo_service.dashboard_metrics_rows(bse_rows, {"RELIANCE": 1000.0}) == []

    def test_rows_without_a_source_key_pass_through_unaffected(self):
        # Older fixtures / the mock provider's single "mock_fo" source
        # never set "source" to a BSE-prefixed value -- must not be
        # accidentally excluded.
        result = fo_service.dashboard_metrics_rows(self._rows_for_expiry("RELIANCE", "2026-07-28"), {"RELIANCE": 1000.0})
        assert len(result) == 1
