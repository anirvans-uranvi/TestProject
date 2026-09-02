"""Tests for DhanProvider's per-user portfolio-sync methods (get_holdings,
get_positions, get_ltp_by_security_id) -- header construction and the
401 -> DhanAuthError mapping used by pages/6_My_Broker.py's "Connect Dhan
account" flow. get_historical_daily is untouched and untested here.

Also covers the F&O instrument resolution added for live ETF/futures/
option LTP (migration 0032): _load_fo_instrument_master's column
resolution and NSE/FUTSTK-OPTSTK filtering, resolve_fo_security_id's
underlying+expiry+strike+option_type matching, and get_fo_quotes'
batching/skip-unresolved behavior -- plus get_quotes' own matching
skip-unresolved fix, added after a live regression: widening the
watched-symbol universe to every tracked ETF (migration 0032) meant one
ETF Dhan's equity instrument master didn't carry made get_quotes raise
and silently wipe out live pricing for the ENTIRE batch, not just that
one symbol.

Also covers _load_instrument_master/_load_fo_instrument_master's DB-cache
behavior when a `client` is given: no freshness check and no download
fallback at all -- a client-backed call only ever reads
dhan_equity_instruments/dhan_fo_instruments (migration 0035) verbatim,
however old, and raises pointing at Settings' "Refresh Instrument Master
- Dhan" button if the table has never been populated. That button
(refresh_dhan_instrument_master) is the only thing left that actually
downloads and persists this data -- see TestRefreshDhanInstrumentMaster."""
from datetime import date

import httpx
import pandas as pd
import pytest

import src.data_providers.dhan_provider as dhan_provider
from src.data_providers.base import ProviderError
from src.data_providers.dhan_provider import DhanAuthError, DhanProvider, resolve_fo_security_id


class _FakeResponse:
    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text or (str(json_data) if json_data is not None else "")

    def json(self):
        return self._json_data


@pytest.fixture(autouse=True)
def _no_throttle_sleep(monkeypatch):
    # _throttle() sleeps against the real wall clock -- neutralize it so
    # tests run instantly regardless of call order/timing.
    monkeypatch.setattr(dhan_provider, "_throttle", lambda: None)


class TestGetHoldings:
    def test_sends_client_id_and_access_token_headers(self, monkeypatch):
        captured = {}

        def fake_request(method, url, json=None, headers=None, timeout=None):
            captured["method"] = method
            captured["url"] = url
            captured["headers"] = headers
            return _FakeResponse(200, json_data=[{"tradingSymbol": "SBIN"}])

        monkeypatch.setattr(httpx, "request", fake_request)
        provider = DhanProvider(client_id="CID1", access_token="TOKEN1")

        result = provider.get_holdings()

        assert result == [{"tradingSymbol": "SBIN"}]
        assert captured["method"] == "GET"
        assert captured["url"].endswith("/holdings")
        assert captured["headers"]["client-id"] == "CID1"
        assert captured["headers"]["access-token"] == "TOKEN1"

    def test_401_raises_dhan_auth_error(self, monkeypatch):
        monkeypatch.setattr(httpx, "request", lambda *a, **k: _FakeResponse(401, text="bad token"))
        provider = DhanProvider(client_id="CID1", access_token="EXPIRED")

        with pytest.raises(DhanAuthError):
            provider.get_holdings()

    def test_other_error_status_raises_generic_provider_error_not_auth_error(self, monkeypatch):
        monkeypatch.setattr(httpx, "request", lambda *a, **k: _FakeResponse(500, text="boom"))
        provider = DhanProvider(client_id="CID1", access_token="TOKEN1")

        with pytest.raises(ProviderError) as exc_info:
            provider.get_holdings()
        assert not isinstance(exc_info.value, DhanAuthError)


# Shaped like a real download (confirmed live against
# https://images.dhan.co/api-data/api-scrip-master.csv): SEM_SEGMENT is
# a single letter ('E' for equity, not "EQ" -- see _load_instrument_master's
# docstring for the real bug this caused), the instrument-type column
# (SEM_INSTRUMENT_NAME) is what actually says "EQUITY".
_EQUITY_MASTER_FIXTURE = pd.DataFrame(
    [
        {"SEM_SMST_SECURITY_ID": "1001", "SEM_TRADING_SYMBOL": "RELIANCE", "SEM_EXM_EXCH_ID": "NSE", "SEM_SEGMENT": "E", "SEM_INSTRUMENT_NAME": "EQUITY"},
        {"SEM_SMST_SECURITY_ID": "1002", "SEM_TRADING_SYMBOL": "TCS", "SEM_EXM_EXCH_ID": "NSE", "SEM_SEGMENT": "E", "SEM_INSTRUMENT_NAME": "EQUITY"},
        # A derivatives-segment row sharing NSE -- must be excluded by
        # the instrument-type filter, not just the exchange one.
        {"SEM_SMST_SECURITY_ID": "1003", "SEM_TRADING_SYMBOL": "RELIANCE-Aug2026-FUT", "SEM_EXM_EXCH_ID": "NSE", "SEM_SEGMENT": "D", "SEM_INSTRUMENT_NAME": "FUTSTK"},
        # No row for "BADETF" -- simulates a tracked symbol Dhan's equity
        # instrument master doesn't carry.
    ]
)

_EMPTY_MASTER_FIXTURE = pd.DataFrame(
    columns=["SEM_SMST_SECURITY_ID", "SEM_TRADING_SYMBOL", "SEM_EXM_EXCH_ID", "SEM_SEGMENT", "SEM_INSTRUMENT_NAME"]
)


