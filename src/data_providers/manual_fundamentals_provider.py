"""Stopgap fundamentals provider: reads PE / PEG / dividend data curated by
hand into CSV files, since Dhan (and no other licensed vendor named in
scope) exposes this data via API. See README "Limitations" -- this is the
clearest gap in v1 and the main place a real vendor integration should be
added by implementing FundamentalsDataProvider against one.

CSV schema:
    data/manual_fundamentals.csv: symbol,as_of_date,pe_ratio,peg_ratio,eps,market_cap
    data/manual_dividends.csv:    symbol,ex_date,amount_per_share,dividend_type

Rows with blank pe_ratio/peg_ratio are treated as missing (None), not zero.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from src.data_providers.base import FundamentalsDataProvider
from src.models.enums import DividendType
from src.models.market_data import DividendEvent, FundamentalSnapshot

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data"

# If the most recent manually-curated row for a symbol is older than this,
# it's flagged stale -- manual data realistically won't be refreshed daily.
STALE_AFTER_DAYS = 120


class ManualFundamentalsProvider(FundamentalsDataProvider):
    name = "manual"

    def __init__(self, data_dir: Path | str = DEFAULT_DATA_DIR):
        self._data_dir = Path(data_dir)
        self._fundamentals_df: pd.DataFrame | None = None
        self._dividends_df: pd.DataFrame | None = None

    def _load_fundamentals(self) -> pd.DataFrame:
        if self._fundamentals_df is None:
            path = self._data_dir / "manual_fundamentals.csv"
            if path.exists():
                df = pd.read_csv(path, parse_dates=["as_of_date"])
            else:
                df = pd.DataFrame(columns=["symbol", "as_of_date", "pe_ratio", "peg_ratio", "eps", "market_cap"])
            self._fundamentals_df = df
        return self._fundamentals_df

    def _load_dividends(self) -> pd.DataFrame:
        if self._dividends_df is None:
            path = self._data_dir / "manual_dividends.csv"
            if path.exists():
                df = pd.read_csv(path, parse_dates=["ex_date"])
            else:
                df = pd.DataFrame(columns=["symbol", "ex_date", "amount_per_share", "dividend_type"])
            self._dividends_df = df
        return self._dividends_df

    def get_fundamentals(self, symbol: str, as_of: date) -> FundamentalSnapshot | None:
        df = self._load_fundamentals()
        rows = df[(df["symbol"] == symbol) & (df["as_of_date"].dt.date <= as_of)]
        if rows.empty:
            return None
        row = rows.sort_values("as_of_date").iloc[-1]
        row_date = row["as_of_date"].date()
        is_stale = (as_of - row_date) > timedelta(days=STALE_AFTER_DAYS)

        def clean(value):
            return None if pd.isna(value) else float(value)

        return FundamentalSnapshot(
            symbol=symbol,
            as_of_date=row_date,
            pe_ratio=clean(row.get("pe_ratio")),
            peg_ratio=clean(row.get("peg_ratio")),
            eps=clean(row.get("eps")),
            market_cap=clean(row.get("market_cap")),
            source=self.name,
            is_stale=is_stale,
        )

    def get_dividend_history(self, symbol: str, from_date: date, to_date: date) -> list[DividendEvent]:
        df = self._load_dividends()
        rows = df[
            (df["symbol"] == symbol)
            & (df["ex_date"].dt.date >= from_date)
            & (df["ex_date"].dt.date <= to_date)
        ]
        events = []
        for _, row in rows.iterrows():
            dtype = row.get("dividend_type", "final")
            try:
                dividend_type = DividendType(str(dtype).lower())
            except ValueError:
                dividend_type = DividendType.FINAL
            events.append(
                DividendEvent(
                    symbol=symbol,
                    ex_date=row["ex_date"].date(),
                    amount_per_share=float(row["amount_per_share"]),
                    dividend_type=dividend_type,
                    source=self.name,
                )
            )
        return events
