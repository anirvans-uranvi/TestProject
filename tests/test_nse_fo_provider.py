from datetime import date

from src.data_providers.nse_fo_provider import bhavcopy_url, parse_fo_bhavcopy
from src.models.enums import OptionType

# A trimmed but real-shaped UDiFF F&O bhavcopy fragment: one stock future
# (STF), two stock options (STO CE/PE), one index future (IDF) and one row
# for a symbol outside a restricted universe -- to exercise all the filters.
HEADER = (
    "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,"
    "FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,"
    "LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,"
    "TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4"
)
ROWS = [
    # RELIANCE July future (STF)
    "2026-07-16,2026-07-16,FO,NSE,STF,140001,,RELIANCE,,2026-07-28,2026-07-28,,,"
    "RELIANCE26JULFUT,1300.00,1313.50,1296.00,1299.10,1299.10,1305.00,1296.00,1299.10,"
    "105054000,-756000,20697,1000000.00,15000,F1,500,,,,,",
    # RELIANCE 1300 CE (STO)
    "2026-07-16,2026-07-16,FO,NSE,STO,140002,,RELIANCE,,2026-07-28,2026-07-28,1300.00,CE,"
    "RELIANCE26JUL1300CE,25.00,30.00,22.00,28.00,27.50,29.00,1296.00,28.00,975000,163000,"
    "662,4400000.00,585,F1,500,,,,,",
    # RELIANCE 1300 PE (STO)
    "2026-07-16,2026-07-16,FO,NSE,STO,140003,,RELIANCE,,2026-07-28,2026-07-28,1300.00,PE,"
    "RELIANCE26JUL1300PE,35.05,41.35,32.00,39.00,39.70,38.90,1296.00,39.00,502500,0,183,"
    "1090100.00,140,F1,500,,,,,",
    # NIFTY index future (IDF) -- must be ignored
    "2026-07-16,2026-07-16,FO,NSE,IDF,140004,,NIFTY,,2026-07-30,2026-07-30,,,"
    "NIFTY26JULFUT,25000.00,25100.00,24900.00,25050.00,25050.00,25010.00,25040.00,25050.00,"
    "12000000,50000,300000,9999.00,90000,F1,65,,,,,",
    # TCS future -- outside a {RELIANCE} universe filter
    "2026-07-16,2026-07-16,FO,NSE,STF,140005,,TCS,,2026-07-28,2026-07-28,,,"
    "TCS26JULFUT,3800.00,3820.00,3790.00,3805.00,3805.00,3810.00,3802.00,3805.00,"
    "8000000,10000,5000,1900000.00,3000,F1,175,,,,,",
]
SAMPLE_CSV = HEADER + "\n" + "\n".join(ROWS) + "\n"


class TestParseFoBhavcopy:
    def test_splits_futures_and_options(self):
        book = parse_fo_bhavcopy(SAMPLE_CSV)
        # RELIANCE + TCS futures; RELIANCE CE + PE options; NIFTY (IDF) ignored
        assert len(book.futures_prices) == 2
        assert len(book.option_prices) == 2
        assert {p.symbol for p in book.futures_prices} == {"RELIANCE", "TCS"}
        assert {p.symbol for p in book.option_prices} == {"RELIANCE"}

    def test_universe_filter(self):
        book = parse_fo_bhavcopy(SAMPLE_CSV, universe={"RELIANCE"})
        assert {p.symbol for p in book.futures_prices} == {"RELIANCE"}
        assert all(p.symbol == "RELIANCE" for p in book.option_prices)
        assert len(book.futures_prices) == 1

    def test_ignores_index_derivatives(self):
        book = parse_fo_bhavcopy(SAMPLE_CSV)
        assert "NIFTY" not in {p.symbol for p in book.futures_prices}

    def test_futures_field_mapping(self):
        book = parse_fo_bhavcopy(SAMPLE_CSV, universe={"RELIANCE"})
        fut = book.futures_prices[0]
        assert fut.expiry_date == date(2026, 7, 28)
        assert fut.trade_date == date(2026, 7, 16)
        assert fut.open == 1300.0
        assert fut.high == 1313.5
        assert fut.close == 1299.1
        assert fut.settlement_price == 1299.1
        assert fut.underlying_price == 1296.0
        assert fut.open_interest == 105054000
        assert fut.change_in_oi == -756000
        assert fut.volume == 20697
        contract = book.futures_contracts[0]
        assert contract.lot_size == 500
        assert contract.contract_name == "RELIANCE26JULFUT"
        assert contract.nse_token == "140001"

    def test_option_field_mapping(self):
        book = parse_fo_bhavcopy(SAMPLE_CSV, universe={"RELIANCE"})
        ce = next(p for p in book.option_prices if p.option_type == OptionType.CE)
        pe = next(p for p in book.option_prices if p.option_type == OptionType.PE)
        assert ce.strike_price == 1300.0
        assert ce.close == 28.0  # ClsPric column
        assert ce.last_price == 27.5  # LastPric column (distinct from close)
        assert ce.open_interest == 975000
        assert ce.change_in_oi == 163000
        assert pe.strike_price == 1300.0
        assert pe.close == 39.0
        assert pe.last_price == 39.7
        assert pe.open_interest == 502500

    def test_empty_and_missing_values(self):
        # A row with blank numeric cells should parse to None, not crash.
        csv = HEADER + "\n" + (
            "2026-07-16,2026-07-16,FO,NSE,STO,140006,,RELIANCE,,2026-07-28,2026-07-28,1400.00,CE,"
            "RELIANCE26JUL1400CE,,,,,,,,,,,,,,F1,,,,,,\n"
        )
        book = parse_fo_bhavcopy(csv, universe={"RELIANCE"})
        assert len(book.option_prices) == 1
        opt = book.option_prices[0]
        assert opt.last_price is None
        assert opt.open_interest is None
        assert opt.strike_price == 1400.0

    def test_bhavcopy_url_format(self):
        url = bhavcopy_url(date(2026, 7, 16))
        assert url.endswith("BhavCopy_NSE_FO_0_0_0_20260716_F_0000.csv.zip")
        assert "nsearchives.nseindia.com" in url