class TestGetQuotes:
    def test_unresolvable_symbol_is_skipped_not_fatal_to_the_whole_batch(self, monkeypatch):
        # Regression: before this fix, one unresolvable symbol (e.g. an
        # ETF Dhan's equity master doesn't carry) made resolve_security_id
        # raise inside the dict comprehension, aborting get_quotes
        # entirely -- load_live_dhan_prices' caller then caught that as a
        # ProviderError and returned {} for the WHOLE batch, silently
        # wiping out live pricing for every other (perfectly resolvable)
        # symbol too. Confirmed live after widening the watched-symbol
        # universe to every tracked ETF (migration 0032).
        monkeypatch.setattr(dhan_provider.pd, "read_csv", lambda *a, **k: _EQUITY_MASTER_FIXTURE)

        def fake_post(url, json=None, headers=None, timeout=None):
            assert set(json["NSE_EQ"]) == {1001, 1002}
            return _FakeResponse(
                200,
                json_data={"data": {"NSE_EQ": {"1001": {"last_price": 2950.0}, "1002": {"last_price": 4100.0}}}},
            )

        monkeypatch.setattr(httpx, "post", fake_post)
        provider = DhanProvider(client_id="CID1", access_token="TOKEN1")

        result = provider.get_quotes(["RELIANCE", "TCS", "BADETF"])

        assert set(result) == {"RELIANCE", "TCS"}
        assert result["RELIANCE"].latest_price == 2950.0
        assert result["TCS"].latest_price == 4100.0

    def test_all_symbols_unresolvable_returns_empty_without_a_request(self, monkeypatch):
        monkeypatch.setattr(dhan_provider.pd, "read_csv", lambda *a, **k: _EQUITY_MASTER_FIXTURE)

        def fake_post(*args, **kwargs):
            raise AssertionError("should not make a request when nothing resolved")

        monkeypatch.setattr(httpx, "post", fake_post)
        provider = DhanProvider(client_id="CID1", access_token="TOKEN1")

        assert provider.get_quotes(["BADETF1", "BADETF2"]) == {}

    def test_instrument_master_matching_zero_rows_raises_instead_of_looking_like_no_symbols_resolved(self, monkeypatch):
        # Regression: _load_instrument_master's equity filter used to
        # silently match zero rows against a real download (see its
        # docstring) -- confirms that's a loud ProviderError now, not a
        # quietly empty master indistinguishable from "these particular
        # symbols don't exist".
        monkeypatch.setattr(dhan_provider.pd, "read_csv", lambda *a, **k: _EMPTY_MASTER_FIXTURE)

        with pytest.raises(ProviderError, match="zero"):
            dhan_provider._load_instrument_master()

    def test_systemic_instrument_master_failure_propagates_not_swallowed_per_symbol(self, monkeypatch):
        # Regression: get_quotes used to call resolve_security_id (and
        # therefore _load_instrument_master) inside the per-symbol
        # try/except ProviderError loop -- since @lru_cache never caches
        # a raised exception, a systemic failure (download error, or the
        # zero-rows case above) re-triggered on every symbol and got
        # silently caught there too, indistinguishable from "just didn't
        # resolve". get_quotes now loads the master once, up front,
        # outside that loop, so this propagates as a real exception.
        monkeypatch.setattr(dhan_provider.pd, "read_csv", lambda *a, **k: _EMPTY_MASTER_FIXTURE)

        def fake_post(*args, **kwargs):
            raise AssertionError("should never reach the network call")

        monkeypatch.setattr(httpx, "post", fake_post)
        provider = DhanProvider(client_id="CID1", access_token="TOKEN1")

        with pytest.raises(ProviderError, match="zero"):
            provider.get_quotes(["SBIN", "TCS", "RELIANCE"])


class TestLoadInstrumentMasterDbCache:
    """Migration 0035: the shared dhan_equity_instruments table.
    _load_instrument_master no longer has any freshness concept or
    download fallback at all when `client` is given -- confirmed live as
    the real cause of "Stock & Option Data Refresh" being unpredictably
    slow (whichever process/worker judged the cache "not fresh today"
    paid for a full ~211,742-row CSV download on that click). Settings'
    "Refresh Instrument Master - Dhan" button (refresh_dhan_instrument_master)
    is now the only thing that downloads and persists this; a
    client-backed call here just reads the DB cache verbatim, however old
    it is, and raises if it's never been populated at all. `client` is
    never touched for real here -- dhan_instrument_repo is monkeypatched
    directly, so any non-None sentinel stands in for a real Supabase
    client."""

    def test_db_entry_is_used_without_downloading_no_matter_how_old(self, monkeypatch):
        def fail_download(*a, **k):
            raise AssertionError("a client-backed call must never download")

        monkeypatch.setattr(dhan_provider.pd, "read_csv", fail_download)
        monkeypatch.setattr(
            dhan_provider.dhan_instrument_repo,
            "get_equity_instruments",
            lambda client: [{"security_id": "1001", "trading_symbol": "RELIANCE"}],
        )

        df = dhan_provider._load_instrument_master(client=object())

        assert df["trading_symbol"].tolist() == ["RELIANCE"]

    def test_empty_db_table_raises_pointing_at_the_refresh_button_instead_of_downloading(self, monkeypatch):
        # Regression: this path used to silently download+persist on a
        # cache miss -- now that Stock & Option Data Refresh no longer
        # does that implicitly, a never-populated table must fail loudly
        # with actionable guidance, not fall back to a real download.
        def fail_download(*a, **k):
            raise AssertionError("a client-backed call must never download")

        monkeypatch.setattr(dhan_provider.pd, "read_csv", fail_download)
        monkeypatch.setattr(dhan_provider.dhan_instrument_repo, "get_equity_instruments", lambda client: [])

        with pytest.raises(ProviderError, match="Refresh Instrument Master"):
            dhan_provider._load_instrument_master(client=object())

    def test_client_none_still_downloads_for_script_or_cron_usage(self, monkeypatch):
        # The one context with no Supabase-backed cache to read at all
        # (factory.py's plain-cron wiring, or a test) -- must keep the
        # old real-download behavior since there's no alternative source.
        monkeypatch.setattr(dhan_provider.pd, "read_csv", lambda *a, **k: _EQUITY_MASTER_FIXTURE)

        df = dhan_provider._load_instrument_master(client=None)

        assert set(df["trading_symbol"]) == {"RELIANCE", "TCS"}


