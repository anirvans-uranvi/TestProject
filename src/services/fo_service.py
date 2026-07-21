"""F&O ingestion + presentation-shaping.

`ingest_fo_day` persists one parsed bhavcopy day (from the real NSE provider
or the mock) into the four F&O tables. The `shape_*` / `*_summary` helpers
are pure functions that turn raw view rows into the structures the Options
screen renders, so they're unit-testable without Streamlit or a live DB.
"""
from __future__ import annotations

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


def _freshest_rows(rows: list[dict]) -> list[dict]:
    """Restrict to the rows whose `trade_date` matches the most recent
    `trade_date` present in `rows`. `latest_option_chain_view` is "latest
    row per contract", but illiquid strikes stop appearing in NSE's daily
    bhavcopy well before their expiry while liquid ones keep updating --
    so within one expiry, different strikes' "latest" rows can genuinely
    be weeks apart, and a stale strike's quoted premium no longer
    reflects reality (confirmed live: LT's 3640 PE strike, one expiry,
    July 2026 -- neighboring strikes had a trade_date of the current
    bhavcopy while 3640 itself hadn't traded since three weeks earlier,
    and its stale ~90 premium made "5% CSP" look ~30x too high). Returns
    `rows` unchanged if none of them have a `trade_date` at all (e.g. in
    tests), so callers with no staleness signal to go on behave exactly
    as before this existed.
    """
    dates = {r.get("trade_date") for r in rows if r.get("trade_date")}
    if not dates:
        return rows
    freshest = max(dates)
    return [r for r in rows if r.get("trade_date") == freshest]


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
    """Spot, ATM strike, aggregate CE/PE open interest, and Put-Call Ratio.

    `chain_rows` come from `latest_option_chain_view`, which is "latest per
    contract" -- individual strikes can genuinely fall stale independently
    of each other (e.g. a deep ITM/OTM contract with zero OI/volume simply
    stops appearing in NSE's daily bhavcopy well before its expiry, while
    liquid near-the-money strikes keep updating daily). So the page-level
    "as of" date and spot must come from the *freshest* trade_date present
    in the chain, not from whichever row happens to sort first by strike --
    picking an arbitrary row previously leaked a stale contract's date/spot
    into the whole page's summary even when most of the chain was current.
    """
    if not chain_rows:
        return {}
    trade_date = max((r.get("trade_date") for r in chain_rows if r.get("trade_date")), default=None)
    latest_rows = [r for r in chain_rows if r.get("trade_date") == trade_date] if trade_date else chain_rows
    spot = next((_num(r.get("underlying_price")) for r in latest_rows if r.get("underlying_price") is not None), None)
    total_ce_oi = sum(_int(r.get("open_interest")) or 0 for r in chain_rows if str(r.get("option_type")) == "CE")
    total_pe_oi = sum(_int(r.get("open_interest")) or 0 for r in chain_rows if str(r.get("option_type")) == "PE")
    strikes = sorted({_num(r.get("strike_price")) for r in chain_rows if r.get("strike_price") is not None})
    atm = min(strikes, key=lambda s: abs(s - spot)) if (spot is not None and strikes) else None
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


def csp_5pct_map(put_rows: list[dict], spot_by_symbol: dict[str, float | None]) -> dict[str, dict]:
    """Pure: for each symbol's PE legs (from
    `fo_repo.get_all_open_options(OptionType.PE)`, every symbol/expiry
    mixed together), restricts to that symbol's own nearest available
    expiry, finds the strike closest to 95% of spot **among strikes with
    the freshest available trade_date** (see `_freshest_rows`, falls back
    to every strike at that expiry if none of them carry a trade_date),
    and returns `{symbol: {"strike": ..., "put_price": ..., "csp_pct":
    ..., "spot": ..., "expiry_date": ...}}`. `spot`/`expiry_date` are just
    the inputs that produced this result, echoed back so a caller (the
    Options screen's "5% CSP" breakdown) can display the calculation
    without having to separately track which expiry/spot were actually
    used.

    "5% CSP" is a cash-secured-put yield: the premium for the strike
    nearest 5% below spot, as a percentage of that strike (the full
    notional a CSP seller sets aside per lot) -- i.e. `put_price / strike *
    100`. Deliberately NOT divided by exchange margin: SPAN margin isn't
    available from NSE as a simple published per-contract figure (it's a
    licensed multi-scenario risk calculation, not a downloadable
    percentage) -- see docs/CODEBASE_GUIDE.md's F&O section for the
    research trail. `strike * lot_size` cancels out of both the premium
    and this ratio, so lot size doesn't need to appear here at all.
    """
    by_symbol: dict[str, list[dict]] = {}
    for r in put_rows:
        if str(r.get("option_type")) != "PE":
            continue
        symbol = r.get("symbol")
        if not symbol:
            continue
        by_symbol.setdefault(symbol, []).append(r)

    result: dict[str, dict] = {}
    for symbol, rows in by_symbol.items():
        spot = spot_by_symbol.get(symbol)
        if spot is None:
            continue
        expiries = {r.get("expiry_date") for r in rows if r.get("expiry_date")}
        if not expiries:
            continue
        near_expiry = min(expiries)
        near_rows = [r for r in rows if r.get("expiry_date") == near_expiry and r.get("strike_price") is not None]
        if not near_rows:
            continue
        target = spot * 0.95
        best_row = min(_freshest_rows(near_rows), key=lambda r: abs(_num(r["strike_price"]) - target))
        strike = _num(best_row["strike_price"])
        put_price = _num(best_row.get("last_price")) or _num(best_row.get("close")) or _num(best_row.get("settlement_price"))
        csp_pct = (put_price / strike * 100) if (put_price is not None and strike) else None
        result[symbol] = {
            "strike": strike,
            "put_price": put_price,
            "csp_pct": csp_pct,
            "spot": spot,
            "expiry_date": near_expiry,
        }
    return result


