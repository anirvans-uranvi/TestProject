"""F&O ingestion + presentation-shaping.

`ingest_fo_day` persists one parsed bhavcopy day (from the real NSE provider
or the mock) into the four F&O tables. The `shape_*` / `*_summary` helpers
are pure functions that turn raw view rows into the structures the Options
screen renders, so they're unit-testable without Streamlit or a live DB.
"""
from __future__ import annotations

from supabase import Client

from src.data_providers.nse_fo_provider import FOBhavcopy
from src.repositories import fo_repo, snapshot_repo


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
    ..., "spot": ..., "expiry_date": ..., "put_trade_date": ...}}`.
    `spot`/`expiry_date` are just the inputs that produced this result,
    echoed back so a caller (the Options screen's "5% CSP" breakdown) can
    display the calculation without having to separately track which
    expiry/spot were actually used. `put_trade_date` is the chosen row's
    own `trade_date` -- this is EOD bhavcopy data (no intraday execution
    timestamp exists), so it's a trading day, not a time of day; showing
    it next to the premium in the UI is what actually surfaces a stale
    quote when `_freshest_rows` had to fall back to one (nothing else in
    the result makes staleness visible after the fact).

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
        near_rows = [r for r in rows if r.get("expiry_date") == near_expiry]
        csp = csp_5pct_for_rows(near_rows, spot, near_expiry)
        if csp is None:
            continue
        result[symbol] = csp
    return result


def csp_5pct_for_rows(pe_rows: list[dict], spot: float, expiry_date) -> dict | None:
    """Pure: the single-expiry core of `csp_5pct_map`, factored out so a
    caller can compute "5% CSP" for a *specific* expiry rather than only
    ever the nearest one -- used by the Options screen to show a
    near/next/far month row each, the same term-structure shape the
    Futures section already uses. `pe_rows` should already be filtered
    to one symbol + one expiry (any option_type mixed in is ignored).
    Returns the same shape as one `csp_5pct_map` result value, or `None`
    if there's no priceable PE strike in `pe_rows`.
    """
    near_rows = [r for r in pe_rows if str(r.get("option_type")) == "PE" and r.get("strike_price") is not None]
    if not near_rows:
        return None
    target = spot * 0.95
    best_row = min(_freshest_rows(near_rows), key=lambda r: abs(_num(r["strike_price"]) - target))
    strike = _num(best_row["strike_price"])
    put_price = _num(best_row.get("last_price")) or _num(best_row.get("close")) or _num(best_row.get("settlement_price"))
    csp_pct = (put_price / strike * 100) if (put_price is not None and strike) else None
    return {
        "strike": strike,
        "put_price": put_price,
        "csp_pct": csp_pct,
        "spot": spot,
        "expiry_date": expiry_date,
        "put_trade_date": best_row.get("trade_date"),
    }


def itm_pmcc_5pct_map(option_rows: list[dict], spot_by_symbol: dict[str, float | None]) -> dict[str, dict]:
    """Pure: for each symbol's CE+PE legs (from
    `fo_repo.get_all_open_options()`, every symbol/expiry/type mixed
    together), restricts to that symbol's own nearest available expiry
    and delegates to `itm_pmcc_for_rows` to build the "5% ITM PMCC"
    column. See that function's docstring for the calculation itself.
    """
    by_symbol: dict[str, list[dict]] = {}
    for r in option_rows:
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
        near_rows = [r for r in rows if r.get("expiry_date") == near_expiry]
        pmcc = itm_pmcc_for_rows(near_rows, spot, near_expiry)
        if pmcc is None:
            continue
        result[symbol] = pmcc
    return result


def itm_pmcc_for_rows(leg_rows: list[dict], spot: float, expiry_date) -> dict | None:
    """Pure: the single-expiry core of `itm_pmcc_5pct_map`, factored out
    so a caller can compute "5% ITM PMCC" for a *specific* expiry rather
    than only ever the nearest one -- mirrors `csp_5pct_for_rows`'s same
    extraction, used by the Dashboard's precomputed cache to store a
    near/next/far row per symbol (see `dashboard_metrics_rows`).
    `leg_rows` should already be filtered to one symbol + one expiry
    (both CE and PE legs mixed in, as `itm_pmcc_5pct_map` already
    expects). Builds:

    1. Buy 1 lot of the ITM CE closest to spot (largest strike < spot,
       preferring strikes with the freshest available trade_date -- see
       `_freshest_rows` -- and only falling back to a stale one if no
       fresh strike is actually ITM).
    2. Sell 1 lot of the PE at that *same* strike.
    3. Sell 1 lot of the CE whose strike is closest to 95% of the bought
       CE's strike (a further-ITM call, same freshness preference as step 1).
    4. Net credit = PE sell price + CE sell price - CE buy price.
    5. `pmcc_pct` = net credit / the bought CE's strike * 100.

    As with `csp_5pct_for_rows`, `strike * lot_size` cancels out of both
    the premiums and this ratio (each leg is 1 lot), so lot size never
    needs to appear here. Returns `{"itm_ce_strike", "otm_ce_strike",
    "buy_ce_price", "sell_pe_price", "sell_ce_price", "net_credit",
    "pmcc_pct", "spot", "expiry_date", "buy_ce_trade_date",
    "sell_pe_trade_date", "sell_ce_trade_date"}` -- the per-leg prices
    and inputs are included (not just the final net credit) so a caller
    (the Options screen's "5% ITM PMCC" breakdown) can show the full
    calculation, not just the result. The three `*_trade_date` fields are
    each leg's own row's `trade_date` (a trading day, not a time of day
    -- this is EOD bhavcopy data, no intraday execution timestamp
    exists), so a stale fallback leg (see the ITM-candidate fallback
    below) is visible in the UI rather than silently blending in with
    the fresher legs around it. Returns `None` if there's no ITM CE, no
    PE at that strike, or a price is missing for any leg.
    """
    rows = [r for r in leg_rows if r.get("strike_price") is not None]
    ce_rows = [r for r in rows if str(r.get("option_type")) == "CE"]
    pe_rows = [r for r in rows if str(r.get("option_type")) == "PE"]
    if not ce_rows or not pe_rows:
        return None

    def _price(r: dict) -> float | None:
        return _num(r.get("last_price")) or _num(r.get("close")) or _num(r.get("settlement_price"))

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
        return None
    buy_ce = max(itm_ce_candidates, key=lambda r: _num(r["strike_price"]))
    itm_strike = _num(buy_ce["strike_price"])
    buy_ce_price = _price(buy_ce)

    pe_same_strike = [r for r in pe_rows if _num(r["strike_price"]) == itm_strike]
    if not pe_same_strike:
        return None
    sell_pe_price = _price(pe_same_strike[0])

    target = itm_strike * 0.95
    sell_ce = min(fresh_ce_rows, key=lambda r: abs(_num(r["strike_price"]) - target))
    otm_strike = _num(sell_ce["strike_price"])
    sell_ce_price = _price(sell_ce)

    if buy_ce_price is None or sell_pe_price is None or sell_ce_price is None or not itm_strike:
        return None

    net_credit = sell_pe_price + sell_ce_price - buy_ce_price
    pmcc_pct = net_credit / itm_strike * 100
    return {
        "itm_ce_strike": itm_strike,
        "otm_ce_strike": otm_strike,
        "buy_ce_price": buy_ce_price,
        "sell_pe_price": sell_pe_price,
        "sell_ce_price": sell_ce_price,
        "net_credit": net_credit,
        "pmcc_pct": pmcc_pct,
        "spot": spot,
        "expiry_date": expiry_date,
        "buy_ce_trade_date": buy_ce.get("trade_date"),
        "sell_pe_trade_date": pe_same_strike[0].get("trade_date"),
        "sell_ce_trade_date": sell_ce.get("trade_date"),
    }


def dashboard_metrics_rows(option_rows: list[dict], spot_by_symbol: dict[str, float | None]) -> list[dict]:
    """Pure: for each symbol with a spot price and open option legs,
    computes "5% CSP" / "5% ITM PMCC" (via `csp_5pct_for_rows` /
    `itm_pmcc_for_rows`, the same already-tested calculations the
    Dashboard has always used) for each of that symbol's **up to 3
    nearest distinct expiries** -- near/next/far, the same term-structure
    shape `pages/5_Options.py`'s Futures section and CSP table already
    use -- and returns one flat row per **(symbol, expiry)**, shaped for
    `dashboard_fo_metrics` (see migration
    0010_dashboard_fo_metrics_per_expiry.sql). This lets the Dashboard
    offer a month dropdown that just selects which already-cached row to
    display, with no live recomputation on selection.

    A symbol with no spot price or no option data gets zero rows (there's
    no `expiry_date` to key a row on, and the column is `not null`) --
    the Dashboard's lookup for that symbol at any selected month then
    simply finds nothing, which it already treats as "N/A". A symbol with
    fewer than 3 expiries just gets fewer rows. `csp_pct`/`pmcc_pct` are
    `None` independently of each other when either calculation has no
    priceable result for that specific expiry (e.g. no ITM CE that month).
    """
    legs_by_symbol: dict[str, list[dict]] = {}
    for r in option_rows:
        symbol = r.get("symbol")
        if not symbol:
            continue
        legs_by_symbol.setdefault(symbol, []).append(r)

    rows: list[dict] = []
    for symbol, spot in spot_by_symbol.items():
        if spot is None:
            continue
        legs = legs_by_symbol.get(symbol)
        if not legs:
            continue
        expiries = sorted({r.get("expiry_date") for r in legs if r.get("expiry_date")})[:3]
        for expiry in expiries:
            expiry_rows = [r for r in legs if r.get("expiry_date") == expiry]
            csp = csp_5pct_for_rows(expiry_rows, spot, expiry)
            pmcc = itm_pmcc_for_rows(expiry_rows, spot, expiry)
            rows.append(
                {
                    "symbol": symbol,
                    "expiry_date": expiry,
                    "spot": spot,
                    "csp_strike": csp["strike"] if csp else None,
                    "csp_put_price": csp["put_price"] if csp else None,
                    "csp_pct": csp["csp_pct"] if csp else None,
                    "csp_put_trade_date": csp["put_trade_date"] if csp else None,
                    "pmcc_itm_ce_strike": pmcc["itm_ce_strike"] if pmcc else None,
                    "pmcc_otm_ce_strike": pmcc["otm_ce_strike"] if pmcc else None,
                    "pmcc_buy_ce_price": pmcc["buy_ce_price"] if pmcc else None,
                    "pmcc_sell_pe_price": pmcc["sell_pe_price"] if pmcc else None,
                    "pmcc_sell_ce_price": pmcc["sell_ce_price"] if pmcc else None,
                    "pmcc_net_credit": pmcc["net_credit"] if pmcc else None,
                    "pmcc_pct": pmcc["pmcc_pct"] if pmcc else None,
                    "pmcc_buy_ce_trade_date": pmcc["buy_ce_trade_date"] if pmcc else None,
                    "pmcc_sell_pe_trade_date": pmcc["sell_pe_trade_date"] if pmcc else None,
                    "pmcc_sell_ce_trade_date": pmcc["sell_ce_trade_date"] if pmcc else None,
                }
            )
    return rows


def recompute_dashboard_metrics(client: Client) -> int:
    """Recomputes and upserts the whole `dashboard_fo_metrics` cache table
    -- the single Python entrypoint every refresh path calls (cron's
    `scripts/run_refresh.py`, `scripts/fetch_fo_data.py`). Reads the same
    two inputs the Dashboard used to read live (spot prices from
    `latest_screener_view`, open option legs from
    `latest_option_chain_view`) and writes the result (up to 3 rows per
    symbol, one per near/next/far expiry) so the Dashboard can just read
    the small cache table instead. Returns the row count for logging."""
    screener_rows = snapshot_repo.get_latest_screener(client)
    spot_by_symbol = {r.symbol: r.latest_price for r in screener_rows}
    option_rows = fo_repo.get_all_open_options(client)
    rows = dashboard_metrics_rows(option_rows, spot_by_symbol)
    fo_repo.upsert_dashboard_fo_metrics(client, rows)
    return len(rows)