_COMBINED_RAW_FIXTURE = pd.DataFrame(
    [
        {
            "SEM_SMST_SECURITY_ID": "1001", "SEM_TRADING_SYMBOL": "RELIANCE", "SEM_EXM_EXCH_ID": "NSE",
            "SEM_SEGMENT": "E", "SEM_INSTRUMENT_NAME": "EQUITY", "SEM_EXPIRY_DATE": None,
            "SEM_STRIKE_PRICE": None, "SEM_OPTION_TYPE": None,
        },
        {
            "SEM_SMST_SECURITY_ID": "50001", "SEM_TRADING_SYMBOL": "RELIANCE-Aug2026-FUT", "SEM_EXM_EXCH_ID": "NSE",
            "SEM_SEGMENT": "D", "SEM_INSTRUMENT_NAME": "FUTSTK", "SEM_EXPIRY_DATE": "2026-08-27",
            "SEM_STRIKE_PRICE": -0.01, "SEM_OPTION_TYPE": "XX",
        },
    ]
)


class TestSharedRawInstrumentMasterDownload:
    """The actual fix for a still-slow "Stock & Option Data Refresh":
    _load_instrument_master and _load_fo_instrument_master used to each
    independently `pd.read_csv` the full Dhan file -- confirmed live as a
    cache-cold refresh downloading the same large CSV twice, concurrently,
    competing for the same outbound bandwidth. Both now route through
    _get_raw_instrument_master, which downloads at most once per IST day
    and hands out a fresh `.copy()` to each filter."""

    def test_equity_and_fo_loaders_share_one_download_when_both_cache_cold(self, monkeypatch):
        call_count = {"n": 0}

        def counting_read_csv(*a, **k):
            call_count["n"] += 1
            return _COMBINED_RAW_FIXTURE

        monkeypatch.setattr(dhan_provider.pd, "read_csv", counting_read_csv)

        equity_df = dhan_provider._load_instrument_master()
        fo_df = dhan_provider._load_fo_instrument_master()

        assert call_count["n"] == 1
        assert equity_df["trading_symbol"].tolist() == ["RELIANCE"]
        assert fo_df["underlying_symbol"].tolist() == ["RELIANCE"]

    def test_each_caller_gets_an_independent_copy_not_the_cached_object(self, monkeypatch):
        # Regression, confirmed live: pandas is not thread-safe for
        # concurrent reads of one shared DataFrame instance (lazy
        # internal caching mutates C-level state even on operations that
        # look read-only) -- handing the *same* cached object to both
        # legs' filter functions while they ran concurrently silently
        # corrupted the result for one or both (a live account saw "15 of
        # 61" equities and "0 of 348" F&O resolve, down from 61/61 and
        # 333+/351 before this cache existed). Each call must get its own
        # object, never a reference to what's sitting in the cache.
        monkeypatch.setattr(dhan_provider.pd, "read_csv", lambda *a, **k: _COMBINED_RAW_FIXTURE)

        first = dhan_provider._get_raw_instrument_master()
        second = dhan_provider._get_raw_instrument_master()

        assert first is not second
        today = dhan_provider.now_ist().date().isoformat()
        assert first is not dhan_provider._raw_master_cache[today]
        assert second is not dhan_provider._raw_master_cache[today]
        # Mutating one caller's copy must never leak into another's.
        first.loc[first.index[0], "SEM_TRADING_SYMBOL"] = "MUTATED"
        assert "MUTATED" not in second["SEM_TRADING_SYMBOL"].values


class TestGetPositions:
    def test_returns_raw_rows(self, monkeypatch):
        monkeypatch.setattr(httpx, "request", lambda *a, **k: _FakeResponse(200, json_data=[{"netQty": -780}]))
        provider = DhanProvider(client_id="CID1", access_token="TOKEN1")

        assert provider.get_positions() == [{"netQty": -780}]


class TestRenewAccessToken:
    """POST /v2/RenewToken -- extends a still-active token by 24h from
    within the app itself (Settings' "Renew Token (+24h)" button), the
    mobile-friendly alternative to re-pasting a token generated on
    web.dhan.co. Uses `dhanClientId` (camelCase), NOT the `client-id`
    header every other v2 endpoint on this class sends -- confirmed
    against Dhan's docs, this endpoint is documented to expect that name
    specifically."""

    def test_sends_dhan_client_id_header_and_returns_new_token(self, monkeypatch):
        captured = {}

        def fake_post(url, headers=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers
            return _FakeResponse(200, json_data={"accessToken": "NEW_TOKEN", "expiryTime": "..."})

        monkeypatch.setattr(httpx, "post", fake_post)
        provider = DhanProvider(client_id="CID1", access_token="OLD_TOKEN")

        result = provider.renew_access_token()

        assert result == "NEW_TOKEN"
        assert captured["url"].endswith("/RenewToken")
        assert captured["headers"]["dhanClientId"] == "CID1"
        assert captured["headers"]["access-token"] == "OLD_TOKEN"
        assert "client-id" not in captured["headers"]

    def test_401_on_an_already_expired_token_raises_dhan_auth_error(self, monkeypatch):
        monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(401, text="expired"))
        provider = DhanProvider(client_id="CID1", access_token="EXPIRED")

        with pytest.raises(DhanAuthError):
            provider.renew_access_token()

    def test_other_error_status_raises_generic_provider_error_not_auth_error(self, monkeypatch):
        monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(500, text="boom"))
        provider = DhanProvider(client_id="CID1", access_token="TOKEN1")

        with pytest.raises(ProviderError) as exc_info:
            provider.renew_access_token()
        assert not isinstance(exc_info.value, DhanAuthError)

    def test_response_missing_access_token_raises_provider_error(self, monkeypatch):
        monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(200, json_data={"expiryTime": "..."}))
        provider = DhanProvider(client_id="CID1", access_token="TOKEN1")

        with pytest.raises(ProviderError):
            provider.renew_access_token()


