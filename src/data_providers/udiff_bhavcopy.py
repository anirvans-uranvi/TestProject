"""Shared UDiFF bhavcopy parsing -- the SEBI-mandated Unified Distilled
File Format both NSE and BSE publish their daily F&O bhavcopy in: same
column schema, same FinInstrmTp codes (STF/STO/IDF/IDO), confirmed live
for both exchanges (see src/data_providers/nse_fo_provider.py and
bse_fo_provider.py's file headers). Those two modules differ only in
where the file lives and how it's fetched (NSE serves a zip, BSE a plain
CSV) -- this module owns the one parsing routine both call, parameterized
by which instrument-type codes and `source` tag each exchange wants, so a
schema fix only ever needs to happen in one place.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date

from src.models.enums import OptionType
from src.models.fo import (
    FuturesContract,
    FuturesDailyPrice,
    OptionContract,
    OptionDailyPrice,
)


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


def parse_udiff_bhavcopy(
    csv_text: str,
    *,
    source_name: str,
    futures_types: set[str],
    option_types: set[str],
    trade_date: date | None = None,
    universe: set[str] | None = None,
) -> FOBhavcopy:
    """Parse one exchange's UDiFF bhavcopy CSV text into the four F&O table
    shapes. `futures_types`/`option_types` are that exchange's own
    FinInstrmTp allow-list -- NSE uses `{"STF"}` / `{"STO", "IDO"}`; BSE
    uses `set()` / `{"IDO"}` (BSE's stock-level futures/options are
    excluded entirely -- see bse_fo_provider.py's file header for why, and
    each provider module for why `IDF` stays out of scope on both). If
    `universe` is given, keeps only those underlying symbols. `trade_date`
    defaults to each row's own TradDt. `source_name` is stamped onto every
    price row so ingested data stays traceable to which exchange/path
    produced it."""
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)

    resolved_date = trade_date
    if resolved_date is None and rows:
        resolved_date = _d(rows[0].get("TradDt")) or date.today()

    result = FOBhavcopy(trade_date=resolved_date or date.today())

    for row in rows:
        instr = (row.get("FinInstrmTp") or "").strip()
        if instr not in futures_types and instr not in option_types:
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
            source=source_name,
        )
        lot_size = _i(row.get("NewBrdLotQty"))
        contract_name = (row.get("FinInstrmNm") or "").strip() or None
        nse_token = (row.get("FinInstrmId") or "").strip() or None

        if instr in futures_types:
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
