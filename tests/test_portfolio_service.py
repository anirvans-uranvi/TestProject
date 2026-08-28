"""Tests for portfolio_service: broker API response translation
(Dhan/Zerodha live sync), instrument-string decoding, cross-broker
merging, and valuation math. CSV parsing was dropped once a live broker
sync (Settings' "Data Provider" section) became the only way to
populate holdings/positions -- see git history for the removed
parse_zerodha_csv/parse_dhan_csv/parse_zerodha_positions_csv/
parse_dhan_positions_csv tests."""
from datetime import date, datetime, timedelta

import pytest

from src.models.company import Company
from src.models.enums import CompanyType, OptionType
from src.models.portfolio import PortfolioTradeFill
from src.services import portfolio_service


class TestMergeHoldings:
    def test_sums_qty_and_investment_for_the_same_symbol_across_brokers(self):
        rows = [
            {"raw_name": "SBIN", "symbol": "SBIN", "qty": 1500, "avg_price": 900, "investment": 1350000},
            {"raw_name": "State Bank of India", "symbol": "SBIN", "qty": 500, "avg_price": 1000, "investment": 500000},
        ]
        merged = portfolio_service.merge_holdings(rows)
        assert len(merged) == 1
        assert merged[0]["qty"] == 2000
        assert merged[0]["investment"] == 1850000
        assert merged[0]["avg_price"] == pytest.approx(1850000 / 2000)

    def test_keeps_unresolved_rows_from_different_brokers_separate(self):
        rows = [
            {"raw_name": "Hindustan Zinc", "symbol": None, "qty": 10, "avg_price": 100, "investment": 1000},
            {"raw_name": "HINDZINC EQ", "symbol": None, "qty": 20, "avg_price": 100, "investment": 2000},
        ]
        merged = portfolio_service.merge_holdings(rows)
        assert len(merged) == 2


class TestComputePortfolioView:
    def test_computes_cur_val_pnl_and_pnl_pct_for_priced_rows(self):
        holdings = [{"raw_name": "SBIN", "symbol": "SBIN", "qty": 10, "avg_price": 900, "investment": 9000}]
        rows, totals = portfolio_service.compute_portfolio_view(holdings, {"SBIN": 1000})
        assert rows[0]["ltp"] == 1000
        assert rows[0]["cur_val"] == 10000
        assert rows[0]["pnl"] == 1000
        assert rows[0]["pnl_pct"] == pytest.approx(1000 / 9000 * 100)
        assert totals["total_cur_val"] == 10000
        assert totals["total_pnl"] == 1000
        assert totals["unpriced_count"] == 0

    def test_unresolved_symbol_shows_as_na_and_is_excluded_from_totals(self):
        holdings = [
            {"raw_name": "SBIN", "symbol": "SBIN", "qty": 10, "avg_price": 900, "investment": 9000},
            {"raw_name": "Hindustan Zinc", "symbol": None, "qty": 10, "avg_price": 500, "investment": 5000},
        ]
        rows, totals = portfolio_service.compute_portfolio_view(holdings, {"SBIN": 1000})
        unpriced = next(r for r in rows if r["symbol"] is None)
        assert unpriced["ltp"] is None
        assert unpriced["cur_val"] is None
        assert unpriced["pnl"] is None
        assert unpriced["pnl_pct"] is None
        assert totals["total_investment"] == 14000
        assert totals["total_cur_val"] == 10000
        assert totals["unpriced_count"] == 1

    def test_symbol_with_no_snapshot_yet_also_shows_as_na(self):
        holdings = [{"raw_name": "NIFTYBEES", "symbol": "NIFTYBEES", "qty": 100, "avg_price": 250, "investment": 25000}]
        rows, totals = portfolio_service.compute_portfolio_view(holdings, {})
        assert rows[0]["ltp"] is None
        assert totals["total_cur_val"] is None
        assert totals["unpriced_count"] == 1

    def test_zero_investment_guards_against_division_by_zero(self):
        holdings = [{"raw_name": "X", "symbol": "X", "qty": 10, "avg_price": 0, "investment": 0}]
        rows, _ = portfolio_service.compute_portfolio_view(holdings, {"X": 5})
        assert rows[0]["pnl_pct"] is None


class TestResolveTrackedSymbols:
    def test_returns_only_symbols_not_already_known(self):
        new = portfolio_service.resolve_tracked_symbols(
            ["SBIN", "NIFTYBEES", "HINDZINC"],
            known_company_symbols={"SBIN"},
            raw_name_by_symbol={"NIFTYBEES": "NIFTYBEES", "HINDZINC": "Hindustan Zinc"},
        )
        symbols = {c.symbol for c in new}
        assert symbols == {"NIFTYBEES", "HINDZINC"}
        by_symbol = {c.symbol: c for c in new}
        assert by_symbol["HINDZINC"].name == "Hindustan Zinc"

    def test_no_new_companies_when_everything_already_known(self):
        new = portfolio_service.resolve_tracked_symbols(["SBIN"], known_company_symbols={"SBIN"}, raw_name_by_symbol={})
        assert new == []

    def test_new_companies_default_to_equity(self):
        """company_type classification is the caller's job (a live
        display-name lookup, see looks_like_etf_name below) -- this stays
        a pure diff."""
        new = portfolio_service.resolve_tracked_symbols(["NIFTYBEES"], known_company_symbols=set(), raw_name_by_symbol={})
        assert new[0].company_type == CompanyType.EQUITY


class TestLooksLikeEtfName:
    def test_matches_real_etf_and_fund_display_names(self):
        assert portfolio_service.looks_like_etf_name("Nippon India ETF Nifty 50 BeES") is True
        assert portfolio_service.looks_like_etf_name("Zerodha Nifty 1D Rate Liquid ETF") is True
        assert (
            portfolio_service.looks_like_etf_name("Zerodha Mutual Fund - Zerodha Nifty 8-13 Yr G-sec Etf") is True
        )

    def test_does_not_match_real_stock_names(self):
        assert portfolio_service.looks_like_etf_name("Hindustan Zinc Limited") is False
        assert portfolio_service.looks_like_etf_name("IndusInd Bank Limited") is False
        assert portfolio_service.looks_like_etf_name("Vedanta Aluminium Metal Limited") is False


class TestParseZerodhaOptionInstrument:
    def test_decodes_weekly_index_option(self):
        decoded = portfolio_service.parse_zerodha_option_instrument("NIFTY2681123000PE")
        assert decoded == {
            "symbol": "NIFTY",
            "expiry_date": date(2026, 8, 11),
            "strike_price": 23000.0,
            "option_type": OptionType.PE,
        }

    def test_decodes_december_month_letter_code(self):
        # 'D' (not a digit) stands in for December in Zerodha's weekly
        # tradingsymbol -- year 26, month D, day 06, strike 63000, CE.
        decoded = portfolio_service.parse_zerodha_option_instrument("BANKNIFTY26D0663000CE")
        assert decoded["option_type"] == OptionType.CE
        assert decoded["expiry_date"] == date(2026, 12, 6)

    def test_decodes_monthly_option(self):
        # Confirmed live against a real Zerodha-synced portfolio -- both
        # index (NIFTY) and stock underlyings use this exact shape for
        # their monthly contracts, no day-of-month in the symbol itself
        # (implicit: NSE monthly F&O always expires the last Thursday).
        decoded = portfolio_service.parse_zerodha_option_instrument("NIFTY26AUG23100PE")
        assert decoded == {
            "symbol": "NIFTY",
            "expiry_date": date(2026, 8, 27),  # last Thursday of August 2026
            "strike_price": 23100.0,
            "option_type": OptionType.PE,
        }

    def test_decodes_monthly_stock_option(self):
        decoded = portfolio_service.parse_zerodha_option_instrument("SBIN25AUG970PE")
        assert decoded == {
            "symbol": "SBIN",
            "expiry_date": date(2025, 8, 28),  # last Thursday of August 2025
            "strike_price": 970.0,
            "option_type": OptionType.PE,
        }

    def test_returns_none_for_unrecognized_format(self):
        assert portfolio_service.parse_zerodha_option_instrument("NOTANOPTION") is None
        assert portfolio_service.parse_zerodha_option_instrument("NIFTY26XYZ23100PE") is None


class TestComputePositionsView:
    def test_pnl_is_direction_correct_for_short_and_long(self):
        # Real sample rows: a short (qty<0) and a long (qty>0) position,
        # cross-checked against each broker's own reported P&L.
        rows = [
            {"raw_name": "HINDALCO 25 AUG 860 PUT", "symbol": "HINDALCO", "qty": -700, "avg_price": 2.55, "ltp": 1.05},
            {"raw_name": "NIFTY 11 AUG 24200 CALL", "symbol": "NIFTY", "qty": 780, "avg_price": 162.0, "ltp": 395.0},
        ]
        computed = portfolio_service.compute_positions_view(rows)
        short_put = next(r for r in computed if r["raw_name"].startswith("HINDALCO"))
        long_call = next(r for r in computed if r["raw_name"].startswith("NIFTY"))
        assert short_put["pnl"] == pytest.approx(1050.0)
        assert long_call["pnl"] == pytest.approx(181740.0)
        assert short_put["pnl_pct"] == pytest.approx(1050.0 / (2.55 * 700) * 100)

    def test_missing_ltp_leaves_pnl_and_pnl_pct_none(self):
        rows = [{"raw_name": "X", "symbol": "X", "qty": -100, "avg_price": 5.0, "ltp": None}]
        computed = portfolio_service.compute_positions_view(rows)
        assert computed[0]["pnl"] is None
        assert computed[0]["pnl_pct"] is None