class TestGetTradeHistory:
    def test_single_page_stops_after_one_empty_page(self, monkeypatch):
        calls = []

        def fake_request(method, url, json=None, headers=None, timeout=None):
            calls.append(url)
            page = url.rsplit("/", 1)[-1]
            if page == "0":
                return _FakeResponse(200, json_data=[{"exchangeTradeId": "T1"}])
            return _FakeResponse(200, json_data=[])

        monkeypatch.setattr(httpx, "request", fake_request)
        provider = DhanProvider(client_id="CID1", access_token="TOKEN1")

        rows = provider.get_trade_history(date(2026, 8, 1), date(2026, 8, 25))

        assert rows == [{"exchangeTradeId": "T1"}]
        assert len(calls) == 2  # page 0 (one row), page 1 (empty, stops)
        assert calls[0].endswith("/trades/2026-08-01/2026-08-25/0")
        assert calls[1].endswith("/trades/2026-08-01/2026-08-25/1")

    def test_concatenates_multiple_non_empty_pages(self, monkeypatch):
        pages = {
            "0": [{"exchangeTradeId": "T1"}],
            "1": [{"exchangeTradeId": "T2"}],
            "2": [],
        }

        def fake_request(method, url, json=None, headers=None, timeout=None):
            page = url.rsplit("/", 1)[-1]
            return _FakeResponse(200, json_data=pages[page])

        monkeypatch.setattr(httpx, "request", fake_request)
        provider = DhanProvider(client_id="CID1", access_token="TOKEN1")

        rows = provider.get_trade_history(date(2026, 8, 1), date(2026, 8, 25))

        assert rows == [{"exchangeTradeId": "T1"}, {"exchangeTradeId": "T2"}]

    def test_no_trades_at_all_returns_empty_list(self, monkeypatch):
        monkeypatch.setattr(httpx, "request", lambda *a, **k: _FakeResponse(200, json_data=[]))
        provider = DhanProvider(client_id="CID1", access_token="TOKEN1")

        assert provider.get_trade_history(date(2026, 8, 1), date(2026, 8, 25)) == []

    def test_raises_instead_of_looping_forever_if_pagination_never_terminates(self, monkeypatch):
        # A safety net, not documented Dhan behavior -- guards against the
        # `page` segment being silently ignored server-side (every page
        # echoing the same non-empty batch forever).
        monkeypatch.setattr(httpx, "request", lambda *a, **k: _FakeResponse(200, json_data=[{"exchangeTradeId": "T1"}]))
        provider = DhanProvider(client_id="CID1", access_token="TOKEN1")

        with pytest.raises(ProviderError):
            provider.get_trade_history(date(2026, 8, 1), date(2026, 8, 25), max_pages=3)


class TestGetLtpBySecurityId:
    def test_flattens_nested_segment_response(self, monkeypatch):
        def fake_request(method, url, json=None, headers=None, timeout=None):
            assert method == "POST"
            assert json == {"NSE_FNO": [49081], "IDX_I": [13]}
            return _FakeResponse(
                200,
                json_data={
                    "data": {
                        "NSE_FNO": {"49081": {"last_price": 1.1}},
                        "IDX_I": {"13": {"last_price": 23000.5}},
                    },
                    "status": "success",
                },
            )

        monkeypatch.setattr(httpx, "request", fake_request)
        provider = DhanProvider(client_id="CID1", access_token="TOKEN1")

        result = provider.get_ltp_by_security_id({"NSE_FNO": ["49081"], "IDX_I": ["13"]})

        assert result == {"49081": 1.1, "13": 23000.5}

    def test_empty_input_short_circuits_without_a_request(self, monkeypatch):
        def fake_request(*args, **kwargs):
            raise AssertionError("should not make a request for an empty security id map")

        monkeypatch.setattr(httpx, "request", fake_request)
        provider = DhanProvider(client_id="CID1", access_token="TOKEN1")

        assert provider.get_ltp_by_security_id({}) == {}


