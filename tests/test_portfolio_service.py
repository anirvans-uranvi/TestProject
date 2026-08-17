"""Tests for portfolio_service: CSV parsing for both broker formats,
name-to-symbol matching, cross-broker merging, and valuation math. The
sample CSV bodies below are the real Zerodha/Dhan export shapes this
feature was built against."""
import io
from datetime import date

import pytest

from src.models.company import Company
from src.models.enums import CompanyType, OptionType
from src.services import portfolio_service

ZERODHA_CSV = """"Instrument","Qty.","Avg. cost","LTP","Invested","Cur. val","P&L","Net chg.","Day chg.",""
"GILT5YBEES",7500,64.3,65.95,482250,494625,12375,2.57,0.05,""
"INDHOTEL",10,660.8,723.4,6608,7234,626,9.47,-0.26,""
"LIQUIDCASE",14000,114.12,115.12,1597631.99,1611680,14048.01,0.88,0.01,""
"LTGILTCASE",50000,29.67,30.44,1483500,1522000,38500,2.6,-0.13,""
"NIFTYBEES",6000,266.6,272.99,1599600,1637940,38340,2.4,-0.14,""
"SBIN",1500,974.2,1021.1,1461300,1531650,70350,4.81,-0.41,""
"VAML",1150,441,438.45,507150,504217.5,-2932.5,-0.58,1.66,""
"""

DHAN_CSV = """"Name","Quantity","Avg Price","Last Traded","Investment","Current Value","P&L","P&L %"
"Coal India",1350,"475.88","427.90","6,42,438.40","5,77,665.00","-64,773.40","-10.08%"
"HDFC Bank",1300,"831.89","746.70","10,81,452.10","9,70,710.00","-1,10,742.10","-10.24%"
"Hindustan Zinc",1225,"621.64","538.55","7,61,505.33","6,59,723.75","-1,01,781.57","-13.37%"
"Indusind Bank",700,"975.64","1,015.35","6,82,947.15","7,10,745.00","27,797.85","4.07%"
"Oil & Natural Gas Corporation",2250,"234.07","252.37","5,26,668.30","5,67,832.50","41,164.20","7.82%"
"Tata Motors Passenger Vehicles",3200,"467.60","328.10","14,96,333.60","10,49,920.00","-4,46,413.60","-29.83%"
"""


def _companies() -> list[Company]:
    return [
        Company(symbol="SBIN", name="State Bank of India Ltd"),
        Company(symbol="COALINDIA", name="Coal India Ltd"),
        Company(symbol="HDFCBANK", name="HDFC Bank Ltd"),
        Company(symbol="ONGC", name="Oil & Natural Gas Corporation Ltd"),
        Company(symbol="TMPV", name="Tata Motors Passenger Vehicles Ltd"),
    ]


class TestParseZerodhaCsv:
    def test_uses_instrument_column_as_symbol_directly(self):
        holdings = portfolio_service.parse_zerodha_csv(io.StringIO(ZERODHA_CSV))
        assert len(holdings) == 7
        sbin = next(h for h in holdings if h["raw_name"] == "SBIN")
        assert sbin["symbol"] == "SBIN"
        assert sbin["qty"] == 1500
        assert sbin["avg_price"] == 974.2
        assert sbin["investment"] == 1461300

    def test_ignores_the_files_own_ltp_and_pnl_columns(self):
        holdings = portfolio_service.parse_zerodha_csv(io.StringIO(ZERODHA_CSV))
        for h in holdings:
            assert set(h.keys()) == {"raw_name", "symbol", "qty", "avg_price", "investment"}


class TestMatchSymbol:
    def test_matches_a_shortened_broker_name(self):
        assert portfolio_service.match_symbol("Coal India", _companies()) == "COALINDIA"

    def test_matches_with_bank_suffix_shared(self):
        assert portfolio_service.match_symbol("HDFC Bank", _companies()) == "HDFCBANK"

    def test_matches_long_multiword_name(self):
        assert portfolio_service.match_symbol("Tata Motors Passenger Vehicles", _companies()) == "TMPV"

    def test_returns_none_when_no_company_matches(self):
        assert portfolio_service.match_symbol("Hindustan Zinc", _companies()) is None

    def test_returns_none_for_ambiguous_match(self):
        companies = [
            Company(symbol="A", name="Tata Motors Ltd"),
            Company(symbol="B", name="Tata Motors Passenger Vehicles Ltd"),
        ]
        assert portfolio_service.match_symbol("Tata Motors", companies) is None