class TestAssignTradeIds:
    def test_default_trade_id_is_the_underlying_symbol(self):
        positions = [
            {"raw_name": "NIFTY 11 AUG 24200 CALL", "broker": "Zerodha", "symbol": "NIFTY"},
            {"raw_name": "ONGC 25 AUG 230 PUT", "broker": "Zerodha", "symbol": "ONGC"},
        ]
        result = portfolio_service.assign_trade_ids(positions, overrides={})
        assert {p["trade_id"] for p in result} == {"NIFTY", "ONGC"}

    def test_undecoded_leg_with_no_symbol_falls_back_to_raw_name(self):
        positions = [{"raw_name": "WEIRD FORMAT 123", "broker": "Zerodha", "symbol": None}]
        result = portfolio_service.assign_trade_ids(positions, overrides={})
        assert result[0]["trade_id"] == "WEIRD FORMAT 123"

    def test_override_wins_over_the_default_per_symbol_grouping(self):
        positions = [{"raw_name": "NIFTY 11 AUG 24200 CALL", "broker": "Zerodha", "symbol": "NIFTY"}]
        overrides = {("Zerodha", "NIFTY 11 AUG 24200 CALL"): "My Custom Trade"}
        result = portfolio_service.assign_trade_ids(positions, overrides)
        assert result[0]["trade_id"] == "My Custom Trade"

    def test_override_can_merge_two_different_underlyings_into_one_trade(self):
        positions = [
            {"raw_name": "NIFTY LEG", "broker": "Zerodha", "symbol": "NIFTY"},
            {"raw_name": "BANKNIFTY LEG", "broker": "Zerodha", "symbol": "BANKNIFTY"},
        ]
        overrides = {("Zerodha", "NIFTY LEG"): "Pairs Trade", ("Zerodha", "BANKNIFTY LEG"): "Pairs Trade"}
        result = portfolio_service.assign_trade_ids(positions, overrides)
        assert {p["trade_id"] for p in result} == {"Pairs Trade"}

    def test_override_is_scoped_by_broker_not_just_raw_name(self):
        positions = [
            {"raw_name": "SAME NAME", "broker": "Zerodha", "symbol": "X"},
            {"raw_name": "SAME NAME", "broker": "Dhan", "symbol": "X"},
        ]
        overrides = {("Zerodha", "SAME NAME"): "Only Zerodha's Trade"}
        result = portfolio_service.assign_trade_ids(positions, overrides)
        zerodha_leg = next(p for p in result if p["broker"] == "Zerodha")
        dhan_leg = next(p for p in result if p["broker"] == "Dhan")
        assert zerodha_leg["trade_id"] == "Only Zerodha's Trade"
        assert dhan_leg["trade_id"] == "X"


class TestClassifyUnderlyingBucket:
    def test_none_symbol_is_other(self):
        assert portfolio_service.classify_underlying_bucket(None, {}) == "other"

    def test_etf_is_other_not_index_or_stock(self):
        # A real bug this guards against: BANKBEES/GILT5YBEES/GOLDBEES/
        # LIQUIDCASE-style ETFs used to be lumped in with genuine Index
        # positions on My Trades' "Index Trades" table; a later fix moved
        # them to "Stock Trades" instead, which was also wrong on a live
        # request -- an ETF belongs in Other Trades.
        assert portfolio_service.classify_underlying_bucket("NIFTYBEES", {"NIFTYBEES": CompanyType.ETF}) == "other"

    def test_fund_is_other_not_index_or_stock(self):
        assert portfolio_service.classify_underlying_bucket("LIQUIDCASE", {"LIQUIDCASE": CompanyType.FUND}) == "other"

    def test_index_company_type_is_index(self):
        assert portfolio_service.classify_underlying_bucket("NIFTY", {"NIFTY": CompanyType.INDEX}) == "index"

    def test_equity_is_stock(self):
        assert portfolio_service.classify_underlying_bucket("RELIANCE", {"RELIANCE": CompanyType.EQUITY}) == "stock"

    def test_unknown_symbol_defaults_to_stock(self):
        assert portfolio_service.classify_underlying_bucket("SOMENEWCO", {}) == "stock"


class TestIsCspTradeType:
    def test_exact_match(self):
        assert portfolio_service.is_csp_trade_type("CSP") is True

    def test_case_insensitive_and_trimmed(self):
        assert portfolio_service.is_csp_trade_type(" csp ") is True

    def test_default_trade_type_is_not_csp(self):
        assert portfolio_service.is_csp_trade_type("Trade") is False

    def test_other_custom_trade_type_is_not_csp(self):
        assert portfolio_service.is_csp_trade_type("Covered Call") is False


class TestIsCoveredCallTradeType:
    def test_exact_match(self):
        assert portfolio_service.is_covered_call_trade_type("Covered Call") is True

    def test_case_insensitive_and_trimmed(self):
        assert portfolio_service.is_covered_call_trade_type(" covered call ") is True

    def test_default_trade_type_is_not_covered_call(self):
        assert portfolio_service.is_covered_call_trade_type("Trade") is False

    def test_csp_is_not_covered_call(self):
        assert portfolio_service.is_covered_call_trade_type("CSP") is False


class TestIsOtherTradeType:
    def test_default_trade_type_is_other(self):
        assert portfolio_service.is_other_trade_type("Trade") is True

    def test_custom_trade_type_is_other(self):
        assert portfolio_service.is_other_trade_type("Strangle") is True

    def test_csp_is_not_other(self):
        assert portfolio_service.is_other_trade_type("CSP") is False

    def test_covered_call_is_not_other(self):
        assert portfolio_service.is_other_trade_type(" Covered Call ") is False


class TestCspBreakevenPrice:
    def test_subtracts_avg_price_from_strike(self):
        assert portfolio_service.csp_breakeven_price(23500.0, 45.0) == 23455.0

    def test_none_strike_is_none(self):
        assert portfolio_service.csp_breakeven_price(None, 45.0) is None

    def test_none_avg_price_is_none(self):
        assert portfolio_service.csp_breakeven_price(23500.0, None) is None


class TestCspBreakevenPct:
    def test_breakeven_below_current_price_is_negative_cushion(self):
        # Breakeven 23455, underlying at 24000 -- breakeven sits below
        # the current price, so there's cushion (negative %).
        assert portfolio_service.csp_breakeven_pct(23455.0, 24000.0) == pytest.approx(-2.2708, abs=1e-3)

    def test_breakeven_above_current_price_is_positive(self):
        # Underlying has already fallen below breakeven.
        assert portfolio_service.csp_breakeven_pct(23455.0, 23000.0) == pytest.approx(1.9783, abs=1e-3)

    def test_none_breakeven_price_is_none(self):
        assert portfolio_service.csp_breakeven_pct(None, 24000.0) is None

    def test_none_underlying_ltp_is_none(self):
        assert portfolio_service.csp_breakeven_pct(23455.0, None) is None

    def test_zero_underlying_ltp_is_none(self):
        assert portfolio_service.csp_breakeven_pct(23455.0, 0.0) is None


class TestCspMaxCredit:
    def test_short_position_uses_absolute_qty(self):
        # A short put's qty is negative (this app's convention) -- max
        # credit is still a positive amount.
        assert portfolio_service.csp_max_credit(45.0, -75.0) == 3375.0

    def test_long_position(self):
        assert portfolio_service.csp_max_credit(45.0, 75.0) == 3375.0

    def test_none_avg_price_is_none(self):
        assert portfolio_service.csp_max_credit(None, -75.0) is None

    def test_none_qty_is_none(self):
        assert portfolio_service.csp_max_credit(45.0, None) is None


