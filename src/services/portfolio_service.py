"""Broker API response translation and valuation for the Portfolio
feature's pages (7_My_Trades.py, 8_My_Holdings.py, 9_My_Positions.py,
10_Analyse_Trade.py, 11_My_CSP.py, 12_My_Portfolio_Trades.py,
15_Other_Stock_Holdings.py, 14_Trade_History.py, 5_Options.py) --
holdings/positions come from a live
Dhan sync (Settings' "Data Provider" section,
src/utils/data_provider_settings.py) only; CSV upload was dropped
entirely once that became the account's one live data source. Holdings
and positions are plain dicts throughout (not a dataclass) -- same
convention as fo_service's row-dict outputs. Holdings keys: raw_name,
symbol (str | None), qty, avg_price, investment. Positions keys:
raw_name, symbol (str | None, the underlying), expiry_date (date | None),
strike_price (float | None), option_type (OptionType | None), qty
(signed -- negative is short), avg_price, ltp.
"""
from __future__ import annotations

from collections import Counter, deque
from datetime import date, datetime

from src.models.company import Company
from src.models.enums import CompanyType, OptionType
from src.models.portfolio import PortfolioHolding, PortfolioPosition, PortfolioTradeFill


def dhan_holdings_from_api(rows: list[dict]) -> list[dict]:
    """Translates GET /v2/holdings rows (src/data_providers/dhan_provider.py's
    get_holdings()) into this app's own holding-dict shape, so
    holdings_to_records/merge_holdings/compute_portfolio_view are reused
    unchanged regardless of source. `tradingSymbol` is already the exact
    NSE symbol -- no fuzzy name matching needed. Skips rows with no
    quantity (a holding fully sold off today)."""
    holdings = []
    for row in rows:
        qty = row.get("totalQty") or 0
        symbol = str(row.get("tradingSymbol") or "").strip().upper()
        if not qty or not symbol:
            continue
        avg_price = float(row.get("avgCostPrice") or 0)
        holdings.append(
            {
                "raw_name": symbol,
                "symbol": symbol,
                "qty": float(qty),
                "avg_price": avg_price,
                "investment": float(qty) * avg_price,
            }
        )
    return holdings


def _dhan_underlying_symbol(trading_symbol: str, is_derivative: bool, option_type_raw: str) -> str:
    """Underlying extraction from Dhan's `tradingSymbol`. A plain
    equity/ETF position's tradingSymbol IS the underlying already, no
    expiry/strike/type suffix at all (e.g. "SILVERBEES") -- returned
    verbatim, whatever characters it contains. A real derivative's
    tradingSymbol instead appends "-<expiry>-<strike>-<CE/PE>" (option) or
    "-<expiry>-<FUT-ish token>" (future) -- confirmed live against a real
    account as e.g. "NIFTY-Aug2026-23000-PE"/"HDFCBANK-Aug2026-700-PE" --
    split from the RIGHT by a known trailing-token count (same approach as
    dhan_provider.py's _underlying_from_trading_symbol), not on the first
    hyphen or a leading-alphabetic-run match, since the underlying itself
    can contain a non-letter character (confirmed live: an earlier
    leading-alphabetic-run regex truncated "M&M" -- Mahindra & Mahindra's
    real NSE symbol -- down to just "M", the underlying's live LTP/
    Momentum/1D/5D/20D silently showing blank for that leg since "M" isn't
    a real symbol) or its own hyphen (e.g. "NAM-INDIA", "BAJAJ-AUTO").

    `is_derivative` -- this row's own drvExpiryDate being a real date, not
    absent/the no-expiry sentinel (_DHAN_NO_EXPIRY_SENTINEL) -- is what
    decides which branch applies, NOT whether tradingSymbol happens to
    contain a hyphen: a plain hyphenated equity/ETF symbol (e.g. an
    intraday "NAM-INDIA" trade) would otherwise be indistinguishable from
    a genuine 2-token futures suffix, and get wrongly split too."""
    symbol = trading_symbol.strip().upper()
    if not is_derivative:
        return symbol
    parts = symbol.split("-")
    trailing = 3 if option_type_raw.strip().upper() in ("PUT", "PE", "CALL", "CE") else 2
    return "-".join(parts[: len(parts) - trailing]) if len(parts) > trailing else parts[0]


# Confirmed against a real GET /v2/positions response: drvOptionType comes
# back as the full word ("PUT"/"CALL"), not the CE/PE code Dhan uses
# elsewhere (e.g. option_contracts.option_type) -- both spellings are
# accepted here for safety.
_DHAN_OPTION_TYPES = {"PUT": OptionType.PE, "PE": OptionType.PE, "CALL": OptionType.CE, "CE": OptionType.CE}


# Dhan's documented sentinel for "this position has no real F&O expiry"
# -- confirmed live: an equity/ETF position (e.g. an intraday SILVERBEES
# trade showing up via /v2/positions, not just /v2/holdings) still comes
# back with a `drvExpiryDate` key, but set to this placeholder rather
# than null/omitted. A plain `if expiry_raw:` truthiness check treats
# that non-empty string as a real expiry, producing a position with a
# bogus far-past "expiry" but no strike/option_type -- which this app's
# own "expiry set, strike/type both blank -> must be a future" heuristic
# (src/utils/refresh_bar.py's _dhan_fo_universe) then misreads as a
# phantom futures contract for a symbol that was never a derivative at
# all.
_DHAN_NO_EXPIRY_SENTINEL = "0001-01-01"