# Shaped exactly like a real download (confirmed live against
# https://images.dhan.co/api-data/api-scrip-master.csv): SM_SYMBOL_NAME
# is blank for every stock F&O row (not modeled here at all -- the
# resolver never reads it), strike_price/option_type are real sentinels
# for a future ("-0.01"/"XX", not blank), and the underlying has to come
# from SEM_TRADING_SYMBOL. NAM-INDIA covers a hyphenated underlying.
_FO_MASTER_FIXTURE = pd.DataFrame(
    [
        {
            "SEM_SMST_SECURITY_ID": "50001",
            "SEM_EXM_EXCH_ID": "NSE",
            "SEM_INSTRUMENT_NAME": "FUTSTK",
            "SEM_TRADING_SYMBOL": "RELIANCE-Aug2026-FUT",
            "SEM_EXPIRY_DATE": "2026-08-27",
            "SEM_STRIKE_PRICE": -0.01,
            "SEM_OPTION_TYPE": "XX",
        },
        {
            "SEM_SMST_SECURITY_ID": "50002",
            "SEM_EXM_EXCH_ID": "NSE",
            "SEM_INSTRUMENT_NAME": "OPTSTK",
            "SEM_TRADING_SYMBOL": "RELIANCE-Aug2026-3000-CE",
            "SEM_EXPIRY_DATE": "2026-08-27",
            "SEM_STRIKE_PRICE": 3000.0,
            "SEM_OPTION_TYPE": "CE",
        },
        {
            "SEM_SMST_SECURITY_ID": "50003",
            "SEM_EXM_EXCH_ID": "NSE",
            "SEM_INSTRUMENT_NAME": "OPTSTK",
            "SEM_TRADING_SYMBOL": "RELIANCE-Aug2026-2900-PE",
            "SEM_EXPIRY_DATE": "2026-08-27",
            "SEM_STRIKE_PRICE": 2900.0,
            "SEM_OPTION_TYPE": "PE",
        },
        # Hyphenated underlying -- confirms right-anchored parsing, not a
        # naive split-on-first-hyphen.
        {
            "SEM_SMST_SECURITY_ID": "50004",
            "SEM_EXM_EXCH_ID": "NSE",
            "SEM_INSTRUMENT_NAME": "OPTSTK",
            "SEM_TRADING_SYMBOL": "NAM-INDIA-Aug2026-720-CE",
            "SEM_EXPIRY_DATE": "2026-08-27",
            "SEM_STRIKE_PRICE": 720.0,
            "SEM_OPTION_TYPE": "CE",
        },
        # Same underlying/expiry/strike on BSE -- must be excluded: stock
        # legs stay NSE-only even now that index legs are cross-exchange.
        {
            "SEM_SMST_SECURITY_ID": "99999",
            "SEM_EXM_EXCH_ID": "BSE",
            "SEM_INSTRUMENT_NAME": "OPTSTK",
            "SEM_TRADING_SYMBOL": "RELIANCE-Aug2026-2900-PE",
            "SEM_EXPIRY_DATE": "2026-08-27",
            "SEM_STRIKE_PRICE": 2900.0,
            "SEM_OPTION_TYPE": "PE",
        },
        # A plain equity row -- must be excluded (derivative instrument
        # types only).
        {
            "SEM_SMST_SECURITY_ID": "11111",
            "SEM_EXM_EXCH_ID": "NSE",
            "SEM_INSTRUMENT_NAME": "EQUITY",
            "SEM_TRADING_SYMBOL": "RELIANCE",
            "SEM_EXPIRY_DATE": None,
            "SEM_STRIKE_PRICE": None,
            "SEM_OPTION_TYPE": None,
        },
        # NIFTY index option on NSE -- must be included (confirmed live:
        # excluding FUTIDX/OPTIDX entirely silently dropped every index
        # CSP/CC leg and index position from live pricing).
        {
            "SEM_SMST_SECURITY_ID": "60001",
            "SEM_EXM_EXCH_ID": "NSE",
            "SEM_INSTRUMENT_NAME": "OPTIDX",
            "SEM_TRADING_SYMBOL": "NIFTY-Aug2026-25900-CE",
            "SEM_EXPIRY_DATE": "2026-08-25",
            "SEM_STRIKE_PRICE": 25900.0,
            "SEM_OPTION_TYPE": "CE",
        },
        # SENSEX index option on BSE -- must ALSO be included, unlike a
        # BSE stock leg: index legs are legitimately cross-exchange
        # (SENSEX/BANKEX only ever list on BSE), confirmed live no NSE
        # SENSEX row exists to prefer instead.
        {
            "SEM_SMST_SECURITY_ID": "70001",
            "SEM_EXM_EXCH_ID": "BSE",
            "SEM_INSTRUMENT_NAME": "OPTIDX",
            "SEM_TRADING_SYMBOL": "SENSEX-Aug2026-81000-CE",
            "SEM_EXPIRY_DATE": "2026-08-27",
            "SEM_STRIKE_PRICE": 81000.0,
            "SEM_OPTION_TYPE": "CE",
        },
    ]
)


@pytest.fixture(autouse=True)
def _clear_instrument_master_caches():
    dhan_provider._equity_master_cache.clear()
    dhan_provider._fo_master_cache.clear()
    dhan_provider._raw_master_cache.clear()
    yield
    dhan_provider._equity_master_cache.clear()
    dhan_provider._fo_master_cache.clear()
    dhan_provider._raw_master_cache.clear()