class TestCspTargetPnl:
    def test_before_cap_uses_the_accelerated_linear_target(self):
        # 50% of the way through -- accelerated target (1.2x pace) is
        # 0.6 * max_credit, well under the 0.95 * max_credit cap.
        trade_date = date(2026, 8, 1)
        expiry_date = date(2026, 8, 21)  # 20 days to expiry
        as_of = date(2026, 8, 11)  # 10 days held
        assert portfolio_service.csp_target_pnl(3375.0, trade_date, expiry_date, as_of) == pytest.approx(2025.0)

    def test_past_the_crossover_point_is_capped_at_95_pct(self):
        # 80% of the way through -- accelerated target (0.8 * 1.2 = 0.96
        # * max_credit) would exceed the 0.95 cap, so the cap wins.
        max_credit = 1000.0
        trade_date = date(2026, 8, 1)
        expiry_date = date(2026, 11, 9)  # 100 days to expiry
        as_of = trade_date + timedelta(days=80)  # 80 days held
        assert portfolio_service.csp_target_pnl(max_credit, trade_date, expiry_date, as_of) == pytest.approx(950.0)

    def test_held_past_expiry_stays_capped_at_95_pct(self):
        # Never crosses 95% of max credit, no matter how long held.
        max_credit = 1000.0
        trade_date = date(2026, 8, 1)
        expiry_date = date(2026, 8, 21)  # 20 days to expiry
        as_of = date(2026, 9, 20)  # 50 days held -- well past expiry
        assert portfolio_service.csp_target_pnl(max_credit, trade_date, expiry_date, as_of) == pytest.approx(950.0)

    def test_trade_date_equal_to_expiry_is_none(self):
        d = date(2026, 8, 21)
        assert portfolio_service.csp_target_pnl(3375.0, d, d, date(2026, 8, 25)) is None

    def test_expiry_before_trade_date_is_none(self):
        assert portfolio_service.csp_target_pnl(3375.0, date(2026, 8, 21), date(2026, 8, 1), date(2026, 8, 25)) is None

    def test_none_max_credit_is_none(self):
        assert portfolio_service.csp_target_pnl(None, date(2026, 8, 1), date(2026, 8, 21)) is None

    def test_none_trade_date_is_none(self):
        assert portfolio_service.csp_target_pnl(3375.0, None, date(2026, 8, 21)) is None

    def test_none_expiry_date_is_none(self):
        assert portfolio_service.csp_target_pnl(3375.0, date(2026, 8, 1), None) is None

    def test_defaults_as_of_to_today(self):
        # No as_of passed -- shouldn't raise, and since trade_date is
        # today, duration_held is 0 so the accelerated target is 0, but
        # the 50% floor takes over.
        today = date.today()
        assert portfolio_service.csp_target_pnl(3375.0, today, today + timedelta(days=1)) == pytest.approx(1687.5)

    def test_early_in_trade_is_floored_at_50_pct(self):
        # 5% of the way through -- accelerated target (0.05 * 1.2 = 6% of
        # max_credit) would be far below half of max credit, so the 50%
        # floor wins.
        max_credit = 1000.0
        trade_date = date(2026, 8, 1)
        expiry_date = date(2026, 8, 21)  # 20 days to expiry
        as_of = trade_date + timedelta(days=1)  # 1 day held
        assert portfolio_service.csp_target_pnl(max_credit, trade_date, expiry_date, as_of) == pytest.approx(500.0)


class TestCspStopLoss:
    def test_no_existing_stop_loss_is_negative_max_credit(self):
        assert portfolio_service.csp_stop_loss(None, 3375.0, None) == -3375.0

    def test_none_max_credit_is_none(self):
        assert portfolio_service.csp_stop_loss(None, None, 10.0) is None

    def test_negative_pnl_pct_keeps_existing_unchanged(self):
        assert portfolio_service.csp_stop_loss(-3375.0, 3375.0, -10.0) == -3375.0

    def test_none_pnl_pct_keeps_existing_unchanged(self):
        assert portfolio_service.csp_stop_loss(-3375.0, 3375.0, None) == -3375.0

    def test_pnl_pct_below_25_keeps_existing_unchanged(self):
        assert portfolio_service.csp_stop_loss(-3375.0, 3375.0, 10.0) == -3375.0

    def test_pnl_pct_between_25_and_50_ratchets_to_breakeven(self):
        assert portfolio_service.csp_stop_loss(-3375.0, 3375.0, 30.0) == 0.0

    def test_pnl_pct_between_25_and_50_never_loosens_an_already_better_stop(self):
        # Existing stop loss (500) is already better than breakeven (0).
        assert portfolio_service.csp_stop_loss(500.0, 3375.0, 30.0) == 500.0

    def test_pnl_pct_above_50_ratchets_to_half_max_credit(self):
        assert portfolio_service.csp_stop_loss(0.0, 3375.0, 60.0) == 1687.5

    def test_pnl_pct_above_50_never_loosens_an_already_better_stop(self):
        assert portfolio_service.csp_stop_loss(2000.0, 3375.0, 60.0) == 2000.0

    def test_pnl_pct_exactly_25_ratchets_to_breakeven(self):
        assert portfolio_service.csp_stop_loss(-3375.0, 3375.0, 25.0) == 0.0

    def test_pnl_pct_exactly_50_ratchets_to_half_max_credit(self):
        assert portfolio_service.csp_stop_loss(0.0, 3375.0, 50.0) == 1687.5


class TestClassifyPositionBucket:
    def test_no_option_type_is_other(self):
        # A plain stock/ETF position or an undecoded F&O row -- symbol is
        # always None alongside a None option_type (see
        # classify_position_bucket's docstring).
        assert portfolio_service.classify_position_bucket(None, None, {}) == "other"

    def test_stock_option_is_stock(self):
        assert (
            portfolio_service.classify_position_bucket(OptionType.CE, "RELIANCE", {"RELIANCE": CompanyType.EQUITY})
            == "stock"
        )

    def test_index_option_is_index(self):
        assert (
            portfolio_service.classify_position_bucket(OptionType.PE, "NIFTY", {"NIFTY": CompanyType.INDEX}) == "index"
        )

    def test_etf_option_is_stock_not_index(self):
        assert (
            portfolio_service.classify_position_bucket(OptionType.CE, "NIFTYBEES", {"NIFTYBEES": CompanyType.ETF})
            == "stock"
        )

    def test_unknown_underlying_option_defaults_to_stock(self):
        assert portfolio_service.classify_position_bucket(OptionType.PE, "SOMENEWCO", {}) == "stock"


class TestClassifyTradeType:
    def _leg(self, *, leg_type, option_type=None, qty=0.0):
        return {"leg_type": leg_type, "option_type": option_type, "qty": qty}

    def _holding(self, qty=100.0):
        return self._leg(leg_type="Holding", qty=qty)

    def _position(self, option_type, qty):
        return self._leg(leg_type="Position", option_type=option_type, qty=qty)

    def test_csp_is_one_short_put_and_no_holding(self):
        legs = [self._position(OptionType.PE, -75)]
        assert portfolio_service.classify_trade_type(legs) == "CSP"

    def test_short_put_with_a_holding_present_is_not_csp(self):
        # A CSP is specifically *uncovered* -- a holding present makes this
        # something else (not classified by any of these rules).
        legs = [self._holding(), self._position(OptionType.PE, -75)]
        assert portfolio_service.classify_trade_type(legs) is None

    def test_long_put_alone_is_not_csp(self):
        legs = [self._position(OptionType.PE, 75)]
        assert portfolio_service.classify_trade_type(legs) is None

    def test_covered_call_is_holding_plus_one_short_call(self):
        legs = [self._holding(), self._position(OptionType.CE, -50)]
        assert portfolio_service.classify_trade_type(legs) == "Covered Call"

    def test_short_call_without_a_holding_is_not_covered_call(self):
        legs = [self._position(OptionType.CE, -50)]
        assert portfolio_service.classify_trade_type(legs) is None

    def test_strangle_both_legs_short(self):
        legs = [self._position(OptionType.PE, -75), self._position(OptionType.CE, -50)]
        assert portfolio_service.classify_trade_type(legs) == "Strangle"

    def test_strangle_both_legs_long(self):
        legs = [self._position(OptionType.PE, 75), self._position(OptionType.CE, 50)]
        assert portfolio_service.classify_trade_type(legs) == "Strangle"

    def test_strangle_with_a_holding_present_is_still_a_strangle(self):
        legs = [self._holding(), self._position(OptionType.PE, -75), self._position(OptionType.CE, -50)]
        assert portfolio_service.classify_trade_type(legs) == "Strangle"

    def test_mismatched_direction_is_not_a_strangle(self):
        legs = [self._position(OptionType.PE, -75), self._position(OptionType.CE, 50)]
        assert portfolio_service.classify_trade_type(legs) is None

    def test_jade_lizard_is_long_call_plus_two_short_legs(self):
        legs = [
            self._position(OptionType.CE, 25),
            self._position(OptionType.CE, -50),
            self._position(OptionType.PE, -75),
        ]
        assert portfolio_service.classify_trade_type(legs) == "Jade Lizard"

    def test_twisted_sister_is_long_put_plus_two_short_legs(self):
        legs = [
            self._position(OptionType.PE, 25),
            self._position(OptionType.CE, -50),
            self._position(OptionType.PE, -75),
        ]
        assert portfolio_service.classify_trade_type(legs) == "Twisted Sister"

    def test_jade_lizard_with_more_than_three_legs(self):
        legs = [
            self._position(OptionType.CE, 25),
            self._position(OptionType.CE, -50),
            self._position(OptionType.PE, -75),
            self._position(OptionType.PE, -80),
        ]
        assert portfolio_service.classify_trade_type(legs) == "Jade Lizard"

    def test_two_long_legs_among_three_is_not_jade_lizard(self):
        legs = [
            self._position(OptionType.CE, 25),
            self._position(OptionType.CE, 30),
            self._position(OptionType.PE, -75),
        ]
        assert portfolio_service.classify_trade_type(legs) is None

    def test_undecoded_leg_bails_to_none_instead_of_guessing(self):
        legs = [self._position(OptionType.PE, -75), self._leg(leg_type="Position", option_type=None, qty=-1)]
        assert portfolio_service.classify_trade_type(legs) is None

    def test_plain_holding_only_is_none(self):
        assert portfolio_service.classify_trade_type([self._holding()]) is None

    def test_empty_legs_is_none(self):
        assert portfolio_service.classify_trade_type([]) is None


