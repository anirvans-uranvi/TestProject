"""F&O ingestion + presentation-shaping.

`ingest_fo_day` persists one parsed bhavcopy day (from the real NSE provider
or the mock) into the four F&O tables. The `shape_*` / `*_summary` helpers
are pure functions that turn raw view rows into the structures the Options
screen renders, so they're unit-testable without Streamlit or a live DB.
"""
from __future__ import annotations

from datetime import date

from supabase import Client

from src.data_providers.nse_fo_provider import FOBhavcopy
from src.repositories import fo_repo


def ingest_fo_day(client: Client, book: FOBhavcopy) -> dict[str, int]:
    """Upsert one trading day's futures + option contracts and prices.

    Returns row counts for logging. `is_open` is left provisional here and
    finalized once per run by `fo_repo.refresh_open_flags`, since a contract's
    open/closed state depends on the real calendar, not any single file day.
    """
    fo_repo.upsert_futures_contracts(client, book.futures_contracts)
    fo_repo.upsert_futures_prices(client, book.futures_prices)
    fo_repo.upsert_option_contracts(client, book.option_contracts)
    fo_repo.upsert_option_prices(client, book.option_prices)
    return {
        "futures_contracts": len(book.futures_contracts),
        "futures_prices": len(book.futures_prices),
        "option_contracts": len(book.option_contracts),
        "option_prices": len(book.option_prices),
    }


def _num(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value) -> int | None:
    n = _num(value)
    return int(n) if n is not None else None


def shape_option_chain(chain_rows: list[dict]) -> list[dict]:
    """Pivot per-leg option rows (from latest_option_chain_view) into one row
    per strike: {strike, ce_last, ce_oi, ce_change_oi, ce_volume, pe_...}.
    Sorted ascending by strike (classic option-chain layout)."""
    by_strike: dict[float, dict] = {}
    for r in chain_rows:
        strike = _num(r.get("strike_price"))
        if strike is None:
            continue
        slot = by_strike.setdefault(strike, {"strike": strike})
        side = "ce" if str(r.get("option_type")) == "CE" else "pe"
        slot[f"{side}_last"] = _num(r.get("last_price")) or _num(r.get("close"))
        slot[f"{side}_settlement"] = _num(r.get("settlement_price"))
        slot[f"{side}_oi"] = _int(r.get("open_interest"))
        slot[f"{side}_change_oi"] = _int(r.get("change_in_oi"))
        slot[f"{side}_volume"] = _int(r.get("volume"))
    return [by_strike[k] for k in sorted(by_strike)]


def option_chain_summary(chain_rows: list[dict]) -> dict:
    """Spot, ATM strike, aggregate CE/PE open interest, and Put-Call Ratio."""
    if not chain_rows:
        return {}
    spot = next((_num(r.get("underlying_price")) for r in chain_rows if r.get("underlying_price") is not None), None)
    total_ce_oi = sum(_int(r.get("open_interest")) or 0 for r in chain_rows if str(r.get("option_type")) == "CE")
    total_pe_oi = sum(_int(r.get("open_interest")) or 0 for r in chain_rows if str(r.get("option_type")) == "PE")
    strikes = sorted({_num(r.get("strike_price")) for r in chain_rows if r.get("strike_price") is not None})
    atm = min(strikes, key=lambda s: abs(s - spot)) if (spot is not None and strikes) else None
    trade_date = next((r.get("trade_date") for r in chain_rows if r.get("trade_date")), None)
    return {
        "spot": spot,
        "atm_strike": atm,
        "total_ce_oi": total_ce_oi,
        "total_pe_oi": total_pe_oi,
        "pcr": (total_pe_oi / total_ce_oi) if total_ce_oi else None,
        "trade_date": trade_date,
    }


def futures_term_structure(futures_rows: list[dict]) -> list[dict]:
    """Annotate open-futures rows (from latest_futures_view) with basis vs
    spot (future last/settlement minus underlying), sorted by expiry."""
    shaped = []
    for r in sorted(futures_rows, key=lambda x: x.get("expiry_date") or ""):
        last = _num(r.get("last_price")) or _num(r.get("close")) or _num(r.get("settlement_price"))
        spot = _num(r.get("underlying_price"))
        shaped.append(
            {
                "expiry_date": r.get("expiry_date"),
                "last_price": last,
                "settlement_price": _num(r.get("settlement_price")),
                "underlying_price": spot,
                "basis": (last - spot) if (last is not None and spot is not None) else None,
                "open_interest": _int(r.get("open_interest")),
                "change_in_oi": _int(r.get("change_in_oi")),
                "volume": _int(r.get("volume")),
                "lot_size": _int(r.get("lot_size")),
            }
        )
    return shaped