class TestLoadFoInstrumentMaster:
    def test_filters_to_nse_stock_legs_plus_nse_and_bse_index_legs(self, monkeypatch):
        monkeypatch.setattr(dhan_provider.pd, "read_csv", lambda *a, **k: _FO_MASTER_FIXTURE)

        master = dhan_provider._load_fo_instrument_master()

        # 50001-50004: NSE stock legs. 60001: NSE index leg. 70001: BSE
        # index leg. 99999 (BSE stock leg) and 11111 (equity) excluded.
        assert set(master["security_id"]) == {"50001", "50002", "50003", "50004", "60001", "70001"}

    def test_nse_index_leg_resolves_by_underlying(self, monkeypatch):
        monkeypatch.setattr(dhan_provider.pd, "read_csv", lambda *a, **k: _FO_MASTER_FIXTURE)

        master = dhan_provider._load_fo_instrument_master()

        assert master[master["security_id"] == "60001"].iloc[0]["underlying_symbol"] == "NIFTY"

    def test_bse_index_leg_is_not_excluded_by_the_nse_only_stock_rule(self, monkeypatch):
        # Regression: an earlier version of this function excluded
        # FUTIDX/OPTIDX entirely, silently dropping every index CSP/CC
        # leg and index position from live pricing -- confirmed live via
        # the "Not live-priced" diagnostic naming NIFTY/BANKNIFTY/
        # FINNIFTY/SENSEX contracts a user's account actually holds.
        monkeypatch.setattr(dhan_provider.pd, "read_csv", lambda *a, **k: _FO_MASTER_FIXTURE)

        master = dhan_provider._load_fo_instrument_master()

        assert master[master["security_id"] == "70001"].iloc[0]["underlying_symbol"] == "SENSEX"

    def test_exchange_column_reflects_each_rows_own_listing(self, monkeypatch):
        # Kept (not discarded after filtering) so get_fo_quotes can query
        # each resolved security_id under its real exchange segment --
        # see TestGetFoQuotes' BSE_FNO test for the live bug this fixes.
        monkeypatch.setattr(dhan_provider.pd, "read_csv", lambda *a, **k: _FO_MASTER_FIXTURE)

        master = dhan_provider._load_fo_instrument_master()

        assert master[master["security_id"] == "50002"].iloc[0]["exchange"] == "NSE"
        assert master[master["security_id"] == "70001"].iloc[0]["exchange"] == "BSE"

    def test_bse_stock_leg_still_excluded_despite_index_legs_now_allowed_cross_exchange(self, monkeypatch):
        monkeypatch.setattr(dhan_provider.pd, "read_csv", lambda *a, **k: _FO_MASTER_FIXTURE)

        master = dhan_provider._load_fo_instrument_master()

        assert "99999" not in set(master["security_id"])

    def test_normalizes_xx_option_type_to_fut(self, monkeypatch):
        # Dhan's real feed uses 'XX' (not blank) for a future's option
        # type -- confirmed live.
        monkeypatch.setattr(dhan_provider.pd, "read_csv", lambda *a, **k: _FO_MASTER_FIXTURE)

        master = dhan_provider._load_fo_instrument_master()

        future_row = master[master["security_id"] == "50001"].iloc[0]
        assert future_row["option_type"] == "FUT"

    def test_underlying_symbol_parsed_from_trading_symbol_not_the_blank_sm_symbol_name_column(self, monkeypatch):
        monkeypatch.setattr(dhan_provider.pd, "read_csv", lambda *a, **k: _FO_MASTER_FIXTURE)

        master = dhan_provider._load_fo_instrument_master()

        assert master[master["security_id"] == "50002"].iloc[0]["underlying_symbol"] == "RELIANCE"

    def test_underlying_symbol_with_internal_hyphen_is_not_truncated(self, monkeypatch):
        # "NAM-INDIA-Aug2026-720-CE" -- a naive split-on-first-hyphen
        # would wrongly resolve this to "NAM".
        monkeypatch.setattr(dhan_provider.pd, "read_csv", lambda *a, **k: _FO_MASTER_FIXTURE)

        master = dhan_provider._load_fo_instrument_master()

        assert master[master["security_id"] == "50004"].iloc[0]["underlying_symbol"] == "NAM-INDIA"

    def test_matching_zero_rows_raises_instead_of_returning_an_unusable_empty_master(self, monkeypatch):
        empty = pd.DataFrame(
            columns=[
                "SEM_SMST_SECURITY_ID", "SEM_EXM_EXCH_ID", "SEM_INSTRUMENT_NAME", "SEM_TRADING_SYMBOL",
                "SEM_EXPIRY_DATE", "SEM_STRIKE_PRICE", "SEM_OPTION_TYPE",
            ]
        )
        monkeypatch.setattr(dhan_provider.pd, "read_csv", lambda *a, **k: empty)

        with pytest.raises(ProviderError, match="zero"):
            dhan_provider._load_fo_instrument_master()


class TestLoadFoInstrumentMasterDbCache:
    """Migration 0035: the shared dhan_fo_instruments table -- same
    no-freshness-check, no-download-fallback behavior as
    TestLoadInstrumentMasterDbCache, for the F&O loader."""

    def test_db_entry_is_used_without_downloading_no_matter_how_old(self, monkeypatch):
        def fail_download(*a, **k):
            raise AssertionError("a client-backed call must never download")

        monkeypatch.setattr(dhan_provider.pd, "read_csv", fail_download)
        monkeypatch.setattr(
            dhan_provider.dhan_instrument_repo,
            "get_fo_instruments",
            lambda client: [
                {
                    "security_id": "50002", "underlying_symbol": "RELIANCE", "expiry_date": "2026-09-24",
                    "strike_price": 3000.0, "option_type": "CE", "exchange": "NSE",
                }
            ],
        )

        df = dhan_provider._load_fo_instrument_master(client=object())

        assert df["underlying_symbol"].tolist() == ["RELIANCE"]
        assert df["expiry_date"].iloc[0] == date(2026, 9, 24)
        assert df["exchange"].tolist() == ["NSE"]

    def test_empty_db_table_raises_pointing_at_the_refresh_button_instead_of_downloading(self, monkeypatch):
        def fail_download(*a, **k):
            raise AssertionError("a client-backed call must never download")

        monkeypatch.setattr(dhan_provider.pd, "read_csv", fail_download)
        monkeypatch.setattr(dhan_provider.dhan_instrument_repo, "get_fo_instruments", lambda client: [])

        with pytest.raises(ProviderError, match="Refresh Instrument Master"):
            dhan_provider._load_fo_instrument_master(client=object())

    def test_client_none_still_downloads_for_script_or_cron_usage(self, monkeypatch):
        monkeypatch.setattr(dhan_provider.pd, "read_csv", lambda *a, **k: _FO_MASTER_FIXTURE)

        df = dhan_provider._load_fo_instrument_master(client=None)

        assert not df.empty