class TestGroupIntoTrades:
    def _leg(self, *, raw_name, broker, symbol, leg_type, pnl, option_type=None, qty=0.0):
        return {
            "raw_name": raw_name,
            "broker": broker,
            "symbol": symbol,
            "leg_type": leg_type,
            "pnl": pnl,
            "option_type": option_type,
            "qty": qty,
        }

    def test_trade_type_mismatch_false_when_no_meta_row_exists(self):
        # A brand-new/never-touched trade is never flagged, regardless of
        # what its legs look like.
        legs = [self._leg(raw_name="X", broker="Zerodha", symbol="X", leg_type="Position", pnl=None, option_type=OptionType.PE, qty=-75)]
        trades = portfolio_service.group_into_trades(legs, overrides={}, trade_meta={}, company_type_by_symbol={})
        assert trades[0]["trade_type_mismatch"] is False

    def test_trade_type_mismatch_true_when_saved_type_disagrees_with_current_legs(self):
        # Saved as CSP, but the legs now look like a Covered Call (a
        # holding plus a short call) -- a genuine disagreement, flagged.
        legs = [
            self._leg(raw_name="X", broker="Zerodha", symbol="X", leg_type="Holding", pnl=None, qty=100),
            self._leg(raw_name="X CE", broker="Zerodha", symbol="X", leg_type="Position", pnl=None, option_type=OptionType.CE, qty=-50),
        ]
        trade_meta = {"X": {"trade_type": "CSP"}}
        trades = portfolio_service.group_into_trades(legs, overrides={}, trade_meta=trade_meta, company_type_by_symbol={})
        assert trades[0]["trade_type_mismatch"] is True

    def test_trade_type_mismatch_false_when_detection_agrees(self):
        legs = [self._leg(raw_name="X", broker="Zerodha", symbol="X", leg_type="Position", pnl=None, option_type=OptionType.PE, qty=-75)]
        trade_meta = {"X": {"trade_type": "csp"}}  # saved lowercase -- still matches case-insensitively
        trades = portfolio_service.group_into_trades(legs, overrides={}, trade_meta=trade_meta, company_type_by_symbol={})
        assert trades[0]["trade_type_mismatch"] is False

    def test_trade_type_mismatch_false_when_shape_matches_no_known_strategy(self):
        # A trade meta row exists (user manually typed a custom label) but
        # the legs don't match any known options shape at all --
        # classify_trade_type returns None, which must NOT count as a
        # mismatch (a custom label like "Earnings Play" shouldn't be
        # flagged just because it isn't a recognized strategy).
        legs = [self._leg(raw_name="X", broker="Zerodha", symbol="X", leg_type="Holding", pnl=None, qty=100)]
        trade_meta = {"X": {"trade_type": "Earnings Play"}}
        trades = portfolio_service.group_into_trades(legs, overrides={}, trade_meta=trade_meta, company_type_by_symbol={})
        assert trades[0]["trade_type_mismatch"] is False

    def test_groups_by_default_underlying_and_sums_pnl(self):
        legs = [
            self._leg(raw_name="RELIANCE", broker="Zerodha", symbol="RELIANCE", leg_type="Holding", pnl=1000.0),
            self._leg(raw_name="RELIANCE 25AUG3000CE", broker="Zerodha", symbol="RELIANCE", leg_type="Position", pnl=-200.0),
        ]
        trades = portfolio_service.group_into_trades(legs, overrides={}, trade_meta={}, company_type_by_symbol={})
        assert len(trades) == 1
        trade = trades[0]
        assert trade["trade_id"] == "RELIANCE"
        assert trade["leg_count"] == 2
        assert trade["total_pnl"] == 800.0
        assert trade["bucket"] == "stock"
        assert trade["underlying_label"] == "RELIANCE"
        assert trade["trade_type"] == "Trade"

    def test_unanimous_index_bucket(self):
        legs = [self._leg(raw_name="NIFTY", broker="Zerodha", symbol="NIFTY", leg_type="Holding", pnl=None)]
        trades = portfolio_service.group_into_trades(
            legs, overrides={}, trade_meta={}, company_type_by_symbol={"NIFTY": CompanyType.INDEX}
        )
        assert trades[0]["bucket"] == "index"
        assert trades[0]["total_pnl"] is None

    def test_etf_bucket_defaults_to_other(self):
        # An ETF holding used to land in "Index Trades" purely because of
        # its company_type, then "Stock Trades" after an incomplete fix --
        # it belongs in Other Trades by default.
        legs = [self._leg(raw_name="NIFTYBEES", broker="Zerodha", symbol="NIFTYBEES", leg_type="Holding", pnl=None)]
        trades = portfolio_service.group_into_trades(
            legs, overrides={}, trade_meta={}, company_type_by_symbol={"NIFTYBEES": CompanyType.ETF}
        )
        assert trades[0]["bucket"] == "other"

    def test_mixed_bucket_legs_fall_to_other(self):
        legs = [
            self._leg(raw_name="RELIANCE", broker="Zerodha", symbol="RELIANCE", leg_type="Holding", pnl=100.0),
            self._leg(raw_name="NIFTY", broker="Zerodha", symbol="NIFTY", leg_type="Holding", pnl=50.0),
        ]
        overrides = {("Zerodha", "RELIANCE"): "Mixed Trade", ("Zerodha", "NIFTY"): "Mixed Trade"}
        trades = portfolio_service.group_into_trades(
            legs, overrides=overrides, trade_meta={}, company_type_by_symbol={"NIFTY": CompanyType.INDEX}
        )
        assert len(trades) == 1
        assert trades[0]["bucket"] == "other"

    def test_bucket_override_wins_over_computed_default(self):
        # The manual escape hatch for an ETF the user deliberately wants
        # shown alongside genuine Index Trades (see
        # supabase/migrations/0024_portfolio_trade_meta_bucket_override.sql).
        legs = [self._leg(raw_name="NIFTYBEES", broker="Zerodha", symbol="NIFTYBEES", leg_type="Holding", pnl=None)]
        trade_meta = {"NIFTYBEES": {"bucket_override": "index"}}
        trades = portfolio_service.group_into_trades(
            legs, overrides={}, trade_meta=trade_meta, company_type_by_symbol={"NIFTYBEES": CompanyType.ETF}
        )
        assert trades[0]["bucket"] == "index"

    def test_no_bucket_override_falls_back_to_computed_default(self):
        legs = [self._leg(raw_name="NIFTYBEES", broker="Zerodha", symbol="NIFTYBEES", leg_type="Holding", pnl=None)]
        trade_meta = {"NIFTYBEES": {"bucket_override": None}}
        trades = portfolio_service.group_into_trades(
            legs, overrides={}, trade_meta=trade_meta, company_type_by_symbol={"NIFTYBEES": CompanyType.ETF}
        )
        assert trades[0]["bucket"] == "other"

    def test_no_resolved_symbol_is_other_bucket(self):
        legs = [self._leg(raw_name="WEIRD FORMAT 123", broker="Zerodha", symbol=None, leg_type="Position", pnl=None)]
        trades = portfolio_service.group_into_trades(legs, overrides={}, trade_meta={}, company_type_by_symbol={})
        assert trades[0]["bucket"] == "other"
        assert trades[0]["underlying_label"] == "WEIRD FORMAT 123"

    def test_default_label_joins_multiple_underlyings_when_merged(self):
        legs = [
            self._leg(raw_name="NIFTY LEG", broker="Zerodha", symbol="NIFTY", leg_type="Position", pnl=None),
            self._leg(raw_name="BANKNIFTY LEG", broker="Zerodha", symbol="BANKNIFTY", leg_type="Position", pnl=None),
        ]
        overrides = {("Zerodha", "NIFTY LEG"): "Pairs Trade", ("Zerodha", "BANKNIFTY LEG"): "Pairs Trade"}
        trades = portfolio_service.group_into_trades(legs, overrides, trade_meta={}, company_type_by_symbol={})
        assert trades[0]["underlying_label"] == "BANKNIFTY + NIFTY"

    def test_trade_meta_override_wins_for_underlying_label_and_trade_type(self):
        legs = [self._leg(raw_name="TATAMTRDVR", broker="Zerodha", symbol="TATAMTRDVR", leg_type="Holding", pnl=None)]
        trade_meta = {"TATAMTRDVR": {"underlying_label": "Tata Motors Passenger Vehicle", "trade_type": "Long Term Hold"}}
        trades = portfolio_service.group_into_trades(
            legs, overrides={}, trade_meta=trade_meta, company_type_by_symbol={}
        )
        assert trades[0]["underlying_label"] == "Tata Motors Passenger Vehicle"
        assert trades[0]["trade_type"] == "Long Term Hold"

    def test_blank_meta_override_falls_back_to_default(self):
        legs = [self._leg(raw_name="RELIANCE", broker="Zerodha", symbol="RELIANCE", leg_type="Holding", pnl=None)]
        trade_meta = {"RELIANCE": {"underlying_label": "", "trade_type": ""}}
        trades = portfolio_service.group_into_trades(
            legs, overrides={}, trade_meta=trade_meta, company_type_by_symbol={}
        )
        assert trades[0]["underlying_label"] == "RELIANCE"
        assert trades[0]["trade_type"] == "Trade"