def dhan_positions_from_api(rows: list[dict], ltp_by_security_id: dict[str, float]) -> list[dict]:
    """Translates GET /v2/positions rows into this app's own position-dict
    shape. expiry/strike/type come straight from Dhan's own
    drvExpiryDate/drvStrikePrice/drvOptionType -- no regex
    instrument-name decoding needed. `netQty` is already signed
    (positive long, negative short), matching this app's convention. `ltp`
    comes from a separate Market Quote call (dhan_provider.get_ltp_by_security_id)
    since the positions payload itself carries no live price. Skips closed
    (netQty == 0) rows -- Dhan still lists those for the trading day.

    Dhan can return more than one /v2/positions row sharing one
    tradingSymbol -- confirmed live (postgrest APIError 23505,
    "portfolio_positions_pkey") -- and not only via the most obvious
    cause (an INTRADAY trade alongside a carried-forward CNC/NRML
    position on the same securityId): a confirmed-live account hit this
    with no intraday/overnight overlap at all, so some other Dhan-side
    duplication (e.g. a repeated/legged entry) can produce it too.
    portfolio_positions' primary key is (user_id, portfolio_name, broker,
    raw_name), so ANY such collision fails the whole sync's insert.
    _dedupe_raw_names below disambiguates exhaustively -- productType,
    then securityId, then a bare ordinal -- so the insert can never
    23505 regardless of cause, while an account with no collision at all
    keeps today's exact raw_name (preserving trade_date/stop_loss/
    trade-group links, keyed on (broker, raw_name))."""
    positions = []
    for row in rows:
        qty = row.get("netQty") or 0
        if not qty:
            continue
        trading_symbol = str(row.get("tradingSymbol") or "").strip()
        security_id = str(row.get("securityId") or "")
        product_type = str(row.get("productType") or "").strip()
        cost_price = row.get("costPrice")
        if cost_price:
            avg_price = float(cost_price)
        elif qty > 0:
            avg_price = float(row.get("buyAvg") or 0)
        else:
            avg_price = float(row.get("sellAvg") or 0)
        expiry_raw = row.get("drvExpiryDate")
        expiry_str = str(expiry_raw)[:10] if expiry_raw else None
        is_derivative = bool(expiry_str and expiry_str != _DHAN_NO_EXPIRY_SENTINEL)
        option_type_raw = str(row.get("drvOptionType") or "").strip().upper()
        strike_raw = row.get("drvStrikePrice")
        positions.append(
            {
                "raw_name": trading_symbol or security_id,
                "_dedupe_tiebreakers": [product_type, security_id],
                "symbol": (
                    _dhan_underlying_symbol(trading_symbol, is_derivative, option_type_raw)
                    if trading_symbol
                    else None
                ),
                "expiry_date": date.fromisoformat(expiry_str) if is_derivative else None,
                "strike_price": float(strike_raw) if strike_raw else None,
                "option_type": _DHAN_OPTION_TYPES.get(option_type_raw),
                "qty": float(qty),
                "avg_price": avg_price,
                "ltp": ltp_by_security_id.get(security_id),
            }
        )
    _dedupe_raw_names(positions)
    return positions


def _dedupe_raw_names(positions: list[dict]) -> None:
    """Mutates `raw_name` in place so it's unique across `positions`,
    trying each dict's `_dedupe_tiebreakers` (broker-specific identifiers,
    most useful first) in order, falling back to a bare ordinal suffix if
    every tiebreaker is exhausted (blank, or still colliding -- e.g. Dhan
    returning a literal duplicate row) -- see dhan_positions_from_api's
    docstring for why this must never fail to produce a unique value. A
    `raw_name` that was never involved in any collision is left exactly
    as given. Pops `_dedupe_tiebreakers` from every dict as a side effect."""
    counts = Counter(p["raw_name"] for p in positions)
    seen: set[str] = set()
    for p in positions:
        tiebreakers = p.pop("_dedupe_tiebreakers")
        base = p["raw_name"]
        if counts[base] <= 1:
            seen.add(base)
            continue
        candidate = base
        for tiebreaker in tiebreakers:
            if tiebreaker and f"{base} ({tiebreaker})" not in seen:
                candidate = f"{base} ({tiebreaker})"
                break
        if candidate == base or candidate in seen:
            n = 2
            while f"{base} #{n}" in seen:
                n += 1
            candidate = f"{base} #{n}"
        p["raw_name"] = candidate
        seen.add(candidate)


_DHAN_TRADE_NO_EXPIRY_SENTINEL = "1970-01-01"  # /v2/trades' own "no real
# expiry" placeholder -- confirmed live NOT the same value /v2/positions
# uses (_DHAN_NO_EXPIRY_SENTINEL, "0001-01-01"); both are checked below
# since apparently each endpoint has its own convention.