class TestRefreshDhanInstrumentMaster:
    """Settings' "Refresh Instrument Master - Dhan" button -- the only
    thing left that downloads Dhan's instrument-master CSV and persists
    both slices, now that _load_instrument_master/_load_fo_instrument_master
    never do so implicitly on a cache miss."""

    def test_downloads_once_persists_both_slices_and_updates_in_memory_caches(self, monkeypatch):
        # One clean equity row plus the full F&O fixture -- concatenated
        # rather than reusing _FO_MASTER_FIXTURE alone for both slices,
        # since none of its rows are SEM_INSTRUMENT_NAME == "EQUITY" (it
        # would raise "zero NSE equity rows" if fed to
        # _download_equity_master on its own).
        combined_raw = pd.concat(
            [
                pd.DataFrame(
                    [
                        {
                            "SEM_SMST_SECURITY_ID": "1001", "SEM_TRADING_SYMBOL": "RELIANCE",
                            "SEM_EXM_EXCH_ID": "NSE", "SEM_SEGMENT": "E", "SEM_INSTRUMENT_NAME": "EQUITY",
                        }
                    ]
                ),
                _FO_MASTER_FIXTURE,
            ],
            ignore_index=True,
        )
        download_count = {"n": 0}

        def counting_read_csv(*a, **k):
            download_count["n"] += 1
            return combined_raw

        monkeypatch.setattr(dhan_provider.pd, "read_csv", counting_read_csv)
        written = {}
        logged = []
        monkeypatch.setattr(
            dhan_provider.dhan_instrument_repo,
            "replace_equity_instruments",
            lambda client, rows: written.setdefault("equity_rows", rows),
        )
        monkeypatch.setattr(
            dhan_provider.dhan_instrument_repo,
            "replace_fo_instruments",
            lambda client, rows: written.setdefault("fo_rows", rows),
        )
        monkeypatch.setattr(dhan_provider.fetch_log_repo, "log_fetch", lambda client, entry: logged.append(entry))

        summary = dhan_provider.refresh_dhan_instrument_master(client=object())

        # One download shared between both slices (_get_raw_instrument_master),
        # not one per slice -- same sharing get_quotes/get_fo_quotes already
        # relied on for a cache-cold "Stock & Option Data Refresh".
        assert download_count["n"] == 1
        assert written["equity_rows"] == [{"security_id": "1001", "trading_symbol": "RELIANCE"}]
        assert {r["security_id"] for r in written["fo_rows"]} == {"50001", "50002", "50003", "50004", "60001", "70001"}
        assert {entry.provider_name for entry in logged} == {"equity", "fo"}
        assert summary == {"equity_count": 1, "fo_count": 6}

        # The in-memory caches must be updated immediately too -- a
        # resolve call in this same process right after the button click
        # must see the fresh data, not whatever was cached before.
        equity_df = dhan_provider._load_instrument_master(client=object())
        fo_df = dhan_provider._load_fo_instrument_master(client=object())
        assert equity_df["trading_symbol"].tolist() == ["RELIANCE"]
        assert set(fo_df["security_id"]) == {"50001", "50002", "50003", "50004", "60001", "70001"}


class TestResolveFoSecurityId:
    def test_resolves_future(self, monkeypatch):
        monkeypatch.setattr(dhan_provider.pd, "read_csv", lambda *a, **k: _FO_MASTER_FIXTURE)

        assert resolve_fo_security_id("RELIANCE", date(2026, 8, 27), 0.0, "FUT") == "50001"

    def test_resolves_option_by_exact_strike(self, monkeypatch):
        monkeypatch.setattr(dhan_provider.pd, "read_csv", lambda *a, **k: _FO_MASTER_FIXTURE)

        assert resolve_fo_security_id("RELIANCE", date(2026, 8, 27), 3000.0, "CE") == "50002"
        assert resolve_fo_security_id("RELIANCE", date(2026, 8, 27), 2900.0, "PE") == "50003"

    def test_no_match_raises_provider_error(self, monkeypatch):
        monkeypatch.setattr(dhan_provider.pd, "read_csv", lambda *a, **k: _FO_MASTER_FIXTURE)

        with pytest.raises(ProviderError):
            resolve_fo_security_id("RELIANCE", date(2026, 8, 27), 9999.0, "CE")

    def test_wrong_exchange_row_never_matches(self, monkeypatch):
        # The BSE-sourced fixture row shares underlying/expiry/strike with
        # the real NSE PE row (50003) -- confirms NSE-only filtering, not
        # just that *a* match exists.
        monkeypatch.setattr(dhan_provider.pd, "read_csv", lambda *a, **k: _FO_MASTER_FIXTURE)

        assert resolve_fo_security_id("RELIANCE", date(2026, 8, 27), 2900.0, "PE") == "50003"

    def test_resolves_nse_index_option(self, monkeypatch):
        monkeypatch.setattr(dhan_provider.pd, "read_csv", lambda *a, **k: _FO_MASTER_FIXTURE)

        assert resolve_fo_security_id("NIFTY", date(2026, 8, 25), 25900.0, "CE") == "60001"

    def test_resolves_bse_index_option(self, monkeypatch):
        monkeypatch.setattr(dhan_provider.pd, "read_csv", lambda *a, **k: _FO_MASTER_FIXTURE)

        assert resolve_fo_security_id("SENSEX", date(2026, 8, 27), 81000.0, "CE") == "70001"


