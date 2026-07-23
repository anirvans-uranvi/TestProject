"""Broker CSV parsing, symbol matching, and valuation for the Portfolio
page (pages/6_Portfolio.py). Holdings are plain dicts throughout (not a
dataclass) -- same convention as fo_service's row-dict outputs -- with
keys: raw_name, symbol (str | None), qty, avg_price, investment.
"""
from __future__ import annotations

import re

import pandas as pd

from src.models.company import Company
from src.models.portfolio import PortfolioHolding


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


def parse_zerodha_csv(file) -> list[dict]:
    """Zerodha's holdings export -- `Instrument` is already the exact
    NSE trading symbol, so it's trusted directly with no name matching.
    The file's own LTP/Cur. val/P&L columns are ignored; those are
    always recomputed live against the app's own market data."""
    df = pd.read_csv(file)
    holdings = []
    for _, row in df.iterrows():
        instrument = row.get("Instrument")
        if pd.isna(instrument) or not str(instrument).strip():
            continue
        qty = _to_float(row.get("Qty."))
        avg_price = _to_float(row.get("Avg. cost"))
        investment = _to_float(row.get("Invested"))
        if qty is None or avg_price is None or investment is None:
            continue
        holdings.append(
            {
                "raw_name": str(instrument).strip(),
                "symbol": str(instrument).strip().upper(),
                "qty": qty,
                "avg_price": avg_price,
                "investment": investment,
            }
        )
    return holdings


def _normalize_name(name: str) -> str:
    normalized = re.sub(r"[^A-Z0-9]", "", name.upper())
    for suffix in ("LIMITED", "LTD"):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
            break
    return normalized


def match_symbol(raw_name: str, companies: list[Company]) -> str | None:
    """Matches a broker's free-text instrument name against known
    companies by normalized-name containment. Returns the symbol only on
    exactly one match -- zero or ambiguous matches are left unresolved
    rather than guessed."""
    normalized_raw = _normalize_name(raw_name)
    if not normalized_raw:
        return None
    matches = set()
    for company in companies:
        normalized_company = _normalize_name(company.name)
        if not normalized_company:
            continue
        if normalized_raw == normalized_company or normalized_raw in normalized_company or normalized_company in normalized_raw:
            matches.add(company.symbol)
    if len(matches) == 1:
        return next(iter(matches))
    return None


def parse_dhan_csv(file, companies: list[Company]) -> list[dict]:
    """Dhan's holdings export -- `Name` is a human company name, not an
    NSE symbol, and numbers are quoted with Indian-style grouping (e.g.
    "6,42,438.40"). Symbol is resolved via match_symbol(); unresolved
    rows keep symbol=None rather than a guess."""
    df = pd.read_csv(file)
    holdings = []
    for _, row in df.iterrows():
        name = row.get("Name")
        if pd.isna(name) or not str(name).strip():
            continue
        raw_name = str(name).strip()
        qty = _to_float(row.get("Quantity"))
        avg_price = _to_float(row.get("Avg Price"))
        investment = _to_float(row.get("Investment"))
        if qty is None or avg_price is None or investment is None:
            continue
        holdings.append(
            {
                "raw_name": raw_name,
                "symbol": match_symbol(raw_name, companies),
                "qty": qty,
                "avg_price": avg_price,
                "investment": investment,
            }
        )
    return holdings


def merge_holdings(rows: list[dict]) -> list[dict]:
    """Combines rows across brokers into one row per stock. Grouped by
    symbol when resolved, else by raw_name (two differently-worded
    unresolved names can't be safely merged). Qty/investment are summed;
    avg_price is recomputed as investment / qty so it stays a true
    weighted average."""
    groups: dict[str, dict] = {}
    order: list[str] = []
    for row in rows:
        key = row["symbol"] or f"__unresolved__{row['raw_name']}"
        if key not in groups:
            groups[key] = {
                "raw_name": row["raw_name"],
                "symbol": row["symbol"],
                "qty": 0.0,
                "investment": 0.0,
            }
            order.append(key)
        groups[key]["qty"] += row["qty"]
        groups[key]["investment"] += row["investment"]

    merged = []
    for key in order:
        g = groups[key]
        avg_price = g["investment"] / g["qty"] if g["qty"] else 0.0
        merged.append(
            {
                "raw_name": g["raw_name"],
                "symbol": g["symbol"],
                "qty": g["qty"],
                "avg_price": avg_price,
                "investment": g["investment"],
            }
        )
    return merged


def compute_portfolio_view(holdings: list[dict], ltp_by_symbol: dict[str, float]) -> tuple[list[dict], dict]:
    """Returns (rows, totals). Each row adds ltp/cur_val/pnl/pnl_pct,
    None when the symbol is unresolved or has no market data yet. Totals
    sum investment across every row, but cur_val/pnl/pnl_pct only over
    rows with a known LTP -- `priced_count`/`unpriced_count` let the
    caller caption a partial total."""
    rows = []
    total_investment = 0.0
    total_cur_val = 0.0
    priced_investment = 0.0
    unpriced_count = 0

    for h in holdings:
        symbol = h["symbol"]
        ltp = ltp_by_symbol.get(symbol) if symbol else None
        cur_val = h["qty"] * ltp if ltp is not None else None
        pnl = cur_val - h["investment"] if cur_val is not None else None
        pnl_pct = (pnl / h["investment"] * 100) if pnl is not None and h["investment"] else None

        rows.append(
            {
                "raw_name": h["raw_name"],
                "symbol": symbol,
                "qty": h["qty"],
                "avg_price": h["avg_price"],
                "investment": h["investment"],
                "ltp": ltp,
                "cur_val": cur_val,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
            }
        )

        total_investment += h["investment"]
        if cur_val is not None:
            total_cur_val += cur_val
            priced_investment += h["investment"]
        else:
            unpriced_count += 1

    total_pnl = total_cur_val - priced_investment if priced_investment else None
    total_pnl_pct = (total_pnl / priced_investment * 100) if total_pnl is not None and priced_investment else None

    totals = {
        "total_investment": total_investment,
        "total_cur_val": total_cur_val if priced_investment else None,
        "total_pnl": total_pnl,
        "total_pnl_pct": total_pnl_pct,
        "priced_count": len(holdings) - unpriced_count,
        "unpriced_count": unpriced_count,
    }
    return rows, totals


def resolve_tracked_symbols(
    portfolio_symbols: list[str],
    known_company_symbols: set[str],
    raw_name_by_symbol: dict[str, str],
) -> list[Company]:
    """Pure diff used by the refresh pipeline (scripts/run_refresh.py):
    which portfolio-only symbols need a minimal companies row registered
    before they can be fetched/priced. Never touches
    nifty50_constituents -- these stay excluded from the Dashboard's
    inner-joined view."""
    new_symbols = sorted(set(portfolio_symbols) - known_company_symbols)
    return [Company(symbol=symbol, name=raw_name_by_symbol.get(symbol, symbol)) for symbol in new_symbols]


def holdings_to_records(user_id: str, broker: str, holdings: list[dict]) -> list[PortfolioHolding]:
    """Converts parsed/merged holding dicts into PortfolioHolding rows
    ready for portfolio_repo.replace_broker_holdings."""
    return [
        PortfolioHolding(
            user_id=user_id,
            broker=broker,
            raw_name=h["raw_name"],
            symbol=h["symbol"],
            qty=h["qty"],
            avg_price=h["avg_price"],
            investment=h["investment"],
        )
        for h in holdings
    ]