def dhan_trade_fills_from_api(rows: list[dict], symbol_by_security_id: dict[str, str] | None = None) -> list[dict]:
    """Translates GET /v2/trades rows (dhan_provider.get_trade_history)
    into this app's own trade-fill-dict shape, for trade_fills_to_records.

    Confirmed live against a real account (2026-08-27, then again
    2026-08-31 for a CNC/equity fill specifically): /v2/trades DOES carry
    structured drvExpiryDate/drvStrikePrice/drvOptionType fields directly,
    same as /v2/positions -- so contract detail is read the same way
    dhan_positions_from_api does (_DHAN_OPTION_TYPES; drvOptionType comes
    back the literal string "NA", and drvStrikePrice 0.0, for a
    non-derivative fill -- both already fall through to None via the
    existing falsy/unmapped-key checks with no special-casing needed).

    `customSymbol`'s format turned out to depend entirely on what's being
    traded, confirmed live from two separate real samples:
    - A derivative (option/future) fill's customSymbol IS a real ticker
      as its first space-separated token (e.g. "GOLD 31 AUG 135000 PUT"
      -> "GOLD", "NIFTY 01 SEP 25000 CALL" -> "NIFTY") -- NOT the hyphen-
      joined format `_dhan_underlying_symbol` was built for, so that
      function doesn't apply here, but the plain `.split()[0]` is correct
      and reliable for this case.
    - A non-derivative (equity/ETF/fund, productType "CNC") fill's
      customSymbol is instead a free-text DISPLAY name -- "Coal India",
      "Oil & Natural Gas Corporation", "Nippon Nifty 50 ETF (NIFTYBEES)"
      -- not a ticker at all, and inconsistently formatted (some funds
      have no ticker in parentheses at all, e.g. "LIC Nifty 10 Year
      G-Sec ETF"). `.split()[0]` on these gives nonsense ("Coal", "Oil",
      "Nippon" respectively) -- a real bug caught by testing against a
      second live sample after the first (option-only) sample's
      unconfirmed equity-branch guess turned out wrong. `securityId` ->
      dhan_equity_instruments' own trading_symbol (`symbol_by_security_id`,
      built by the caller from dhan_instrument_repo.get_equity_instruments
      -- the same instrument master "Refresh Instrument Master - Dhan"
      already populates) is the reliable resolution instead. Falls back to
      the raw customSymbol display name, unresolved, when the security_id
      isn't in the map (e.g. instrument master not yet refreshed) -- same
      "still saved and shown, just unresolved" convention as every other
      *_from_api function here.

    **A real bug in Dhan's own API found here**: `exchangeTradeId` --
    documented as the exchange's unique-per-fill trade identifier, and
    this table's originally-intended natural key -- comes back the
    literal string `"0"` on every single fill (confirmed live across 400
    rows spanning multiple orders/symbols/times). It is NOT usable as a
    unique key. A synthetic composite (`orderId:exchangeOrderId:
    exchangeTime:tradedQuantity:tradedPrice`) is used instead, stored in
    the same `exchange_trade_id` column -- stable across re-syncs of the
    same fill (all inputs are immutable once a trade has settled), so
    upsert_trade_fills' dedup still works correctly."""
    symbol_by_security_id = symbol_by_security_id or {}
    fills = []
    for row in rows:
        custom_symbol = str(row.get("customSymbol") or "").strip()
        security_id = str(row.get("securityId") or "")
        expiry_raw = row.get("drvExpiryDate")
        expiry_str = str(expiry_raw)[:10] if expiry_raw else None
        is_derivative = bool(
            expiry_str and expiry_str not in (_DHAN_NO_EXPIRY_SENTINEL, _DHAN_TRADE_NO_EXPIRY_SENTINEL)
        )
        option_type_raw = str(row.get("drvOptionType") or "").strip().upper()
        strike_raw = row.get("drvStrikePrice")
        order_id = str(row.get("orderId") or "")
        exchange_time = row.get("exchangeTime")
        traded_qty = row.get("tradedQuantity")
        traded_price = row.get("tradedPrice")
        if is_derivative:
            symbol = custom_symbol.split()[0] if custom_symbol else None
        else:
            symbol = symbol_by_security_id.get(security_id) or (custom_symbol or None)
        fills.append(
            {
                # See docstring: Dhan's own exchangeTradeId is always "0" on
                # this endpoint, so a synthetic composite stands in for it.
                "exchange_trade_id": f"{order_id}:{row.get('exchangeOrderId')}:{exchange_time}:{traded_qty}:{traded_price}",
                "order_id": order_id or None,
                "raw_name": custom_symbol or security_id,
                "symbol": symbol,
                "expiry_date": date.fromisoformat(expiry_str) if is_derivative else None,
                "strike_price": float(strike_raw) if strike_raw else None,
                "option_type": _DHAN_OPTION_TYPES.get(option_type_raw),
                "transaction_type": str(row.get("transactionType") or "").strip().upper(),
                "qty": float(traded_qty or 0),
                "price": float(traded_price or 0),
                "product_type": row.get("productType"),
                "traded_at": datetime.fromisoformat(exchange_time),
                "brokerage": float(row.get("brokerageCharges") or 0),
                "taxes_and_charges": sum(
                    float(row.get(key) or 0)
                    for key in ("sebiTax", "stt", "serviceTax", "exchangeTransactionCharges", "stampDuty")
                ),
            }
        )
    return fills


def trade_fills_to_records(
    user_id: str, portfolio_name: str, broker: str, fills: list[dict]
) -> list[PortfolioTradeFill]:
    """Converts parsed trade-fill dicts into PortfolioTradeFill rows ready
    for portfolio_repo.upsert_trade_fills -- same conversion pattern as
    holdings_to_records/positions_to_records."""
    return [
        PortfolioTradeFill(
            user_id=user_id,
            portfolio_name=portfolio_name,
            broker=broker,
            exchange_trade_id=f["exchange_trade_id"],
            order_id=f.get("order_id"),
            raw_name=f["raw_name"],
            symbol=f["symbol"],
            expiry_date=f["expiry_date"],
            strike_price=f["strike_price"],
            option_type=f["option_type"],
            transaction_type=f["transaction_type"],
            qty=f["qty"],
            price=f["price"],
            product_type=f.get("product_type"),
            traded_at=f["traded_at"],
            brokerage=f.get("brokerage", 0),
            taxes_and_charges=f.get("taxes_and_charges", 0),
        )
        for f in fills
    ]


def _trade_fill_contract_key(fill: PortfolioTradeFill) -> tuple:
    return (fill.symbol, fill.expiry_date, fill.strike_price, fill.option_type)


_FIFO_EPS = 1e-9  # guards against float drift on repeated subtraction, not a real trading tolerance


def _fifo_walk(fills: list[PortfolioTradeFill]) -> tuple[list[dict], list[dict]]:
    """Shared FIFO engine behind compute_realized_pnl/compute_open_lots --
    see compute_realized_pnl's own docstring for the full matching
    algorithm (grouping, same-side-opens/opposite-side-closes, flips).
    Returns (closed_lots, open_lots): closed_lots is exactly what
    compute_realized_pnl returns; open_lots is every lot fragment still
    unmatched once each group's fills are exhausted -- one dict per
    fragment (NOT aggregated), each carrying that fragment's own entry
    price/time/raw_name and signed qty (positive = long, negative =
    short) -- compute_open_lots' own return value."""
    groups: dict[tuple, list[PortfolioTradeFill]] = {}
    for fill in fills:
        groups.setdefault(_trade_fill_contract_key(fill), []).append(fill)

    closed_lots: list[dict] = []
    open_lot_rows: list[dict] = []
    for (symbol, expiry_date, strike_price, option_type), group_fills in groups.items():
        ordered = sorted(group_fills, key=lambda f: f.traded_at)
        open_lots: deque = deque()  # each: [qty_remaining(signed), price, traded_at, charge_per_unit, raw_name]
        for fill in ordered:
            charge_per_unit = (fill.taxes_and_charges + fill.brokerage) / fill.qty if fill.qty else 0.0
            remaining_qty = fill.qty if fill.transaction_type == "BUY" else -fill.qty
            while abs(remaining_qty) > _FIFO_EPS and open_lots and (open_lots[0][0] > 0) != (remaining_qty > 0):
                lot = open_lots[0]
                close_qty = min(abs(lot[0]), abs(remaining_qty))
                entry_price, entry_time, entry_charge, entry_raw_name = lot[1], lot[2], lot[3], lot[4]
                is_long_lot = lot[0] > 0
                gross_pnl = (fill.price - entry_price) * close_qty if is_long_lot else (entry_price - fill.price) * close_qty
                total_charges = (entry_charge + charge_per_unit) * close_qty
                closed_lots.append(
                    {
                        "symbol": symbol,
                        "expiry_date": expiry_date,
                        "strike_price": strike_price,
                        "option_type": option_type,
                        "raw_name": fill.raw_name,  # the closing fill's own -- see compute_realized_pnl's docstring
                        "entry_time": entry_time,
                        "exit_time": fill.traded_at,
                        "qty_closed": close_qty,
                        "entry_price": entry_price,
                        "exit_price": fill.price,
                        "gross_pnl": gross_pnl,
                        "charges": total_charges,
                        "net_pnl": gross_pnl - total_charges,
                    }
                )
                lot[0] -= close_qty if is_long_lot else -close_qty
                remaining_qty -= close_qty if remaining_qty > 0 else -close_qty
                if abs(lot[0]) < _FIFO_EPS:
                    open_lots.popleft()
            if abs(remaining_qty) > _FIFO_EPS:
                open_lots.append([remaining_qty, fill.price, fill.traded_at, charge_per_unit, fill.raw_name])
        for lot in open_lots:
            open_lot_rows.append(
                {
                    "symbol": symbol,
                    "expiry_date": expiry_date,
                    "strike_price": strike_price,
                    "option_type": option_type,
                    "raw_name": lot[4],
                    "qty": lot[0],
                    "price": lot[1],
                    "traded_at": lot[2],
                }
            )
    return closed_lots, open_lot_rows