class TestPositionsToRecords:
    def test_builds_portfolio_position_models(self):
        positions = [
            {
                "raw_name": "ONGC 25 AUG 230 PUT",
                "symbol": "ONGC",
                "expiry_date": date(2026, 8, 25),
                "strike_price": 230.0,
                "option_type": OptionType.PE,
                "qty": -2250,
                "avg_price": 1.24,
                "ltp": 1.5,
            }
        ]
        records = portfolio_service.positions_to_records("u1", "Portfolio 1", "Dhan", positions)
        assert len(records) == 1
        assert records[0].user_id == "u1"
        assert records[0].symbol == "ONGC"
        assert records[0].option_type == OptionType.PE
        assert records[0].qty == -2250
        assert records[0].ltp_as_of is None

    def test_carries_ltp_as_of_through_when_present(self):
        positions = [
            {
                "raw_name": "ONGC 25 AUG 230 PUT",
                "symbol": "ONGC",
                "expiry_date": date(2026, 8, 25),
                "strike_price": 230.0,
                "option_type": OptionType.PE,
                "qty": -2250,
                "avg_price": 1.24,
                "ltp": 1.5,
                "ltp_as_of": date(2026, 8, 17),
            }
        ]
        records = portfolio_service.positions_to_records("u1", "Portfolio 1", "Dhan", positions)
        assert records[0].ltp_as_of == date(2026, 8, 17)


class TestHoldingsToRecords:
    def test_builds_portfolio_holding_models(self):
        holdings = [{"raw_name": "SBIN", "symbol": "SBIN", "qty": 10, "avg_price": 900, "investment": 9000}]
        records = portfolio_service.holdings_to_records("u1", "Portfolio 1", "Zerodha", holdings)
        assert len(records) == 1
        assert records[0].user_id == "u1"
        assert records[0].portfolio_name == "Portfolio 1"
        assert records[0].broker == "Zerodha"
        assert records[0].symbol == "SBIN"


class TestDhanHoldingsFromApi:
    def test_translates_holdings_endpoint_rows(self):
        rows = [
            {
                "exchange": "NSE",
                "tradingSymbol": "sbin",
                "securityId": "3045",
                "isin": "INE062A01020",
                "totalQty": 10,
                "avgCostPrice": 900.0,
            }
        ]
        holdings = portfolio_service.dhan_holdings_from_api(rows)
        assert holdings == [
            {"raw_name": "SBIN", "symbol": "SBIN", "qty": 10.0, "avg_price": 900.0, "investment": 9000.0}
        ]

    def test_skips_rows_with_zero_or_missing_quantity(self):
        rows = [
            {"tradingSymbol": "SOLDOFF", "totalQty": 0, "avgCostPrice": 100.0},
            {"tradingSymbol": "", "totalQty": 5, "avgCostPrice": 100.0},
        ]
        assert portfolio_service.dhan_holdings_from_api(rows) == []


class TestDhanPositionsFromApi:
    def test_translates_short_position_using_cost_price_and_ltp_lookup(self):
        # Field values match a real GET /v2/positions response: tradingSymbol
        # is "SYMBOL-MonYYYY-STRIKE-CE/PE" and drvOptionType is the full
        # word ("PUT"/"CALL"), not a CE/PE code.
        rows = [
            {
                "tradingSymbol": "NIFTY-Aug2026-23000-PE",
                "securityId": "49081",
                "exchangeSegment": "NSE_FNO",
                "netQty": -780,
                "costPrice": 10.15,
                "buyAvg": 0,
                "sellAvg": 10.15,
                "drvExpiryDate": "2026-08-11",
                "drvStrikePrice": 23000,
                "drvOptionType": "PUT",
            }
        ]
        positions = portfolio_service.dhan_positions_from_api(rows, {"49081": 1.1})
        assert len(positions) == 1
        p = positions[0]
        assert p["symbol"] == "NIFTY"
        assert p["expiry_date"] == date(2026, 8, 11)
        assert p["strike_price"] == 23000.0
        assert p["option_type"] == OptionType.PE
        assert p["qty"] == -780.0
        assert p["avg_price"] == 10.15
        assert p["ltp"] == 1.1

    def test_underlying_containing_an_ampersand_is_not_truncated(self):
        # Regression, confirmed live: an earlier leading-alphabetic-run
        # regex (`^([A-Z]+)`) stopped matching at the first non-letter
        # character, truncating "M&M" (Mahindra & Mahindra's real NSE
        # symbol) down to just "M" -- a symbol that doesn't exist, so
        # the leg's underlying LTP/Momentum/1D/5D/20D all silently came
        # back blank on My CSP/My CC.
        rows = [
            {
                "tradingSymbol": "M&M-Sep2026-3200-PE", "securityId": "1", "netQty": -200, "costPrice": 10.95,
                "drvExpiryDate": "2026-09-29", "drvStrikePrice": 3200, "drvOptionType": "PUT",
            }
        ]
        positions = portfolio_service.dhan_positions_from_api(rows, {})
        assert positions[0]["symbol"] == "M&M"

    def test_underlying_with_its_own_hyphen_in_a_derivative_symbol_keeps_both_parts(self):
        # "NAM-INDIA" is a real NSE symbol with a hyphen of its own --
        # splitting on the *first* hyphen (rather than a known trailing-
        # token count) would wrongly cut this down to "NAM".
        rows = [
            {
                "tradingSymbol": "NAM-INDIA-Sep2026-720-CE", "securityId": "1", "netQty": -50, "costPrice": 5.0,
                "drvExpiryDate": "2026-09-29", "drvStrikePrice": 720, "drvOptionType": "CALL",
            }
        ]
        positions = portfolio_service.dhan_positions_from_api(rows, {})
        assert positions[0]["symbol"] == "NAM-INDIA"

    def test_futures_position_underlying_strips_the_two_trailing_tokens(self):
        rows = [
            {
                "tradingSymbol": "RELIANCE-Aug2026-FUT", "securityId": "1", "netQty": 100, "costPrice": 2950.0,
                "drvExpiryDate": "2026-08-27", "drvStrikePrice": 0, "drvOptionType": "",
            }
        ]
        positions = portfolio_service.dhan_positions_from_api(rows, {})
        assert positions[0]["symbol"] == "RELIANCE"

    def test_plain_hyphenated_equity_symbol_is_not_mistaken_for_a_futures_suffix(self):
        # A plain equity/ETF position's tradingSymbol has no expiry suffix
        # at all -- but "NAM-INDIA" held as a stock position looks exactly
        # like a 2-token "<underlying>-<FUT-ish token>" shape. Only a real
        # drvExpiryDate (not absent/the no-expiry sentinel) should trigger
        # the trailing-token split; here it must return the tradingSymbol
        # verbatim instead of wrongly stripping "-INDIA" off as if it were
        # a futures suffix.
        rows = [
            {
                "tradingSymbol": "NAM-INDIA", "securityId": "1", "netQty": 100, "costPrice": 500.0,
                "drvExpiryDate": "0001-01-01", "drvStrikePrice": 0, "drvOptionType": "",
            }
        ]
        positions = portfolio_service.dhan_positions_from_api(rows, {})
        assert positions[0]["symbol"] == "NAM-INDIA"

    def test_equity_etf_position_with_dhan_no_expiry_sentinel_gets_no_expiry_date(self):
        # Regression, confirmed live: an ETF (SILVERBEES) intraday
        # position via /v2/positions still carries a drvExpiryDate key,
        # but Dhan's own documented sentinel for "no real F&O expiry" --
        # "0001-01-01" -- not null/omitted. A plain truthiness check on
        # that string treated it as a real expiry, producing a position
        # with a bogus far-past expiry but no strike/option_type -- which
        # src/utils/refresh_bar.py's _dhan_fo_universe then misread as a
        # phantom futures contract for a symbol that was never a
        # derivative (rendered in the UI as "SILVERBEES 01-Jan-01 FUT").
        rows = [
            {
                "tradingSymbol": "SILVERBEES", "securityId": "500", "exchangeSegment": "NSE_EQ",
                "netQty": 100, "costPrice": 85.5,
                "drvExpiryDate": "0001-01-01", "drvStrikePrice": 0, "drvOptionType": "",
            }
        ]
        positions = portfolio_service.dhan_positions_from_api(rows, {})
        assert positions[0]["expiry_date"] is None
        assert positions[0]["strike_price"] is None
        assert positions[0]["option_type"] is None

    def test_accepts_both_ce_pe_codes_and_call_put_words(self):
        rows = [
            {"tradingSymbol": "A-1", "securityId": "1", "netQty": 1, "costPrice": 1, "drvOptionType": "CE"},
            {"tradingSymbol": "A-2", "securityId": "2", "netQty": 1, "costPrice": 1, "drvOptionType": "PE"},
            {"tradingSymbol": "A-3", "securityId": "3", "netQty": 1, "costPrice": 1, "drvOptionType": "CALL"},
            {"tradingSymbol": "A-4", "securityId": "4", "netQty": 1, "costPrice": 1, "drvOptionType": "put"},
        ]
        positions = portfolio_service.dhan_positions_from_api(rows, {})
        assert [p["option_type"] for p in positions] == [
            OptionType.CE, OptionType.PE, OptionType.CE, OptionType.PE,
        ]

    def test_falls_back_to_buy_or_sell_avg_when_cost_price_is_absent(self):
        long_row = {
            "tradingSymbol": "ONGC-Aug2026-230-PE", "securityId": "1", "exchangeSegment": "NSE_FNO",
            "netQty": 100, "costPrice": 0, "buyAvg": 1.24, "sellAvg": 0,
            "drvExpiryDate": "2026-08-25", "drvStrikePrice": 230, "drvOptionType": "PUT",
        }
        short_row = {
            "tradingSymbol": "ONGC-Aug2026-257.5-CE", "securityId": "2", "exchangeSegment": "NSE_FNO",
            "netQty": -100, "costPrice": None, "buyAvg": 0, "sellAvg": 2.5,
            "drvExpiryDate": "2026-08-25", "drvStrikePrice": 257.5, "drvOptionType": "CALL",
        }
        positions = portfolio_service.dhan_positions_from_api([long_row, short_row], {})
        assert positions[0]["avg_price"] == 1.24
        assert positions[1]["avg_price"] == 2.5

    def test_missing_ltp_lookup_leaves_ltp_none(self):
        rows = [
            {
                "tradingSymbol": "NIFTY26AUG23000PE", "securityId": "49081", "exchangeSegment": "NSE_FNO",
                "netQty": -780, "costPrice": 10.15,
                "drvExpiryDate": "2026-08-11", "drvStrikePrice": 23000, "drvOptionType": "PE",
            }
        ]
        positions = portfolio_service.dhan_positions_from_api(rows, {})
        assert positions[0]["ltp"] is None

    def test_skips_closed_positions_with_zero_net_qty(self):
        rows = [{"tradingSymbol": "CLOSED", "securityId": "1", "netQty": 0, "costPrice": 1.0}]
        assert portfolio_service.dhan_positions_from_api(rows, {}) == []

    def test_same_trading_symbol_under_two_product_types_gets_disambiguated(self):
        # Regression, confirmed live via postgrest.exceptions.APIError
        # 23505 ("duplicate key value violates unique constraint
        # portfolio_positions_pkey"): Dhan allows the same contract to be
        # held as both an INTRADAY trade and a carried-forward CNC
        # position at once, returning two /v2/positions rows with the
        # identical tradingSymbol -- portfolio_positions' primary key is
        # (user_id, portfolio_name, broker, raw_name), so both rows
        # landing on the same raw_name failed the whole sync's insert.
        rows = [
            {
                "tradingSymbol": "RELIANCE", "securityId": "2885", "productType": "INTRADAY",
                "netQty": 10, "costPrice": 2900.0,
            },
            {
                "tradingSymbol": "RELIANCE", "securityId": "2885", "productType": "CNC",
                "netQty": 5, "costPrice": 2800.0,
            },
        ]
        positions = portfolio_service.dhan_positions_from_api(rows, {})
        raw_names = {p["raw_name"] for p in positions}
        assert len(raw_names) == 2
        assert raw_names == {"RELIANCE (INTRADAY)", "RELIANCE (CNC)"}

    def test_unique_trading_symbols_keep_their_plain_raw_name(self):
        # No collision -- raw_name must stay exactly the tradingSymbol so
        # existing trade_date/stop_loss/trade-group links (keyed on
        # (broker, raw_name)) survive the next sync unchanged.
        rows = [
            {"tradingSymbol": "RELIANCE", "securityId": "1", "productType": "CNC", "netQty": 10, "costPrice": 2900.0},
            {"tradingSymbol": "TCS", "securityId": "2", "productType": "CNC", "netQty": 5, "costPrice": 3500.0},
        ]
        positions = portfolio_service.dhan_positions_from_api(rows, {})
        assert {p["raw_name"] for p in positions} == {"RELIANCE", "TCS"}

    def test_collision_with_identical_product_type_falls_back_to_security_id(self):
        # Regression, confirmed live: the first fix (suffix with
        # productType only) still 23505'd on a real account that
        # confirmed it had no intraday/overnight overlap -- meaning Dhan
        # returned two same-tradingSymbol rows sharing one productType
        # too, which a productType-only suffix can't disambiguate.
        rows = [
            {
                "tradingSymbol": "RELIANCE", "securityId": "111", "productType": "CNC",
                "netQty": 10, "costPrice": 2900.0,
            },
            {
                "tradingSymbol": "RELIANCE", "securityId": "222", "productType": "CNC",
                "netQty": 5, "costPrice": 2800.0,
            },
        ]
        positions = portfolio_service.dhan_positions_from_api(rows, {})
        raw_names = {p["raw_name"] for p in positions}
        assert len(raw_names) == 2
        assert raw_names == {"RELIANCE (CNC)", "RELIANCE (222)"}

    def test_fully_identical_rows_still_get_unique_raw_names(self):
        # Absolute last resort: even a literal duplicate row (same
        # tradingSymbol, productType, and securityId) must not crash the
        # sync -- falls back to a bare ordinal suffix.
        row = {
            "tradingSymbol": "RELIANCE", "securityId": "111", "productType": "CNC",
            "netQty": 10, "costPrice": 2900.0,
        }
        positions = portfolio_service.dhan_positions_from_api([row, dict(row)], {})
        raw_names = {p["raw_name"] for p in positions}
        assert len(raw_names) == 2