class TestParseDhanCsv:
    def test_matches_known_companies_and_leaves_others_unresolved(self):
        holdings = portfolio_service.parse_dhan_csv(io.StringIO(DHAN_CSV), _companies())
        by_name = {h["raw_name"]: h for h in holdings}
        assert by_name["Coal India"]["symbol"] == "COALINDIA"
        assert by_name["HDFC Bank"]["symbol"] == "HDFCBANK"
        assert by_name["Oil & Natural Gas Corporation"]["symbol"] == "ONGC"
        assert by_name["Tata Motors Passenger Vehicles"]["symbol"] == "TMPV"
        assert by_name["Hindustan Zinc"]["symbol"] is None
        assert by_name["Indusind Bank"]["symbol"] is None

    def test_parses_indian_grouped_quoted_numbers(self):
        holdings = portfolio_service.parse_dhan_csv(io.StringIO(DHAN_CSV), _companies())
        coal_india = next(h for h in holdings if h["raw_name"] == "Coal India")
        assert coal_india["qty"] == 1350
        assert coal_india["avg_price"] == pytest.approx(475.88)
        assert coal_india["investment"] == pytest.approx(642438.40)


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


ZERODHA_POSITIONS_CSV = """"Product","Instrument","Qty.","Avg.","LTP","P&L","Chg.",""
"NRML","NIFTY2681123000PE",-780,10.15,1.1,7059,-89.16,""
"NRML","NIFTY2681124000PE",780,56.99,8,-38210.25,-85.96,""
"NRML","NIFTY2681124200CE",780,159.63,383.45,174580.25,140.21,""
"""

DHAN_POSITIONS_CSV = """"Name","Product","Qty","Avg Price","LTP","P&L","% Change","Action"
"HINDALCO 25 AUG 860 PUT","Normal","-700.00","2.55","1.05","1,050.00","58.82"
"ONGC 25 AUG 260 CALL","Normal","-2,250.00","2.95","0.62","5,242.50","78.98"
"NIFTY 11 AUG 24200 CALL","Normal","780.00","162.00","395.00","1,81,740.00","143.83"
"""


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


class TestParseZerodhaPositionsCsv:
    def test_decodes_every_row_and_keeps_signed_qty(self):
        positions = portfolio_service.parse_zerodha_positions_csv(io.StringIO(ZERODHA_POSITIONS_CSV))
        assert len(positions) == 3
        short_put = next(p for p in positions if p["raw_name"] == "NIFTY2681123000PE")
        assert short_put["symbol"] == "NIFTY"
        assert short_put["qty"] == -780
        assert short_put["avg_price"] == 10.15
        assert short_put["ltp"] == 1.1
        assert short_put["expiry_date"] == date(2026, 8, 11)
        assert short_put["strike_price"] == 23000.0
        assert short_put["option_type"] == OptionType.PE


class TestParseDhanPositionName:
    def test_decodes_put_with_explicit_reference_date(self):
        decoded = portfolio_service.parse_dhan_position_name("ONGC 25 AUG 230 PUT", as_of=date(2026, 8, 7))
        assert decoded == {
            "symbol": "ONGC",
            "expiry_date": date(2026, 8, 25),
            "strike_price": 230.0,
            "option_type": OptionType.PE,
        }

    def test_infers_next_year_when_date_has_already_passed_this_year(self):
        decoded = portfolio_service.parse_dhan_position_name("NIFTY 15 JAN 24000 CALL", as_of=date(2026, 8, 7))
        assert decoded["expiry_date"] == date(2027, 1, 15)

    def test_returns_none_for_unrecognized_format(self):
        assert portfolio_service.parse_dhan_position_name("SOME FUTURE CONTRACT", as_of=date(2026, 8, 7)) is None


class TestParseDhanPositionsCsv:
    def test_decodes_indian_grouped_numbers_and_signed_qty(self):
        positions = portfolio_service.parse_dhan_positions_csv(io.StringIO(DHAN_POSITIONS_CSV), as_of=date(2026, 8, 7))
        assert len(positions) == 3
        ongc = next(p for p in positions if p["raw_name"] == "ONGC 25 AUG 260 CALL")
        assert ongc["symbol"] == "ONGC"
        assert ongc["qty"] == -2250
        assert ongc["avg_price"] == pytest.approx(2.95)
        assert ongc["ltp"] == pytest.approx(0.62)
        assert ongc["expiry_date"] == date(2026, 8, 25)
        assert ongc["option_type"] == OptionType.CE


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


class TestGroupIntoTrades:
    def _leg(self, *, raw_name, broker, symbol, leg_type, pnl):
        return {"raw_name": raw_name, "broker": broker, "symbol": symbol, "leg_type": leg_type, "pnl": pnl}

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