def itm_pmcc_5pct_map(option_rows: list[dict], spot_by_symbol: dict[str, float | None]) -> dict[str, dict]:
    """Pure: for each symbol's CE+PE legs (from
    `fo_repo.get_all_open_options()`, every symbol/expiry/type mixed
    together), restricts to that symbol's own nearest available expiry and
    builds the "5% ITM PMCC" column:

    1. Buy 1 lot of the ITM CE closest to spot (largest strike < spot,
       preferring strikes with the freshest available trade_date -- see
       `_freshest_rows` -- and only falling back to a stale one if no
       fresh strike is actually ITM).
    2. Sell 1 lot of the PE at that *same* strike.
    3. Sell 1 lot of the CE whose strike is closest to 95% of the bought
       CE's strike (a further-ITM call, same freshness preference as step 1).
    4. Net credit = PE sell price + CE sell price - CE buy price.
    5. `pmcc_pct` = net credit / the bought CE's strike * 100.

    As with `csp_5pct_map`, `strike * lot_size` cancels out of both the
    premiums and this ratio (each leg is 1 lot), so lot size never needs
    to appear here. Returns `{symbol: {"itm_ce_strike", "otm_ce_strike",
    "buy_ce_price", "sell_pe_price", "sell_ce_price", "net_credit",
    "pmcc_pct", "spot", "expiry_date"}}` -- the per-leg prices and inputs
    are included (not just the final net credit) so a caller (the
    Options screen's "5% ITM PMCC" breakdown) can show the full
    calculation, not just the result. A symbol is omitted if there's no
    ITM CE, no PE at that strike, or a price is missing for any leg.
    """
    by_symbol: dict[str, list[dict]] = {}
    for r in option_rows:
        symbol = r.get("symbol")
        if not symbol:
            continue
        by_symbol.setdefault(symbol, []).append(r)

    def _price(r: dict) -> float | None:
        return _num(r.get("last_price")) or _num(r.get("close")) or _num(r.get("settlement_price"))

    result: dict[str, dict] = {}
    for symbol, rows in by_symbol.items():
        spot = spot_by_symbol.get(symbol)
        if spot is None:
            continue
        expiries = {r.get("expiry_date") for r in rows if r.get("expiry_date")}
        if not expiries:
            continue
        near_expiry = min(expiries)
        near_rows = [r for r in rows if r.get("expiry_date") == near_expiry and r.get("strike_price") is not None]
        ce_rows = [r for r in near_rows if str(r.get("option_type")) == "CE"]
        pe_rows = [r for r in near_rows if str(r.get("option_type")) == "PE"]
        if not ce_rows or not pe_rows:
            continue

        # Prefer strikes with the freshest available trade_date (see
        # _freshest_rows) -- an illiquid strike's "latest" row can be
        # weeks older than its liquid neighbors', with a premium that no
        # longer reflects reality. Falls back to the full (possibly
        # stale-inclusive) ce_rows if the freshest-only subset has no ITM
        # candidate at all, so a symbol isn't dropped just because its
        # single most-recent trade_date happens to have no strike below
        # spot.
        fresh_ce_rows = _freshest_rows(ce_rows)
        itm_ce_candidates = [r for r in fresh_ce_rows if _num(r["strike_price"]) < spot]
        if not itm_ce_candidates:
            itm_ce_candidates = [r for r in ce_rows if _num(r["strike_price"]) < spot]
        if not itm_ce_candidates:
            continue
        buy_ce = max(itm_ce_candidates, key=lambda r: _num(r["strike_price"]))
        itm_strike = _num(buy_ce["strike_price"])
        buy_ce_price = _price(buy_ce)

        pe_same_strike = [r for r in pe_rows if _num(r["strike_price"]) == itm_strike]
        if not pe_same_strike:
            continue
        sell_pe_price = _price(pe_same_strike[0])

        target = itm_strike * 0.95
        sell_ce = min(fresh_ce_rows, key=lambda r: abs(_num(r["strike_price"]) - target))
        otm_strike = _num(sell_ce["strike_price"])
        sell_ce_price = _price(sell_ce)

        if buy_ce_price is None or sell_pe_price is None or sell_ce_price is None or not itm_strike:
            continue

        net_credit = sell_pe_price + sell_ce_price - buy_ce_price
        pmcc_pct = net_credit / itm_strike * 100
        result[symbol] = {
            "itm_ce_strike": itm_strike,
            "otm_ce_strike": otm_strike,
            "buy_ce_price": buy_ce_price,
            "sell_pe_price": sell_pe_price,
            "sell_ce_price": sell_ce_price,
            "net_credit": net_credit,
            "pmcc_pct": pmcc_pct,
            "spot": spot,
            "expiry_date": near_expiry,
        }
    return result