def compute_realized_pnl(fills: list[PortfolioTradeFill]) -> list[dict]:
    """FIFO lot-matching over every synced trade fill, grouped by contract
    identity -- (symbol, expiry_date, strike_price, option_type). A plain
    equity/ETF fill has the latter three all None, which is itself a
    valid, distinct group key (one equity = one group), so stock and
    option/future fills are never cross-matched.

    Within each group, fills are walked in traded_at order keeping a FIFO
    queue of open lots. A fill on the *same* side as the current net
    position (or the group's very first fill) opens a new lot. A fill on
    the *opposite* side consumes open lots oldest-first, emitting one
    closed-lot dict per (partial or full) match -- `gross_pnl` follows the
    entry side: a long lot's close is `(exit_price - entry_price) * qty`,
    a short lot's is `(entry_price - exit_price) * qty`. If a closing
    fill's quantity exceeds every open lot's remaining quantity (a
    position *flip*, e.g. long 10 fully closed by a sell of 15), the
    excess opens a new lot in the new direction rather than being dropped
    or raising. `raw_name` on each closed-lot dict is the *closing*
    fill's own (Dhan's descriptive string, e.g. "GOLD 31 AUG 135000 PUT")
    -- for Trade History's Instrument column.

    Any quantity never closed by the end of a group's fills isn't emitted
    at all -- see compute_open_lots for that side of the same computation.
    Charges are pro-rated by qty from both the opening and closing fill's
    own taxes_and_charges (each per-unit, `fill.taxes_and_charges /
    fill.qty`, since a fill can be partially consumed across several
    closes)."""
    closed, _ = _fifo_walk(fills)
    return closed


def compute_open_lots(fills: list[PortfolioTradeFill]) -> list[dict]:
    """The FIFO leftover from the same computation compute_realized_pnl
    runs -- fill fragments never matched to a close, i.e. the actual
    trades that built up (or are still building up) each symbol's current
    open position. Feeds Trade History's Unrealised P&L section ("trades
    leading to this holding"). One row per still-open lot fragment, NOT
    aggregated -- a symbol can have several (e.g. two separate buys at
    different prices/times, neither yet sold). `qty` keeps FIFO's signed
    convention (positive = long/still held to be sold, negative =
    short/still held to be bought back); `price`/`traded_at`/`raw_name`
    are that specific fragment's own entry values, not the position's
    weighted average -- summing/averaging across a symbol's rows here is
    the caller's job if it wants an aggregate."""
    _, open_lots = _fifo_walk(fills)
    return open_lots


def apply_fallback_option_ltp(
    positions: list[dict], option_chains: dict[tuple[str, date], list[dict]]
) -> list[dict]:
    """Fills any still-missing `ltp` (e.g. a Dhan sync whose Market Quote
    call 401'd because the account lacks the separate "Data APIs"
    subscription -- see dhan_positions_from_api's docstring and
    pages/6_My_Broker.py's _sync_dhan) from this app's own F&O data:
    `option_chains` is `{(symbol, expiry_date): latest_option_chain_view
    rows}`, the same shape fo_repo.get_option_chain returns. This is the
    most recent trading day's close/settle, not a live tick -- same
    "not live" caveat this page already shows under the positions table.
    Only ever fills gaps; never overwrites an `ltp` a broker already gave.
    Positions with no matching chain entry (most commonly index options --
    NIFTY/BANKNIFTY/SENSEX aren't tracked by this app at all) are left as
    they were.

    Also sets `ltp_as_of` to the fallback row's own `trade_date` whenever
    it fills a gap -- confirmed as a real, user-visible bug: without this,
    a JioFin CSP showed LTP 3.30 on My CSP (this app's own EOD close from
    the last F&O refresh) while Dhan's own app showed a live 4.40 for the
    same contract, with no indication in this app that the number wasn't
    live. `ltp_as_of` lets the UI show "(as of <date>)" next to it, same
    convention the Dashboard already uses for a stale screener price. A
    position whose `ltp` a broker/CSV already supplied keeps
    `ltp_as_of=None` (that number is trusted as live/as-given, per this
    function's own "only ever fills gaps" rule above)."""
    lookups: dict[tuple[str, date], dict[tuple[float, str], tuple[float, date | None]]] = {}
    for key, rows in option_chains.items():
        lookups[key] = {
            (round(float(r["strike_price"]), 4), r["option_type"]): (price, r.get("trade_date"))
            for r in rows
            if (price := r.get("last_price") or r.get("close")) is not None
        }
    filled = []
    for p in positions:
        if p["ltp"] is None and p["symbol"] and p["expiry_date"] and p["option_type"] and p["strike_price"] is not None:
            lookup = lookups.get((p["symbol"], p["expiry_date"]), {})
            found = lookup.get((round(p["strike_price"], 4), p["option_type"].value))
            if found is not None:
                ltp, trade_date = found
                p = {**p, "ltp": ltp, "ltp_as_of": trade_date}
        filled.append(p)
    return filled