class TestZerodhaHoldingsFromApi:
    def test_translates_holdings_endpoint_rows(self):
        rows = [{"tradingsymbol": "sbin", "exchange": "NSE", "quantity": 10, "average_price": 900.0, "last_price": 1021.1}]
        holdings = portfolio_service.zerodha_holdings_from_api(rows)
        assert holdings == [
            {"raw_name": "SBIN", "symbol": "SBIN", "qty": 10.0, "avg_price": 900.0, "investment": 9000.0}
        ]

    def test_skips_rows_with_zero_or_missing_quantity(self):
        rows = [
            {"tradingsymbol": "SOLDOFF", "quantity": 0, "average_price": 100.0},
            {"tradingsymbol": "", "quantity": 5, "average_price": 100.0},
        ]
        assert portfolio_service.zerodha_holdings_from_api(rows) == []

    def test_a_fully_pledged_holding_is_not_dropped(self):
        # Real bug this guards against: a holding pledged entirely as
        # margin comes back quantity=0 (Kite's own web UI shows "Qty. 0"
        # too) even though it's still fully owned -- confirmed live
        # against a real account. The pledged quantity is reported
        # separately (collateral_quantity here), and average_price is
        # computed against the *total* owned quantity, not just the free
        # portion -- these exact figures (GILT5YBEES) are from that real
        # account: avg 64.30 * 7500 = 4,82,250.00, matching Kite's own
        # displayed "Invested" to the rupee.
        rows = [
            {
                "tradingsymbol": "GILT5YBEES",
                "quantity": 0,
                "t1_quantity": 0,
                "collateral_quantity": 7500,
                "average_price": 64.30,
                "last_price": 66.26,
            }
        ]
        holdings = portfolio_service.zerodha_holdings_from_api(rows)
        assert holdings == [
            {"raw_name": "GILT5YBEES", "symbol": "GILT5YBEES", "qty": 7500.0, "avg_price": 64.30, "investment": 482250.0}
        ]

    def test_partially_pledged_holding_sums_free_and_pledged_quantity(self):
        rows = [{"tradingsymbol": "PARTIAL", "quantity": 100, "t1_quantity": 0, "collateral_quantity": 400, "average_price": 10.0}]
        holdings = portfolio_service.zerodha_holdings_from_api(rows)
        assert holdings[0]["qty"] == 500.0
        assert holdings[0]["investment"] == 5000.0


