"""NSE F&O data provider: the daily UDiFF bhavcopy.

Yahoo/yfinance carries no NSE derivatives, and NSE's live option-chain API
serves hollow JSON to scripts, so the reliable free source is the
end-of-day F&O bhavcopy -- one zip per trading day, downloadable with just
a browser User-Agent (no cookie handshake) from the `nsearchives` host:

    https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_YYYYMMDD_F_0000.csv.zip

Each row is one contract's full trading-day summary: OHLC, LTP, previous
close, settlement, underlying (spot) price, open interest + change, volume,
turnover, number of trades, expiry, strike, option type, and lot size.
Instrument types: STF = stock future, STO = stock option (IDF/IDO are index
derivatives -- out of scope).

The HTTP download and the CSV parse are deliberately separated so the parse
is unit-testable against an inline fixture with no network.
"""
from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass, field
from datetime import date, timedelta

import requests

from src.models.enums import OptionType
from src.models.fo import (
    FuturesContract,
    FuturesDailyPrice,
    OptionContract,
    OptionDailyPrice,
)

SOURCE_NAME = "nse_fo_bhavcopy"

BHAVCOPY_URL_TEMPLATE = (
    "https://nsearchives.nseindia.com/content/fo/"
    "BhavCopy_NSE_FO_0_0_0_{yyyymmdd}_F_0000.csv.zip"
)

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

# Bhavcopy FinInstrmTp codes we care about (stock derivatives only).
_FUTURES_TYPES = {"STF"}
_OPTION_TYPES = {"STO"}


@dataclass
class FOBhavcopy:
    """Parsed bhavcopy for one trading day, split into the four table shapes.

    Each contract appears once per day, so contracts need no de-duplication.
    Contracts carry provisional `is_open=True` / `first_seen`/`last_seen` set
    to this trade date; the ingestion run finalizes `is_open` against the
    real current date via `fo_repo.refresh_open_flags`.
    """

    trade_date: date
    futures_contracts: list[FuturesContract] = field(default_factory=list)
    futures_prices: list[FuturesDailyPrice] = field(default_factory=list)
    option_contracts: list[OptionContract] = field(default_factory=list)
    option_prices: list[OptionDailyPrice] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.futures_prices or self.option_prices)


def bhavcopy_url(trade_date: date) -> str:
    return BHAVCOPY_URL_TEMPLATE.format(yyyymmdd=trade_date.strftime("%Y%m%d"))


def _f(value: str | None) -> float | None:
    if value is None:
        return None
    value = value.strip()
    if value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _i(value: str | None) -> int | None:
    f = _f(value)
    return int(round(f)) if f is not None else None


def _d(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value.strip()[:10])


