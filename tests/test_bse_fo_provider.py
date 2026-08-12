from datetime import date

import pytest

from src.data_providers import bse_fo_provider
from src.data_providers.base import ProviderError
from src.models.enums import OptionType

# Same UDiFF schema as NSE's bhavcopy (tests/test_nse_fo_provider.py exercises
# the shared parsing logic -- field mapping, empty-cell handling --
# thoroughly via that module's identical wiring). This fixture only needs
# to prove BSE's own wiring: its own URL, its own `source` tag, that
# SENSEX/BANKEX (confirmed live -- see bse_fo_provider.py's file header)
# come through as index options, and that BSE's own stock futures/options
# rows are excluded entirely (BSE's stock-level F&O liquidity is
# negligible -- NSE is the sole stock F&O source; see _OPTION_TYPES'
# comment in bse_fo_provider.py).
HEADER = (
    "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,"
    "FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,"
    "LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,"
    "TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4"
)
ROWS = [
    # SENSEX 83900 CE (IDO) -- real shape confirmed live against BSE's bhavcopy
    "2026-08-07,2026-08-07,FO,BSE,IDO,839781,,SENSEX,,2026-08-27,2026-08-27,83900.00,CE,"
    "SENSEX26AUG83900CE,0.00,0.00,0.00,16.50,0.00,91.30,78499.17,74.25,220,0,0,0.00,0,F1,20,,,,,",
    # BANKEX 65100 CE (IDO)
    "2026-08-07,2026-08-07,FO,BSE,IDO,828071,,BANKEX,,2026-08-27,2026-08-27,65100.00,CE,"
    "BANKEX26AUG65100CE,0.00,0.00,0.00,1120.05,0.00,1198.50,65492.23,1598.46,990,0,0,0.00,0,F1,30,,,,,",
    # RELIANCE future (STF) -- BSE carries these rows, but they're
    # deliberately excluded (illiquid) -- must NOT appear in futures_prices.
    "2026-08-07,2026-08-07,FO,BSE,STF,140001,,RELIANCE,,2026-08-27,2026-08-27,,,"
    "RELIANCE26AUGFUT,1300.00,1313.50,1296.00,1299.10,1299.10,1305.00,1296.00,1299.10,"
    "105054000,-756000,20697,1000000.00,15000,F1,500,,,,,",
    # RELIANCE 1300 CE (STO) -- same story: BSE carries it, must be dropped.
    "2026-08-07,2026-08-07,FO,BSE,STO,140002,,RELIANCE,,2026-08-27,2026-08-27,1300.00,CE,"
    "RELIANCE26AUG1300CE,0.00,0.00,0.00,25.00,0.00,30.00,1299.10,28.50,150,0,0,0.00,0,F1,500,,,,,",
    # SENSEX index future (IDF) -- must be ignored, same as NSE
    "2026-08-07,2026-08-07,FO,BSE,IDF,140004,,SENSEX,,2026-08-27,2026-08-27,,,"
    "SENSEX26AUGFUT,78000.00,78500.00,77500.00,78200.00,78200.00,78000.00,78499.17,78200.00,"
    "500000,10000,2000,999.00,900,F1,20,,,,,",
]
SAMPLE_CSV = HEADER + "\n" + "\n".join(ROWS) + "\n"


class TestBhavcopyUrl:
    def test_uses_bseindia_host_and_plain_csv_extension(self):
        url = bse_fo_provider.bhavcopy_url(date(2026, 8, 7))
        assert url == "https://www.bseindia.com/download/Bhavcopy/Derivative/BhavCopy_BSE_FO_0_0_0_20260807_F_0000.CSV"


class TestParseFoBhavcopy:
    def test_keeps_only_index_options_drops_stock_futures_and_options_and_index_futures(self):
        book = bse_fo_provider.parse_fo_bhavcopy(SAMPLE_CSV)
        assert {p.symbol for p in book.option_prices} == {"SENSEX", "BANKEX"}
        assert book.futures_prices == []
        assert book.futures_contracts == []

    def test_stamps_its_own_source_name(self):
        book = bse_fo_provider.parse_fo_bhavcopy(SAMPLE_CSV)
        assert all(p.source == "bse_fo_bhavcopy" for p in book.option_prices)
        assert all(p.source == "bse_fo_bhavcopy" for p in book.futures_prices)
        assert bse_fo_provider.SOURCE_NAME == "bse_fo_bhavcopy"

    def test_sensex_option_field_mapping(self):
        book = bse_fo_provider.parse_fo_bhavcopy(SAMPLE_CSV, universe={"SENSEX"})
        opt = book.option_prices[0]
        assert opt.symbol == "SENSEX"
        assert opt.strike_price == 83900.0
        assert opt.option_type == OptionType.CE
        assert opt.close == 16.5
        assert opt.underlying_price == 78499.17

    def test_universe_filter(self):
        book = bse_fo_provider.parse_fo_bhavcopy(SAMPLE_CSV, universe={"BANKEX"})
        assert {p.symbol for p in book.option_prices} == {"BANKEX"}
        assert book.futures_prices == []


class _FakeResponse:
    def __init__(self, status_code: int, content: bytes):
        self.status_code = status_code
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeSession:
    def __init__(self, response: _FakeResponse):
        self._response = response

    def get(self, url, headers=None, timeout=None):
        return self._response


class TestDownloadBhavcopyCsv:
    def test_404_returns_none(self):
        session = _FakeSession(_FakeResponse(404, b""))
        assert bse_fo_provider.download_bhavcopy_csv(date(2026, 8, 11), session=session) is None

    def test_near_empty_body_returns_none(self):
        session = _FakeSession(_FakeResponse(200, b"too small"))
        assert bse_fo_provider.download_bhavcopy_csv(date(2026, 8, 11), session=session) is None

    def test_real_csv_is_returned(self):
        content = SAMPLE_CSV.encode("utf-8") + b"," * 500  # pad past the 500-byte guard
        session = _FakeSession(_FakeResponse(200, content))
        result = bse_fo_provider.download_bhavcopy_csv(date(2026, 8, 11), session=session)
        assert result is not None
        assert result.startswith("TradDt,")

    def test_non_csv_body_past_the_size_guard_raises_instead_of_silently_matching_nothing(self):
        # Real bug this guards against: BSE's bot-detection can serve a
        # >500-byte, non-CSV response to a blocked network origin -- it
        # decodes and "parses" fine, it just matches zero universe
        # symbols, so a caller previously saw a silent false-success
        # ("0 futures + 0 option rows") instead of a diagnostic error.
        # Confirmed live: the identical URL/date returned a real,
        # fully-populated bhavcopy from a normal dev machine at the same
        # time a deployed Edge Function reported 0 rows for it.
        blocked_page = "<html><body>Access denied by security policy</body></html>" + " " * 500
        session = _FakeSession(_FakeResponse(200, blocked_page.encode("utf-8")))
        with pytest.raises(ProviderError, match="did not return a bhavcopy CSV"):
            bse_fo_provider.download_bhavcopy_csv(date(2026, 8, 11), session=session)