class TestZerodhaPositionsFromApi:
    def test_translates_short_weekly_index_option_using_ltp_from_the_row(self):
        # tradingsymbol is Zerodha's own weekly-option format -- the exact
        # same string shape parse_zerodha_option_instrument already
        # decodes for the CSV positions export, so it's reused as-is here.
        rows = [
            {
                "tradingsymbol": "NIFTY2681123000PE",
                "quantity": -75,
                "average_price": 10.15,
                "last_price": 1.1,
            }
        ]
        positions = portfolio_service.zerodha_positions_from_api(rows)
        assert len(positions) == 1
        p = positions[0]
        assert p["raw_name"] == "NIFTY2681123000PE"
        assert p["symbol"] == "NIFTY"
        assert p["expiry_date"] == date(2026, 8, 11)
        assert p["strike_price"] == 23000.0
        assert p["option_type"] == OptionType.PE
        assert p["qty"] == -75.0
        assert p["avg_price"] == 10.15
        assert p["ltp"] == 1.1

    def test_decodes_monthly_option_format(self):
        # A real bug this guards against: NIFTY's monthly contracts
        # ("NIFTY26AUG23100PE" -- no day-of-month in the symbol) used to
        # fail to decode entirely, so My Trades showed the raw instrument
        # string as the "underlying" and sorted it into Other Trades
        # instead of Index Trades -- confirmed live.
        rows = [{"tradingsymbol": "NIFTY26AUG23100PE", "quantity": -75, "average_price": 10.15, "last_price": 12.0}]
        positions = portfolio_service.zerodha_positions_from_api(rows)
        assert positions[0]["symbol"] == "NIFTY"
        assert positions[0]["expiry_date"] == date(2026, 8, 27)
        assert positions[0]["strike_price"] == 23100.0
        assert positions[0]["option_type"] == OptionType.PE

    def test_undecoded_futures_format_keeps_raw_name_with_no_contract_detail(self):
        rows = [{"tradingsymbol": "NIFTY26AUGFUT", "quantity": 1, "average_price": 5.0, "last_price": 4.5}]
        positions = portfolio_service.zerodha_positions_from_api(rows)
        assert positions[0]["symbol"] is None
        assert positions[0]["expiry_date"] is None
        assert positions[0]["raw_name"] == "NIFTY26AUGFUT"

    def test_missing_last_price_leaves_ltp_none(self):
        rows = [{"tradingsymbol": "NIFTY2681123000PE", "quantity": -75, "average_price": 10.15}]
        assert portfolio_service.zerodha_positions_from_api(rows)[0]["ltp"] is None

    def test_skips_closed_positions_with_zero_quantity(self):
        rows = [{"tradingsymbol": "CLOSED", "quantity": 0, "average_price": 1.0}]
        assert portfolio_service.zerodha_positions_from_api(rows) == []

    def test_same_trading_symbol_under_two_products_gets_disambiguated(self):
        # Same fix/reasoning as dhan_positions_from_api's identical
        # regression test -- Kite can hold one tradingsymbol under more
        # than one margin product (CNC/MIS/NRML) at once, which would
        # otherwise collide on portfolio_positions' primary key.
        rows = [
            {"tradingsymbol": "RELIANCE", "product": "MIS", "quantity": 10, "average_price": 2900.0},
            {"tradingsymbol": "RELIANCE", "product": "CNC", "quantity": 5, "average_price": 2800.0},
        ]
        positions = portfolio_service.zerodha_positions_from_api(rows)
        assert {p["raw_name"] for p in positions} == {"RELIANCE (MIS)", "RELIANCE (CNC)"}


class TestApplyFallbackOptionLtp:
    def _position(self, **overrides):
        base = {
            "raw_name": "HDFCBANK-Aug2026-700-PE",
            "symbol": "HDFCBANK",
            "expiry_date": date(2026, 8, 25),
            "strike_price": 700.0,
            "option_type": OptionType.PE,
            "qty": -1300.0,
            "avg_price": 4.9,
            "ltp": None,
        }
        return {**base, **overrides}

    def test_fills_missing_ltp_from_matching_chain_row(self):
        chains = {
            ("HDFCBANK", date(2026, 8, 25)): [
                {"strike_price": 700.0, "option_type": "PE", "last_price": 5.2, "close": 5.0},
            ]
        }
        positions = portfolio_service.apply_fallback_option_ltp([self._position()], chains)
        assert positions[0]["ltp"] == 5.2

    def test_falls_back_to_close_when_last_price_is_missing(self):
        chains = {
            ("HDFCBANK", date(2026, 8, 25)): [
                {"strike_price": 700.0, "option_type": "PE", "last_price": None, "close": 5.0},
            ]
        }
        positions = portfolio_service.apply_fallback_option_ltp([self._position()], chains)
        assert positions[0]["ltp"] == 5.0

    def test_never_overwrites_an_ltp_the_broker_already_supplied(self):
        chains = {("HDFCBANK", date(2026, 8, 25)): [{"strike_price": 700.0, "option_type": "PE", "last_price": 5.2}]}
        positions = portfolio_service.apply_fallback_option_ltp([self._position(ltp=9.9)], chains)
        assert positions[0]["ltp"] == 9.9

    def test_sets_ltp_as_of_to_the_chain_rows_trade_date_when_filling(self):
        # A real bug this guards against: a fallback LTP looked
        # identical to a live one on My CSP, with nothing distinguishing
        # a Dhan sync's stale EOD close from an actual live quote.
        chains = {
            ("HDFCBANK", date(2026, 8, 25)): [
                {"strike_price": 700.0, "option_type": "PE", "last_price": 5.2, "trade_date": date(2026, 8, 17)},
            ]
        }
        positions = portfolio_service.apply_fallback_option_ltp([self._position()], chains)
        assert positions[0]["ltp_as_of"] == date(2026, 8, 17)

    def test_never_sets_ltp_as_of_when_broker_already_supplied_ltp(self):
        chains = {
            ("HDFCBANK", date(2026, 8, 25)): [
                {"strike_price": 700.0, "option_type": "PE", "last_price": 5.2, "trade_date": date(2026, 8, 17)},
            ]
        }
        positions = portfolio_service.apply_fallback_option_ltp([self._position(ltp=9.9)], chains)
        assert positions[0].get("ltp_as_of") is None

    def test_leaves_ltp_none_when_no_chain_for_that_symbol_and_expiry(self):
        # e.g. NIFTY index options -- this app tracks no F&O data for indices.
        positions = portfolio_service.apply_fallback_option_ltp([self._position(symbol="NIFTY")], {})
        assert positions[0]["ltp"] is None

    def test_leaves_ltp_none_when_strike_not_in_chain(self):
        chains = {("HDFCBANK", date(2026, 8, 25)): [{"strike_price": 850.0, "option_type": "PE", "last_price": 1.0}]}
        positions = portfolio_service.apply_fallback_option_ltp([self._position()], chains)
        assert positions[0]["ltp"] is None

    def test_ignores_non_option_positions(self):
        stock_row = self._position(option_type=None, strike_price=None, expiry_date=None)
        positions = portfolio_service.apply_fallback_option_ltp([stock_row], {})
        assert positions[0]["ltp"] is None


class TestDhanTradeFillsFromApi:
    # Verbatim (values), shape confirmed live 2026-08-27 against a real
    # account -- see dhan_trade_fills_from_api's own docstring for the
    # exchangeTradeId="0" bug this uncovered.
    _LIVE_PUT_FILL = {
        "dhanClientId": "1107705688",
        "orderId": "228260827162002",
        "exchangeOrderId": "623962328507896",
        "exchangeTradeId": "0",
        "transactionType": "SELL",
        "exchangeSegment": "MCX_COMM",
        "productType": "MARGIN",
        "orderType": None,
        "customSymbol": "GOLD 31 AUG 135000 PUT",
        "securityId": "566396",
        "tradedQuantity": 400,
        "tradedPrice": 19.75,
        "isin": "",
        "instrument": "OPTFUT",
        "sebiTax": 0.0079,
        "stt": 3.95,
        "brokerageCharges": 20.0,
        "serviceTax": 4.1958,
        "exchangeTransactionCharges": 3.3022,
        "stampDuty": 0.0,
        "createTime": "NA",
        "updateTime": "NA",
        "exchangeTime": "2026-08-27T16:12:15",
        "drvExpiryDate": "2026-08-31",
        "drvOptionType": "PUT",
        "drvStrikePrice": 135000.0,
    }

    def test_parses_a_live_option_fill(self):
        fills = portfolio_service.dhan_trade_fills_from_api([self._LIVE_PUT_FILL])
        assert len(fills) == 1
        fill = fills[0]
        assert fill["symbol"] == "GOLD"
        assert fill["expiry_date"] == date(2026, 8, 31)
        assert fill["strike_price"] == 135000.0
        assert fill["option_type"] == OptionType.PE
        assert fill["transaction_type"] == "SELL"
        assert fill["qty"] == 400
        assert fill["price"] == 19.75
        assert fill["product_type"] == "MARGIN"
        assert fill["traded_at"] == datetime(2026, 8, 27, 16, 12, 15)
        assert fill["brokerage"] == 20.0

    def test_call_option_decodes_the_same_way(self):
        row = {**self._LIVE_PUT_FILL, "customSymbol": "NIFTY 01 SEP 25000 CALL", "drvOptionType": "CALL"}
        fill = portfolio_service.dhan_trade_fills_from_api([row])[0]
        assert fill["symbol"] == "NIFTY"
        assert fill["option_type"] == OptionType.CE

    def test_synthesizes_exchange_trade_id_since_dhans_own_field_is_always_zero(self):
        # The real bug this guards against: exchangeTradeId comes back "0"
        # on every fill regardless of order/symbol/time -- confirmed live
        # across 400 rows -- so it can never be used as a unique key.
        fill = portfolio_service.dhan_trade_fills_from_api([self._LIVE_PUT_FILL])[0]
        assert fill["exchange_trade_id"] != "0"
        assert "228260827162002" in fill["exchange_trade_id"]  # orderId
        assert "623962328507896" in fill["exchange_trade_id"]  # exchangeOrderId

    def test_two_distinct_fills_with_dhans_broken_id_get_distinct_synthetic_ids(self):
        other = {**self._LIVE_PUT_FILL, "orderId": "999", "exchangeOrderId": "888", "exchangeTime": "2026-08-27T16:13:00"}
        fills = portfolio_service.dhan_trade_fills_from_api([self._LIVE_PUT_FILL, other])
        assert fills[0]["exchange_trade_id"] != fills[1]["exchange_trade_id"]

    def test_charges_are_summed_from_all_five_dhan_fields(self):
        fill = portfolio_service.dhan_trade_fills_from_api([self._LIVE_PUT_FILL])[0]
        expected = 0.0079 + 3.95 + 4.1958 + 3.3022 + 0.0  # sebiTax+stt+serviceTax+exchangeTransactionCharges+stampDuty
        assert fill["taxes_and_charges"] == pytest.approx(expected)

    def test_a_single_token_custom_symbol_is_treated_as_a_plain_equity(self):
        # Not yet confirmed live (every fill seen so far was an option) --
        # inferred by analogy with dhan_positions_from_api's non-derivative
        # handling, same as this function's own docstring notes.
        row = {**self._LIVE_PUT_FILL, "customSymbol": "SBIN", "drvExpiryDate": "0001-01-01", "drvOptionType": None, "drvStrikePrice": None}
        fill = portfolio_service.dhan_trade_fills_from_api([row])[0]
        assert fill["symbol"] == "SBIN"
        assert fill["expiry_date"] is None
        assert fill["strike_price"] is None
        assert fill["option_type"] is None