def assign_trade_ids(positions: list[dict], overrides: dict[tuple[str, str], str]) -> list[dict]:
    """Adds a `trade_id` to each leg -- the manual "Trade" grouping shown
    on My Trades / Analyse Trade (pages/7_My_Trades.py,
    10_Analyse_Trade.py; see group_into_trades, which calls this). Works
    identically for holding legs and position legs -- it only touches
    symbol/raw_name/broker. Default is one Trade per underlying symbol (or
    raw_name, for a still-undecoded/unresolved leg with no resolved
    symbol); `overrides` -- keyed by (broker, raw_name), from
    portfolio_repo.list_trade_groups -- wins when the user has manually
    combined this leg into, or split it out of, a Trade. Each leg dict
    must include a `broker` key."""
    result = []
    for p in positions:
        default_trade_id = p["symbol"] or p["raw_name"]
        trade_id = overrides.get((p["broker"], p["raw_name"]), default_trade_id)
        result.append({**p, "trade_id": trade_id})
    return result


def classify_underlying_bucket(symbol: str | None, company_type_by_symbol: dict[str, CompanyType]) -> str:
    """Which of the three "My Trades" tables a leg's underlying sorts
    into -- "stock" (the default -- an unknown/unclassified symbol, or a
    real `Equity`, same fallback convention pages/6_Portfolio.py's
    now-retired _is_etf_or_fund helper used for the Holdings ETF/MF
    split), "index" (company_type `Index` only), or "other" (symbol is
    `None` -- an undecoded F&O contract or an unmatched holding, nothing
    to classify by -- **or** company_type `ETF`/`Fund`, which don't
    belong in either Stock or Index Trades).

    **Two real bugs this fixed, in sequence**: ETF/Fund first got lumped
    in with Index here, so gilt/liquid/gold ETFs (BANKBEES, GILT5YBEES,
    GOLDBEES, LIQUIDCASE, ...) showed up in "Index Trades" alongside
    genuine index positions (NIFTY, FINNIFTY). Moving them to "stock"
    instead was itself wrong on a live request -- an ETF isn't a real
    equity trade either, so it belongs in Other Trades, not Stock Trades.
    An ETF someone deliberately wants shown alongside Index Trades (or
    Stock Trades) can still be pinned there via
    PortfolioTradeMeta.bucket_override (see group_into_trades) -- this
    function only decides the *default*."""
    if symbol is None:
        return "other"
    company_type = company_type_by_symbol.get(symbol)
    if company_type == CompanyType.INDEX:
        return "index"
    if company_type in (CompanyType.ETF, CompanyType.FUND):
        return "other"
    return "stock"


def is_csp_trade_type(trade_type: str) -> bool:
    """Whether a Trade's (free-text, user-editable) `trade_type` marks it
    as a Cash Secured Put -- the signal `pages/11_My_CSP.py` filters on.
    Case-insensitive and whitespace-trimmed so "CSP", "csp", " CSP " all
    match -- there's no fixed enum of trade types (see PortfolioTradeMeta),
    "CSP" is just a convention this page expects the user to type on
    Analyse Trade."""
    return trade_type.strip().lower() == "csp"


def is_portfolio_trade_type(trade_type: str) -> bool:
    """Whether a Trade's (free-text, user-editable) `trade_type` marks it
    as involving a stock holding alongside its option legs -- the signal
    `pages/12_My_Portfolio_Trades.py` (formerly My CC) filters on.
    Case-insensitive, whitespace-trimmed prefix match on `"portfolio "` --
    this is the exact convention `classify_trade_type` writes for
    Portfolio CC/Strangle/Jade Lizard/Twisted Sister/IC, but, like every
    other `is_*_trade_type` check here, it's a plain string convention,
    not a re-derivation from the trade's actual legs: a manually-typed
    custom label that also happens to carry a holding (e.g. "Hedged",
    "Batman" -- both seen on real accounts) won't match unless it's
    renamed to start with "Portfolio ". Deliberately a prefix match, not
    an exact one against a fixed list -- a hand-typed "Portfolio Collar"
    or similar it doesn't yet auto-detect still lands here."""
    return trade_type.strip().lower().startswith("portfolio ")


def classify_trade_type(legs: list[dict]) -> str | None:
    """Auto-detects a Trade's options strategy from the shape of its
    current legs -- called both to classify a brand-new trade during
    Portfolio Refresh (data_provider_settings.py::_auto_classify_new_trades)
    and, at read time, to flag an already-user-classified trade whose legs
    no longer match (group_into_trades' `trade_type_mismatch`). Each leg
    dict needs `leg_type` ("Holding"/"Position"), `option_type` (a CE/PE
    OptionType, or None for an undecoded/futures Position leg), and `qty`
    (signed: negative = short/sell, positive = long/buy) -- the same shape
    build_trade_legs/group_into_trades already produce.

    Returns None (-> caller defaults to "Trade") when the shape matches
    none of the rules below, including whenever a Position leg's
    option_type can't be read (an undecoded contract or a futures leg) --
    better to not guess than to misclassify on incomplete information.

    - **Holding**: one or more Holding legs and zero Position legs at all
      -- just a stock/ETF holding, no options (or futures) involved.
    - **CSP**: exactly one Position leg, a short (`qty < 0`) PE, and no
      Holding legs at all (a CSP is specifically *uncovered* by a stock
      position -- that's what "cash-secured" instead of "covered" means).
    - **Portfolio CC** (Covered Call): at least one Holding leg plus
      exactly one Position leg, a short CE. Always "Portfolio "-prefixed,
      unconditionally -- unlike the four below, there's no bare "CC": the
      rule itself already requires a Holding leg to fire at all, so the
      un-prefixed name would never be reachable.
    - **Strangle**: exactly one PE and one CE Position leg (two total),
      both short or both long -- a Holding leg may or may not also be
      present.
    - **Jade Lizard / Twisted Sister**: three or more Position legs, with
      exactly one long (`qty > 0`) and the rest all short. The lone long
      leg's side decides which: a bought CE -> Jade Lizard (short put +
      short call + a further-OTM long call caps upside risk); a bought PE
      -> Twisted Sister (the put-side mirror).
    - **IC** (Iron Condor): exactly four Position legs, two long and two
      short (confirmed with the user: this is a leg-count rule only --
      it doesn't separately require a specific PE/CE split, unlike
      Strangle above).

    For Strangle/Jade Lizard/Twisted Sister/IC, a Holding leg being
    present doesn't change which strategy is detected, but it does get
    flagged in the name: one of these four alongside a stock holding is
    prefixed "Portfolio " (e.g. "Portfolio Strangle", "Portfolio IC")
    since the holding changes the position's actual risk profile even
    though the option legs alone still fit the same shape. CSP never
    takes this prefix (its own rule already rules a holding out)."""
    holdings = [leg for leg in legs if leg.get("leg_type") == "Holding"]
    positions = [leg for leg in legs if leg.get("leg_type") == "Position"]
    if holdings and not positions:
        return "Holding"
    if any(leg.get("option_type") is None for leg in positions):
        return None

    pe_legs = [leg for leg in positions if leg["option_type"] == OptionType.PE]
    ce_legs = [leg for leg in positions if leg["option_type"] == OptionType.CE]
    prefix = "Portfolio " if holdings else ""

    if len(positions) == 1 and not holdings and len(pe_legs) == 1 and pe_legs[0]["qty"] < 0:
        return "CSP"

    if holdings and len(positions) == 1 and len(ce_legs) == 1 and ce_legs[0]["qty"] < 0:
        return f"{prefix}CC"

    if len(positions) == 2 and len(pe_legs) == 1 and len(ce_legs) == 1:
        if (pe_legs[0]["qty"] < 0) == (ce_legs[0]["qty"] < 0):
            return f"{prefix}Strangle"

    if len(positions) >= 3:
        long_legs = [leg for leg in positions if leg["qty"] > 0]
        short_legs = [leg for leg in positions if leg["qty"] < 0]
        if len(long_legs) == 1 and len(short_legs) == len(positions) - 1:
            return f"{prefix}Jade Lizard" if long_legs[0]["option_type"] == OptionType.CE else f"{prefix}Twisted Sister"
        if len(positions) == 4 and len(long_legs) == 2 and len(short_legs) == 2:
            return f"{prefix}IC"

    return None


