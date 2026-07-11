#!/usr/bin/env python
"""Imports a screener.in "Export screen results" CSV into Supabase as
fundamentals data, since screener.in has no public API and we don't scrape
it -- see README "Limitations".

Workflow:
    1. On screener.in, build a screen/watchlist containing the Nifty 50
       symbols. Use "edit columns" to include at least a symbol column
       (NSE Code) and PE; add Dividend Yield, PEG, Market Cap, EPS if you
       have them.
    2. Click "Export" to download the CSV.
    3. python scripts/import_screener_csv.py path/to/export.csv

Column names are matched fuzzily (case/spacing-insensitive) since
screener.in exports whatever columns you chose to include, in whatever
order. Missing columns are simply skipped (left as missing data, not
zero) -- rerun after adding a column to backfill it.

Dividend yield handling: screener.in's export gives a *dividend yield
percentage*, not individual ex-dividend dates, but our TTM-yield
calculation sums individual `dividend_events`. This script approximates
by writing ONE synthetic dividend_events row per symbol, dated today,
sized so `ttm_dividend_yield()` reproduces the imported percentage against
today's stored price. It's tagged `source="screener_in_estimated"` so it's
never confused with real per-event dividend history. Re-running this
script replaces last time's estimate (same ex_date -> upsert), so run it
roughly as often as you re-export from screener.in.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.models.enums import DividendType, FetchStatus, FetchType  # noqa: E402
from src.models.fetch_log import ProviderFetchLog  # noqa: E402
from src.models.market_data import DividendEvent, FundamentalSnapshot  # noqa: E402
from src.repositories import companies_repo, dividends_repo, fetch_log_repo, fundamentals_repo, price_repo  # noqa: E402
from src.repositories.supabase_client import get_service_client  # noqa: E402
from src.services.market_calendar import IST  # noqa: E402
from src.utils.logging import get_logger  # noqa: E402

logger = get_logger(__name__)

SOURCE = "screener_in"


def _find_column(columns: list[str], *must_contain: str, exclude: tuple[str, ...] = ()) -> str | None:
    for col in columns:
        norm = "".join(ch for ch in col.upper() if ch.isalnum() or ch == " ")
        if all(token in norm for token in must_contain) and not any(token in norm for token in exclude):
            return col
    return None


def _to_float(value) -> float | None:
    if pd.isna(value):
        return None
    if isinstance(value, str):
        value = value.replace(",", "").replace("%", "").strip()
        if value in ("", "-", "NA", "N/A"):
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv_path", type=Path, help="Path to the screener.in-exported CSV")
    parser.add_argument(
        "--skip-dividend-estimate",
        action="store_true",
        help="Only import PE/PEG/EPS/market cap; skip the synthetic dividend-yield event",
    )
    args = parser.parse_args()

    if not args.csv_path.exists():
        raise SystemExit(f"File not found: {args.csv_path}")

    df = pd.read_csv(args.csv_path)
    columns = list(df.columns)

    symbol_col = _find_column(columns, "NSE") or _find_column(columns, "SYMBOL")
    pe_col = _find_column(columns, "PE", exclude=("PEG", "SPECIAL", "TYPE"))
    peg_col = _find_column(columns, "PEG")
    div_yield_col = _find_column(columns, "DIV", "YIELD") or _find_column(columns, "DIV", "YLD")
    market_cap_col = _find_column(columns, "MAR", "CAP") or _find_column(columns, "MARKET", "CAP")
    eps_col = _find_column(columns, "EPS")

    if symbol_col is None:
        raise SystemExit(
            f"Could not find an NSE-symbol column among: {columns}. "
            "Make sure your screener.in export includes 'NSE Code' (or a column with 'symbol' in the name)."
        )
    market_cap_in_crores = market_cap_col is not None and "CR" in market_cap_col.upper()

    logger.info(
        "matched columns -- symbol: %s, PE: %s, PEG: %s, dividend yield: %s, market cap: %s (crores=%s), EPS: %s",
        symbol_col, pe_col, peg_col, div_yield_col, market_cap_col, market_cap_in_crores, eps_col,
    )

    client = get_service_client()
    known_symbols = {c.symbol for c in companies_repo.list_current_constituents(client)}
    today = date.today()

    imported, skipped_unmatched, skipped_no_price = 0, [], []

    for _, row in df.iterrows():
        raw_symbol = str(row[symbol_col]).strip().upper()
        if raw_symbol not in known_symbols:
            skipped_unmatched.append(raw_symbol)
            continue

        pe = _to_float(row[pe_col]) if pe_col else None
        peg = _to_float(row[peg_col]) if peg_col else None
        eps = _to_float(row[eps_col]) if eps_col else None
        market_cap = _to_float(row[market_cap_col]) if market_cap_col else None
        if market_cap is not None and market_cap_in_crores:
            market_cap *= 1e7  # crore -> rupees, matching our schema's convention

        fundamentals_repo.upsert_fundamental_snapshot(
            client,
            FundamentalSnapshot(
                symbol=raw_symbol, as_of_date=today, pe_ratio=pe, peg_ratio=peg,
                eps=eps, market_cap=market_cap, source=SOURCE, is_stale=False,
            ),
        )
        imported += 1

        if not args.skip_dividend_estimate and div_yield_col:
            yield_pct = _to_float(row[div_yield_col])
            if yield_pct is not None:
                latest = price_repo.get_latest_close(client, raw_symbol)
                if latest and latest.effective_close:
                    amount = round(yield_pct / 100 * latest.effective_close, 4)
                    dividends_repo.upsert_dividend_events(
                        client,
                        [
                            DividendEvent(
                                symbol=raw_symbol, ex_date=today, amount_per_share=amount,
                                dividend_type=DividendType.FINAL, source=f"{SOURCE}_estimated",
                            )
                        ],
                    )
                else:
                    skipped_no_price.append(raw_symbol)

    fetch_log_repo.log_fetch(
        client,
        ProviderFetchLog(
            provider_name=SOURCE, fetch_type=FetchType.FUNDAMENTALS, symbol=None,
            status=FetchStatus.SUCCESS, retry_count=0,
            started_at=datetime.now(IST), finished_at=datetime.now(IST),
        ),
    )

    logger.info("imported fundamentals for %d symbols", imported)
    if skipped_unmatched:
        logger.warning("skipped %d unmatched symbol(s) not in nifty50_constituents: %s", len(skipped_unmatched), skipped_unmatched)
    if skipped_no_price:
        logger.warning("skipped dividend estimate for %d symbol(s) with no stored price yet: %s", len(skipped_no_price), skipped_no_price)


if __name__ == "__main__":
    main()