class TestTradeFillsToRecords:
    def test_builds_portfolio_trade_fill_models(self):
        fills = [
            {
                "exchange_trade_id": "T1",
                "order_id": "O1",
                "raw_name": "SBIN",
                "symbol": "SBIN",
                "expiry_date": None,
                "strike_price": None,
                "option_type": None,
                "transaction_type": "BUY",
                "qty": 10,
                "price": 900.0,
                "product_type": "CNC",
                "traded_at": datetime(2026, 8, 1, 9, 30),
                "brokerage": 20.0,
                "taxes_and_charges": 5.0,
            }
        ]
        records = portfolio_service.trade_fills_to_records("u1", "Portfolio 1", "Dhan", fills)
        assert len(records) == 1
        assert records[0].user_id == "u1"
        assert records[0].exchange_trade_id == "T1"
        assert records[0].symbol == "SBIN"
        assert records[0].transaction_type == "BUY"
        assert records[0].qty == 10
        assert records[0].brokerage == 20.0
        assert records[0].taxes_and_charges == 5.0

    def test_charges_default_to_zero_when_absent(self):
        fills = [
            {
                "exchange_trade_id": "T1",
                "raw_name": "SBIN",
                "symbol": "SBIN",
                "expiry_date": None,
                "strike_price": None,
                "option_type": None,
                "transaction_type": "SELL",
                "qty": 10,
                "price": 900.0,
                "traded_at": datetime(2026, 8, 1, 9, 30),
            }
        ]
        records = portfolio_service.trade_fills_to_records("u1", "Portfolio 1", "Dhan", fills)
        assert records[0].brokerage == 0
        assert records[0].taxes_and_charges == 0


class TestComputeRealizedPnl:
    _next_id = 0

    def _fill(self, **overrides) -> PortfolioTradeFill:
        TestComputeRealizedPnl._next_id += 1
        base = dict(
            user_id="u1",
            portfolio_name="Portfolio 1",
            broker="Dhan",
            exchange_trade_id=f"T{TestComputeRealizedPnl._next_id}",
            raw_name="SBIN",
            symbol="SBIN",
            expiry_date=None,
            strike_price=None,
            option_type=None,
            transaction_type="BUY",
            qty=10,
            price=100.0,
            traded_at=datetime(2026, 8, 1, 9, 30),
            brokerage=0.0,
            taxes_and_charges=0.0,
        )
        base.update(overrides)
        return PortfolioTradeFill(**base)

    def test_fully_open_position_emits_nothing(self):
        fills = [self._fill(transaction_type="BUY", qty=10, price=100.0)]
        assert portfolio_service.compute_realized_pnl(fills) == []

    def test_full_close_of_a_long_position(self):
        fills = [
            self._fill(transaction_type="BUY", qty=100, price=10.0, traded_at=datetime(2026, 8, 1, 9, 30)),
            self._fill(transaction_type="SELL", qty=100, price=12.0, traded_at=datetime(2026, 8, 2, 9, 30)),
        ]
        closed = portfolio_service.compute_realized_pnl(fills)
        assert len(closed) == 1
        assert closed[0]["qty_closed"] == 100
        assert closed[0]["entry_price"] == 10.0
        assert closed[0]["exit_price"] == 12.0
        assert closed[0]["gross_pnl"] == pytest.approx(200.0)
        assert closed[0]["net_pnl"] == pytest.approx(200.0)  # no charges in this fixture

    def test_partial_close_leaves_the_remainder_unclosed(self):
        fills = [
            self._fill(transaction_type="BUY", qty=100, price=10.0, traded_at=datetime(2026, 8, 1)),
            self._fill(transaction_type="SELL", qty=40, price=12.0, traded_at=datetime(2026, 8, 2)),
        ]
        closed = portfolio_service.compute_realized_pnl(fills)
        assert len(closed) == 1
        assert closed[0]["qty_closed"] == 40
        assert closed[0]["gross_pnl"] == pytest.approx(80.0)

    def test_multiple_opens_are_closed_oldest_first(self):
        fills = [
            self._fill(transaction_type="BUY", qty=50, price=10.0, traded_at=datetime(2026, 8, 1)),
            self._fill(transaction_type="BUY", qty=50, price=11.0, traded_at=datetime(2026, 8, 2)),
            self._fill(transaction_type="SELL", qty=100, price=15.0, traded_at=datetime(2026, 8, 3)),
        ]
        closed = portfolio_service.compute_realized_pnl(fills)
        assert len(closed) == 2
        assert closed[0]["entry_price"] == 10.0
        assert closed[0]["qty_closed"] == 50
        assert closed[1]["entry_price"] == 11.0
        assert closed[1]["qty_closed"] == 50

    def test_short_position_close_profits_when_bought_back_lower(self):
        fills = [
            self._fill(transaction_type="SELL", qty=100, price=20.0, traded_at=datetime(2026, 8, 1)),
            self._fill(transaction_type="BUY", qty=100, price=15.0, traded_at=datetime(2026, 8, 2)),
        ]
        closed = portfolio_service.compute_realized_pnl(fills)
        assert len(closed) == 1
        assert closed[0]["gross_pnl"] == pytest.approx(500.0)

    def test_position_flip_opens_a_new_lot_in_the_new_direction(self):
        fills = [
            self._fill(transaction_type="BUY", qty=10, price=100.0, traded_at=datetime(2026, 8, 1)),
            self._fill(transaction_type="SELL", qty=15, price=110.0, traded_at=datetime(2026, 8, 2)),
            self._fill(transaction_type="BUY", qty=5, price=105.0, traded_at=datetime(2026, 8, 3)),
        ]
        closed = portfolio_service.compute_realized_pnl(fills)
        assert len(closed) == 2
        # First close: the original long 10 fully closed by part of the sell.
        assert closed[0]["qty_closed"] == 10
        assert closed[0]["entry_price"] == 100.0
        assert closed[0]["exit_price"] == 110.0
        assert closed[0]["gross_pnl"] == pytest.approx(100.0)
        # Second close: the flip-opened short 5 (at the sell's own price) closed by the final buy.
        assert closed[1]["qty_closed"] == 5
        assert closed[1]["entry_price"] == 110.0
        assert closed[1]["exit_price"] == 105.0
        assert closed[1]["gross_pnl"] == pytest.approx(25.0)

    def test_different_contracts_on_the_same_symbol_never_cross_match(self):
        stock_buy = self._fill(
            symbol="RELIANCE", strike_price=None, option_type=None, expiry_date=None,
            transaction_type="BUY", qty=10, price=2500.0, traded_at=datetime(2026, 8, 1),
        )
        option_sell = self._fill(
            symbol="RELIANCE", strike_price=2500.0, option_type=OptionType.CE, expiry_date=date(2026, 8, 25),
            transaction_type="SELL", qty=10, price=2500.0, traded_at=datetime(2026, 8, 1),
        )
        closed = portfolio_service.compute_realized_pnl([stock_buy, option_sell])
        assert closed == []  # both remain open in their own separate contract-identity group

    def test_charges_are_prorated_by_qty_across_partial_closes(self):
        fills = [
            self._fill(
                transaction_type="BUY", qty=100, price=10.0, traded_at=datetime(2026, 8, 1),
                brokerage=10.0, taxes_and_charges=10.0,  # 0.20/unit total
            ),
            self._fill(
                transaction_type="SELL", qty=40, price=12.0, traded_at=datetime(2026, 8, 2),
                brokerage=0.0, taxes_and_charges=0.0,
            ),
            self._fill(
                transaction_type="SELL", qty=60, price=12.0, traded_at=datetime(2026, 8, 3),
                brokerage=0.0, taxes_and_charges=0.0,
            ),
        ]
        closed = portfolio_service.compute_realized_pnl(fills)
        assert len(closed) == 2
        assert closed[0]["charges"] == pytest.approx(0.20 * 40)
        assert closed[1]["charges"] == pytest.approx(0.20 * 60)
        # Entry charge is spent exactly once in total across both closes.
        assert closed[0]["charges"] + closed[1]["charges"] == pytest.approx(20.0)