def csp_breakeven_price(strike_price: float | None, avg_price: float | None) -> float | None:
    """The textbook CSP breakeven price: `strike_price - avg_price` --
    the premium collected (`avg_price`, the price the put was written
    at) reduces the effective cost basis below the strike by that much.
    `None` (-> N/A) when either input is missing."""
    if strike_price is None or avg_price is None:
        return None
    return strike_price - avg_price


def csp_breakeven_pct(breakeven_price: float | None, underlying_ltp: float | None) -> float | None:
    """My CSP's "Breakeven" column's parenthesized percentage: how far
    `breakeven_price` (see csp_breakeven_price above) sits from the
    underlying's current price -- `(breakeven_price / underlying_ltp - 1)
    * 100`. Negative means breakeven is *below* the current price (there's
    cushion -- the underlying would need to fall by this % before the
    position starts losing money past the premium collected); positive
    means breakeven is already above the current price. `None` (-> N/A)
    when either input is missing, or `underlying_ltp` is 0 (division by
    zero -- shouldn't happen for a real quote, but a leg with no resolved
    symbol has no LTP to begin with)."""
    if breakeven_price is None or underlying_ltp is None or underlying_ltp == 0:
        return None
    return (breakeven_price / underlying_ltp - 1) * 100


def csp_max_credit(avg_price: float | None, qty: float | None) -> float | None:
    """Total premium collected for writing a CSP leg -- `avg_price *
    abs(qty)`. `abs()` because `qty` is signed (negative for a short
    position, this app's convention -- see PortfolioPosition), but
    "credit received" is inherently a positive amount. `None` when
    either input is missing."""
    if avg_price is None or qty is None:
        return None
    return avg_price * abs(qty)


def csp_target_pnl(
    max_credit: float | None, trade_date: date | None, expiry_date: date | None, as_of: date | None = None
) -> float | None:
    """My CSP's "Target P&L" -- `max(0.5 * max_credit, min(0.95 *
    max_credit, max_credit * (duration_held / duration_to_expiry) *
    1.2))`. The inner term is a time-decay target that runs 20% *faster*
    than plain linear (theta decay tends to accelerate as expiry nears,
    so this reflects a "higher than average decay" expectation, not a
    straight-line one) -- it changes every day as `duration_held` grows.
    The `0.95 * max_credit` cap means chasing the last 5% of premium
    isn't worth the assignment/gamma risk of holding to the very end, so
    the target never crosses 95% of max credit no matter how long the
    position is held, even well past expiry. The `0.5 * max_credit`
    floor means the target never drops below half the max credit either
    -- even early in the trade (when the time-decay term is still
    small), the target holds at 50% rather than sinking toward 0. `as_of`
    defaults to `date.today()`, explicit for testability, same
    convention `screener_service`/`refresh_service` already use. `None`
    when `max_credit`/`trade_date`/`expiry_date` is missing, or the
    duration to expiry isn't positive (expiry on or before the trade
    date -- division by zero, or nonsensical for a same-day/already-past
    expiry)."""
    if max_credit is None or trade_date is None or expiry_date is None:
        return None
    duration_to_expiry = (expiry_date - trade_date).days
    if duration_to_expiry <= 0:
        return None
    duration_held = ((as_of or date.today()) - trade_date).days
    accelerated_target = max_credit * (duration_held / duration_to_expiry) * 1.2
    capped_target = min(max_credit * 0.95, accelerated_target)
    return max(max_credit * 0.5, capped_target)


def csp_stop_loss(existing_stop_loss: float | None, max_credit: float | None, pnl_pct: float | None) -> float | None:
    """My CSP's "Stop Loss" -- a ratchet that only ever tightens (moves
    up), computed fresh on every render and saved back
    (`portfolio_repo.set_position_stop_loss`) so the next render's
    `existing_stop_loss` reflects it:
    - No `existing_stop_loss` yet (nothing saved for this leg before --
      a "new trade") -> `-max_credit`: willing to give back the entire
      premium collected before stopping out.
    - `pnl_pct` negative -> unchanged (never tighten while the position
      is already underwater).
    - `25 <= pnl_pct < 50` -> `max(existing_stop_loss, 0)` -- move the
      stop to breakeven once a quarter of the max credit is captured.
    - `pnl_pct >= 50` -> `max(existing_stop_loss, 0.5 * max_credit)` --
      lock in at least half the max credit once captured.
    - `0 <= pnl_pct < 25` -> unchanged (no rule specified for this band).
    `None` (nothing to save) when `max_credit` is missing -- there's
    nothing to compute a ratchet against."""
    if max_credit is None:
        return None
    if existing_stop_loss is None:
        return -max_credit
    if pnl_pct is None or pnl_pct < 0:
        return existing_stop_loss
    if pnl_pct >= 50:
        return max(existing_stop_loss, 0.5 * max_credit)
    if pnl_pct >= 25:
        return max(existing_stop_loss, 0.0)
    return existing_stop_loss


