"""Tests for portfolio_service: CSV parsing for both broker formats,
name-to-symbol matching, cross-broker merging, and valuation math. The
sample CSV bodies below are the real Zerodha/Dhan export shapes this
feature was built against."""
import io

import pytest

from src.models.company import Company
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


class TestHoldingsToRecords:
    def test_builds_portfolio_holding_models(self):
        holdings = [{"raw_name": "SBIN", "symbol": "SBIN", "qty": 10, "avg_price": 900, "investment": 9000}]
        records = portfolio_service.holdings_to_records("u1", "Zerodha", holdings)
        assert len(records) == 1
        assert records[0].user_id == "u1"
        assert records[0].broker == "Zerodha"
        assert records[0].symbol == "SBIN"