def parse_fo_bhavcopy(
    csv_text: str,
    trade_date: date | None = None,
    universe: set[str] | None = None,
) -> FOBhavcopy:
    """Parse bhavcopy CSV text into the four F&O table shapes.

    Keeps only stock futures (STF) and stock options (STO); ignores index
    derivatives (IDF/IDO). If `universe` is given, keeps only those
    underlying symbols. `trade_date` defaults to each row's own TradDt.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)

    resolved_date = trade_date
    if resolved_date is None and rows:
        resolved_date = _d(rows[0].get("TradDt")) or date.today()

    result = FOBhavcopy(trade_date=resolved_date or date.today())

    for row in rows:
        instr = (row.get("FinInstrmTp") or "").strip()
        if instr not in _FUTURES_TYPES and instr not in _OPTION_TYPES:
            continue

        symbol = (row.get("TckrSymb") or "").strip()
        if not symbol or (universe is not None and symbol not in universe):
            continue

        row_trade_date = _d(row.get("TradDt")) or result.trade_date
        expiry = _d(row.get("XpryDt"))
        if expiry is None:
            continue

        common_price = dict(
            symbol=symbol,
            expiry_date=expiry,
            trade_date=row_trade_date,
            open=_f(row.get("OpnPric")),
            high=_f(row.get("HghPric")),
            low=_f(row.get("LwPric")),
            close=_f(row.get("ClsPric")),
            last_price=_f(row.get("LastPric")),
            prev_close=_f(row.get("PrvsClsgPric")),
            settlement_price=_f(row.get("SttlmPric")),
            underlying_price=_f(row.get("UndrlygPric")),
            open_interest=_i(row.get("OpnIntrst")),
            change_in_oi=_i(row.get("ChngInOpnIntrst")),
            volume=_i(row.get("TtlTradgVol")),
            turnover=_f(row.get("TtlTrfVal")),
            num_trades=_i(row.get("TtlNbOfTxsExctd")),
            source=SOURCE_NAME,
        )
        lot_size = _i(row.get("NewBrdLotQty"))
        contract_name = (row.get("FinInstrmNm") or "").strip() or None
        nse_token = (row.get("FinInstrmId") or "").strip() or None

        if instr in _FUTURES_TYPES:
            result.futures_contracts.append(
                FuturesContract(
                    symbol=symbol,
                    expiry_date=expiry,
                    contract_name=contract_name,
                    nse_token=nse_token,
                    lot_size=lot_size,
                    is_open=True,
                    first_seen_date=row_trade_date,
                    last_seen_date=row_trade_date,
                )
            )
            result.futures_prices.append(FuturesDailyPrice(**common_price))
        else:  # option
            strike = _f(row.get("StrkPric"))
            optn = (row.get("OptnTp") or "").strip().upper()
            if strike is None or optn not in ("CE", "PE"):
                continue
            option_type = OptionType(optn)
            result.option_contracts.append(
                OptionContract(
                    symbol=symbol,
                    expiry_date=expiry,
                    strike_price=strike,
                    option_type=option_type,
                    contract_name=contract_name,
                    nse_token=nse_token,
                    lot_size=lot_size,
                    is_open=True,
                    first_seen_date=row_trade_date,
                    last_seen_date=row_trade_date,
                )
            )
            result.option_prices.append(
                OptionDailyPrice(strike_price=strike, option_type=option_type, **common_price)
            )

    return result


def download_bhavcopy_csv(
    trade_date: date, session: requests.Session | None = None, timeout: int = 30
) -> str | None:
    """Download and unzip one day's F&O bhavcopy, returning its CSV text.

    Returns None on a 404 (weekend / holiday / not-yet-published), so callers
    can walk backwards to the previous trading day.
    """
    sess = session or requests.Session()
    resp = sess.get(bhavcopy_url(trade_date), headers=_BROWSER_HEADERS, timeout=timeout)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    # A real bhavcopy is a zip; NSE occasionally serves a small HTML/PDF error
    # body with a 200 -- guard against that.
    if len(resp.content) < 1000:
        return None
    try:
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
    except zipfile.BadZipFile:
        return None
    return zf.read(zf.namelist()[0]).decode("utf-8", errors="replace")


def fetch_fo_bhavcopy(
    trade_date: date,
    universe: set[str] | None = None,
    session: requests.Session | None = None,
) -> FOBhavcopy | None:
    """Download + parse one day's bhavcopy. None if that day has no file."""
    csv_text = download_bhavcopy_csv(trade_date, session=session)
    if csv_text is None:
        return None
    return parse_fo_bhavcopy(csv_text, trade_date=trade_date, universe=universe)


def latest_available_bhavcopy(
    universe: set[str] | None = None,
    on_or_before: date | None = None,
    max_lookback: int = 7,
    session: requests.Session | None = None,
) -> FOBhavcopy | None:
    """Walk back from `on_or_before` (default today) up to `max_lookback` days
    to the most recent published F&O bhavcopy, skipping weekends/holidays."""
    sess = session or requests.Session()
    d = on_or_before or date.today()
    for _ in range(max_lookback):
        parsed = fetch_fo_bhavcopy(d, universe=universe, session=sess)
        if parsed is not None and not parsed.is_empty:
            return parsed
        d -= timedelta(days=1)
    return None