def classify_position_bucket(
    option_type: OptionType | None, symbol: str | None, company_type_by_symbol: dict[str, CompanyType]
) -> str:
    """Which of My Positions' three tables (Stock Options / Index Options
    / Others) a position leg sorts into. "other" covers everything that
    isn't a decoded option contract -- an undecoded F&O row, a futures
    position, or a stock/ETF bought/sold as a position rather than a
    holding -- since `option_type` and `symbol` are only ever set together
    (dhan_positions_from_api takes both fields from the same
    decode-or-nothing result). A decoded option's
    underlying then splits "stock" vs "index" the same way
    classify_underlying_bucket does for My Trades (company_type Index
    only -> "index"; ETF/Fund/everything else -> "stock")."""
    if option_type is None:
        return "other"
    if company_type_by_symbol.get(symbol) == CompanyType.INDEX:
        return "index"
    return "stock"


def group_into_trades(
    legs: list[dict],
    overrides: dict[tuple[str, str], str],
    trade_meta: dict[str, dict],
    company_type_by_symbol: dict[str, CompanyType],
) -> list[dict]:
    """Groups holding + position legs into "Trades" for the My Trades /
    Analyse Trade pages. Each leg dict must already carry `leg_type`
    ("Holding" or "Position", added by the caller before grouping) plus
    the usual symbol/raw_name/broker/pnl keys (holding legs priced via
    compute_portfolio_view, position legs via compute_positions_view --
    both already work on unmerged per-broker rows, which is what "Trades"
    needs: leg-level identity, not merge_holdings' cross-broker merge).

    `trade_id` assignment reuses assign_trade_ids unchanged -- it only
    touches symbol/raw_name/broker, already agnostic to leg_type. Returns
    one dict per trade: `trade_id`, `legs` (the leg dicts, each now also
    carrying its assigned trade_id), `bucket` ("stock"/"index"/"other" --
    trade_meta's `bucket_override` if set (the user manually pinned this
    trade to a table -- e.g. an ETF they want shown alongside genuine
    Index Trades), else unanimous across the trade's own legs'
    classify_underlying_bucket, else "other" when the legs' computed
    buckets disagree, since a trade mixing stock and index underlyings
    doesn't cleanly belong to either specific table),
    `default_underlying_label` (sorted, " + "-joined distinct
    symbol-or-raw_name across the trade's legs), `underlying_label`
    (trade_meta's override if set, else the default -- see
    PortfolioTradeMeta for why this is free text, not constrained to a
    known symbol), `trade_type` (trade_meta's override if set, else
    "Trade"), `trade_type_mismatch` (True only when a trade_meta row
    exists -- the user has classified this trade before, even if that
    classification happens to be the literal string "Trade" -- *and*
    classify_trade_type's read of the trade's current legs disagrees with
    the saved `trade_type`; a brand-new/never-touched trade, or one whose
    legs don't match any known strategy shape at all, is never flagged),
    `leg_count`, `total_pnl` (sum over legs with a known pnl; None if none
    are priced), and `option_pnl` (same sum, but Position legs only,
    excluding any Holding leg's own pnl -- None if no Position leg is
    priced; added for My Portfolio Trades' "Option P&L" column, which
    needs the option legs' contribution on its own, separate from the
    stock holding's)."""
    assigned = assign_trade_ids(legs, overrides)
    trades: dict[str, list[dict]] = {}
    order: list[str] = []
    for leg in assigned:
        trade_id = leg["trade_id"]
        if trade_id not in trades:
            trades[trade_id] = []
            order.append(trade_id)
        trades[trade_id].append(leg)

    result = []
    for trade_id in order:
        trade_legs = trades[trade_id]
        meta = trade_meta.get(trade_id)
        bucket_override = meta.get("bucket_override") if meta else None
        if bucket_override:
            bucket = bucket_override
        else:
            buckets = {classify_underlying_bucket(leg["symbol"], company_type_by_symbol) for leg in trade_legs}
            bucket = next(iter(buckets)) if len(buckets) == 1 else "other"
        default_label = " + ".join(sorted({leg["symbol"] or leg["raw_name"] for leg in trade_legs}))
        underlying_label = (meta.get("underlying_label") if meta else None) or default_label
        trade_type = (meta.get("trade_type") if meta else None) or "Trade"
        detected_type = classify_trade_type(trade_legs)
        trade_type_mismatch = bool(
            meta is not None
            and detected_type is not None
            and detected_type.strip().lower() != trade_type.strip().lower()
        )
        priced = [leg for leg in trade_legs if leg.get("pnl") is not None]
        total_pnl = sum(leg["pnl"] for leg in priced) if priced else None
        priced_positions = [leg for leg in priced if leg["leg_type"] == "Position"]
        option_pnl = sum(leg["pnl"] for leg in priced_positions) if priced_positions else None
        result.append(
            {
                "trade_id": trade_id,
                "legs": trade_legs,
                "bucket": bucket,
                "default_underlying_label": default_label,
                "underlying_label": underlying_label,
                "trade_type": trade_type,
                "trade_type_mismatch": trade_type_mismatch,
                "leg_count": len(trade_legs),
                "total_pnl": total_pnl,
                "option_pnl": option_pnl,
            }
        )
    return result