class TestGetFoQuotes:
    def test_batches_resolved_contracts_into_one_request(self, monkeypatch):
        monkeypatch.setattr(dhan_provider.pd, "read_csv", lambda *a, **k: _FO_MASTER_FIXTURE)
        captured = {}

        def fake_request(method, url, json=None, headers=None, timeout=None):
            captured["json"] = json
            return _FakeResponse(
                200,
                json_data={
                    "data": {
                        "NSE_FNO": {
                            "50001": {"last_price": 2950.0},
                            "50002": {"last_price": 45.5},
                            "50003": {"last_price": 32.0},
                        }
                    }
                },
            )

        monkeypatch.setattr(httpx, "request", fake_request)
        provider = DhanProvider(client_id="CID1", access_token="TOKEN1")
        contracts = [
            ("RELIANCE", date(2026, 8, 27), 0.0, "FUT"),
            ("RELIANCE", date(2026, 8, 27), 3000.0, "CE"),
            ("RELIANCE", date(2026, 8, 27), 2900.0, "PE"),
        ]

        result = provider.get_fo_quotes(contracts)

        assert set(captured["json"]["NSE_FNO"]) == {50001, 50002, 50003}
        assert result == {
            ("RELIANCE", date(2026, 8, 27), 0.0, "FUT"): 2950.0,
            ("RELIANCE", date(2026, 8, 27), 3000.0, "CE"): 45.5,
            ("RELIANCE", date(2026, 8, 27), 2900.0, "PE"): 32.0,
        }

    def test_bse_listed_contract_is_queried_under_bse_fno_not_nse_fno(self, monkeypatch):
        # The real live bug: every resolved contract used to be queried
        # under one hardcoded "NSE_FNO" segment, regardless of which
        # exchange it actually resolved on. SENSEX (BSE_FNO) resolves a
        # real security_id fine (see TestResolveFoSecurityId) but Dhan's
        # LTP endpoint silently returns nothing for a BSE-listed id
        # queried under NSE_FNO -- confirmed live as the exact "Not
        # live-priced" symptom for SENSEX/BANKEX contracts only.
        monkeypatch.setattr(dhan_provider.pd, "read_csv", lambda *a, **k: _FO_MASTER_FIXTURE)
        captured = {}

        def fake_request(method, url, json=None, headers=None, timeout=None):
            captured["json"] = json
            return _FakeResponse(
                200,
                json_data={
                    "data": {
                        "NSE_FNO": {"50002": {"last_price": 45.5}},
                        "BSE_FNO": {"70001": {"last_price": 350.0}},
                    }
                },
            )

        monkeypatch.setattr(httpx, "request", fake_request)
        provider = DhanProvider(client_id="CID1", access_token="TOKEN1")
        contracts = [
            ("RELIANCE", date(2026, 8, 27), 3000.0, "CE"),
            ("SENSEX", date(2026, 8, 27), 81000.0, "CE"),
        ]

        result = provider.get_fo_quotes(contracts)

        assert captured["json"] == {"NSE_FNO": [50002], "BSE_FNO": [70001]}
        assert result == {
            ("RELIANCE", date(2026, 8, 27), 3000.0, "CE"): 45.5,
            ("SENSEX", date(2026, 8, 27), 81000.0, "CE"): 350.0,
        }

    def test_unresolvable_contract_is_skipped_not_fatal(self, monkeypatch):
        monkeypatch.setattr(dhan_provider.pd, "read_csv", lambda *a, **k: _FO_MASTER_FIXTURE)

        def fake_request(method, url, json=None, headers=None, timeout=None):
            return _FakeResponse(200, json_data={"data": {"NSE_FNO": {"50002": {"last_price": 45.5}}}})

        monkeypatch.setattr(httpx, "request", fake_request)
        provider = DhanProvider(client_id="CID1", access_token="TOKEN1")
        contracts = [
            ("RELIANCE", date(2026, 8, 27), 3000.0, "CE"),
            ("RELIANCE", date(2026, 8, 27), 9999.0, "CE"),  # no such strike -- unresolvable
        ]

        result = provider.get_fo_quotes(contracts)

        assert result == {("RELIANCE", date(2026, 8, 27), 3000.0, "CE"): 45.5}

    def test_empty_input_short_circuits_without_a_request(self, monkeypatch):
        def fake_request(*args, **kwargs):
            raise AssertionError("should not make a request for an empty contract list")

        monkeypatch.setattr(httpx, "request", fake_request)
        provider = DhanProvider(client_id="CID1", access_token="TOKEN1")

        assert provider.get_fo_quotes([]) == {}

    def test_systemic_instrument_master_failure_propagates_not_swallowed_per_contract(self, monkeypatch):
        # Same fix as get_quotes' equivalent test -- the F&O master is
        # now loaded once, up front, outside the per-contract try/except,
        # so a systemic failure (e.g. the zero-rows case) is a real
        # exception instead of silently skipping every contract.
        empty = pd.DataFrame(
            columns=[
                "SEM_SMST_SECURITY_ID", "SEM_EXM_EXCH_ID", "SEM_INSTRUMENT_NAME", "SEM_TRADING_SYMBOL",
                "SEM_EXPIRY_DATE", "SEM_STRIKE_PRICE", "SEM_OPTION_TYPE",
            ]
        )
        monkeypatch.setattr(dhan_provider.pd, "read_csv", lambda *a, **k: empty)

        def fake_request(*args, **kwargs):
            raise AssertionError("should never reach the network call")

        monkeypatch.setattr(httpx, "request", fake_request)
        provider = DhanProvider(client_id="CID1", access_token="TOKEN1")

        with pytest.raises(ProviderError, match="zero"):
            provider.get_fo_quotes([("RELIANCE", date(2026, 8, 27), 3000.0, "CE")])