def trade_type_by_symbol(
    holding_legs: list[dict],
    position_legs: list[dict],
    overrides: dict[tuple[str, str], str],
    trade_meta: dict[str, dict],
) -> dict[str, str]:
    """Every underlying `symbol` this account currently has **any** Trade
    on, mapped to that Trade's own `trade_type`, for **one** portfolio's
    holdings/positions (same per-portfolio scoping `group_into_trades`
    itself always uses, `trade_id` collisions aside) -- the Screener for
    CSP page's (`pages/1_Dashboard.py`) "Trade Taken" column, so a stock
    already carrying a position isn't mistaken for a fresh CSP
    opportunity without saying what's already on it. The caller
    (`portfolio_page.load_trade_types_by_symbol`) merges this across
    every portfolio the account has, since this is account-wide, not
    scoped to one portfolio name.

    Every Trade type counts, not just CSP -- a plain stock-only Holding
    Trade shows up too (as `"Holding"`, or `"Trade"` if it hasn't been
    auto-classified yet), since holding the stock outright is itself
    relevant context for a wheel-strategy decision, not only an existing
    options trade against it. A symbol absent from the returned dict
    means no Trade touches it at all -- the caller shows a blank cell for
    that case, never a literal `"None"`.

    If the same symbol appears across more than one Trade for this
    portfolio (rare -- only possible via a manual split in Analyse
    Trade), every distinct `trade_type` found is joined with `" + "`,
    sorted, same convention `group_into_trades`' own
    `default_underlying_label` already uses for multiple symbols on one
    Trade.

    Deliberately takes plain leg dicts, not `PortfolioHolding`/
    `PortfolioPosition` model instances or `build_trade_legs`'
    live-priced output -- this only needs `leg_type`/`symbol`/
    `raw_name`/`broker`/`qty`/`option_type` to group and classify, the
    same lightweight shape `data_provider_settings.py`'s
    `_auto_classify_new_trades` already builds for the same reason (no
    live LTP fetch needed just to answer this). `overrides`/`trade_meta`
    are passed straight through to `group_into_trades`, same shape as
    every other call site. `company_type_by_symbol` is passed as `{}`
    since bucket (stock/index/other) is irrelevant to this check."""
    trades = group_into_trades(holding_legs + position_legs, overrides, trade_meta, company_type_by_symbol={})
    types_by_symbol: dict[str, set[str]] = {}
    for t in trades:
        for leg in t["legs"]:
            symbol = leg.get("symbol")
            if symbol:
                types_by_symbol.setdefault(symbol, set()).add(t["trade_type"])
    return {symbol: " + ".join(sorted(types)) for symbol, types in types_by_symbol.items()}


def compute_positions_view(positions: list[dict]) -> list[dict]:
    """Adds pnl/pnl_pct to each position, recomputed from qty/avg_price/
    ltp rather than trusted from Dhan's own response (its raw P&L-ish
    fields aren't reliably direction-aware, so recomputing here is
    trustworthy regardless). pnl = (ltp - avg_price) * qty, which is direction-
    correct for both long (positive qty) and short (negative qty)
    positions. pnl_pct is against the premium notional (avg_price *
    |qty|), not stock notional -- there's no equivalent of a holding's
    "investment" for a written option. Both are None when ltp is missing
    (no live-like price to compare against)."""
    rows = []
    for p in positions:
        ltp = p["ltp"]
        pnl = (ltp - p["avg_price"]) * p["qty"] if ltp is not None else None
        notional = p["avg_price"] * abs(p["qty"])
        pnl_pct = (pnl / notional * 100) if pnl is not None and notional else None
        rows.append({**p, "pnl": pnl, "pnl_pct": pnl_pct})
    return rows


def positions_to_records(
    user_id: str, portfolio_name: str, broker: str, positions: list[dict]
) -> list[PortfolioPosition]:
    """Converts parsed position dicts into PortfolioPosition rows ready
    for portfolio_repo.replace_broker_positions. `ltp_as_of` is read via
    `.get()`, not `[...]` -- only apply_fallback_option_ltp's output ever
    carries that key; every other position-parsing path never sets it at
    all, meaning "this LTP is live/as-given"."""
    return [
        PortfolioPosition(
            user_id=user_id,
            portfolio_name=portfolio_name,
            broker=broker,
            raw_name=p["raw_name"],
            symbol=p["symbol"],
            expiry_date=p["expiry_date"],
            strike_price=p["strike_price"],
            option_type=p["option_type"],
            qty=p["qty"],
            avg_price=p["avg_price"],
            ltp=p["ltp"],
            ltp_as_of=p.get("ltp_as_of"),
        )
        for p in positions
    ]


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
    """Pure diff used by the refresh pipeline (scripts/run_refresh.py,
    scripts/fetch_fo_data.py): which portfolio-only symbols need a
    minimal companies row registered before they can be fetched/priced.
    Never touches nifty50_constituents -- these never become an official
    constituent -- but (since migration 0013) they DO still show up on
    the Dashboard's own screener for the tracking user, via
    latest_screener_view's own portfolio_holdings widening. Returns
    `company_type=Equity` (the model default) for every row; the caller is
    responsible for classifying real ETFs/funds via
    looks_like_etf_name() before upserting -- this stays a pure,
    network-free diff on purpose (see its own unit tests)."""
    new_symbols = sorted(set(portfolio_symbols) - known_company_symbols)
    return [Company(symbol=symbol, name=raw_name_by_symbol.get(symbol, symbol)) for symbol in new_symbols]


def looks_like_etf_name(name: str) -> bool:
    """Classifies a symbol as an ETF/fund from its *real* display name --
    e.g. yfinance's `longName`/`shortName`, not `companies.name` for a
    portfolio-only symbol, which is often just the raw ticker itself with
    no real name attached (Dhan's own `tradingSymbol` is the exact NSE
    symbol, so `raw_name == symbol` for every Dhan-sourced holding --
    see dhan_holdings_from_api above).

    Deliberately NOT based on yfinance's own `quoteType` field: checked
    live against every ETF/fund this app currently tracks (NIFTYBEES,
    GILT5YBEES, LIQUIDCASE, LTGILTCASE) and Yahoo returns `"EQUITY"` for
    every single one of them -- a real data-quality quirk for
    Indian-listed ETFs, not a bug on this app's side. The real display
    name is a much more reliable signal: all four literally contain
    "ETF" (e.g. "Nippon India ETF Nifty 50 BeES", "Zerodha Nifty 1D Rate
    Liquid ETF"), and no real NSE stock this app tracks has "ETF"
    anywhere in its name."""
    return "etf" in name.lower()


def holdings_to_records(user_id: str, portfolio_name: str, broker: str, holdings: list[dict]) -> list[PortfolioHolding]:
    """Converts parsed/merged holding dicts into PortfolioHolding rows
    ready for portfolio_repo.replace_broker_holdings."""
    return [
        PortfolioHolding(
            user_id=user_id,
            portfolio_name=portfolio_name,
            broker=broker,
            raw_name=h["raw_name"],
            symbol=h["symbol"],
            qty=h["qty"],
            avg_price=h["avg_price"],
            investment=h["investment"],
        )
        for h in holdings
    ]
