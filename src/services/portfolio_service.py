"""Broker CSV parsing, symbol matching, and valuation for the Portfolio
feature's pages (pages/6_My_Broker.py, 7_My_Trades.py, 8_My_Holdings.py,
9_My_Positions.py, 10_Analyse_Trade.py). Holdings and positions are plain dicts
throughout (not a dataclass) -- same convention as fo_service's row-dict
outputs. Holdings keys: raw_name, symbol (str | None), qty, avg_price,
investment. Positions keys: raw_name, symbol (str | None, the underlying),
expiry_date (date | None), strike_price (float | None), option_type
(OptionType | None), qty (signed -- negative is short), avg_price, ltp.
"""
from __future__ import annotations

import re
from datetime import date

import pandas as pd

from src.models.company import Company
from src.models.enums import CompanyType, OptionType
from src.models.portfolio import PortfolioHolding, PortfolioPosition


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


_MONTH_ABBR = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_WEEKLY_MONTH_CHARS = {str(m): m for m in range(1, 10)} | {"O": 10, "N": 11, "D": 12}

# Zerodha's own NSE-derived tradingsymbol for a *weekly* option contract --
# only indices (NIFTY, BANKNIFTY, SENSEX, ...) currently have weekly
# expiries -- encodes the full expiry inline: 2-digit year, then a single
# month character (1-9, or O/N/D for Oct/Nov/Dec), then 2-digit day.
# e.g. "NIFTY2681123000PE" = NIFTY, 2026, month 8 (Aug), day 11, strike
# 23000, PE. There's no sample of Zerodha's *monthly* stock-option format
# (e.g. "SBIN25AUG970PE") to work from, so that's deliberately left
# unparsed below rather than guessed at (monthly expiry falls on the last
# Thursday of the month, which shifts for exchange holidays -- not safe to
# infer from the symbol alone without a real template).
_ZERODHA_WEEKLY_OPTION_RE = re.compile(
    r"^(?P<symbol>[A-Z]+)(?P<yy>\d{2})(?P<month>[1-9OND])(?P<dd>\d{2})"
    r"(?P<strike>\d+(?:\.\d+)?)(?P<type>CE|PE)$"
)


def parse_zerodha_option_instrument(instrument: str) -> dict | None:
    """Decodes a Zerodha F&O tradingsymbol into its underlying/expiry/
    strike/type. Returns None for anything that isn't a weekly option in
    the format above (futures, monthly stock options, or malformed
    input) -- callers keep the row with symbol=None rather than dropping
    it."""
    m = _ZERODHA_WEEKLY_OPTION_RE.match(instrument.strip().upper())
    if not m:
        return None
    year = 2000 + int(m.group("yy"))
    month = _WEEKLY_MONTH_CHARS[m.group("month")]
    day = int(m.group("dd"))
    try:
        expiry_date = date(year, month, day)
    except ValueError:
        return None
    return {
        "symbol": m.group("symbol"),
        "expiry_date": expiry_date,
        "strike_price": float(m.group("strike")),
        "option_type": OptionType(m.group("type")),
    }


def parse_zerodha_positions_csv(file) -> list[dict]:
    """Zerodha's positions export -- `Instrument` is the exact NSE
    tradingsymbol (decoded via parse_zerodha_option_instrument above).
    `Qty.` keeps its sign (short positions are negative). The file's own
    LTP is trusted (there's no live per-contract price source for index
    options -- see nse_fo_provider's IDF/IDO exclusion); P&L is always
    recomputed from qty/avg_price/ltp rather than trusting the file's own
    P&L/Chg. columns (see compute_positions_view)."""
    df = pd.read_csv(file)
    positions = []
    for _, row in df.iterrows():
        instrument = row.get("Instrument")
        if pd.isna(instrument) or not str(instrument).strip():
            continue
        raw_name = str(instrument).strip()
        qty = _to_float(row.get("Qty."))
        avg_price = _to_float(row.get("Avg."))
        ltp = _to_float(row.get("LTP"))
        if qty is None or avg_price is None:
            continue
        decoded = parse_zerodha_option_instrument(raw_name) or {}
        positions.append(
            {
                "raw_name": raw_name,
                "symbol": decoded.get("symbol"),
                "expiry_date": decoded.get("expiry_date"),
                "strike_price": decoded.get("strike_price"),
                "option_type": decoded.get("option_type"),
                "qty": qty,
                "avg_price": avg_price,
                "ltp": ltp,
            }
        )
    return positions


_DHAN_POSITION_NAME_RE = re.compile(
    r"^(?P<symbol>[A-Z]+)\s+(?P<dd>\d{1,2})\s+(?P<mmm>[A-Z]{3})\s+"
    r"(?P<strike>\d+(?:\.\d+)?)\s+(?P<type>CALL|PUT)$"
)


def parse_dhan_position_name(raw_name: str, as_of: date | None = None) -> dict | None:
    """Decodes Dhan's positions "Name" format -- "<SYMBOL> <DD> <MMM>
    <STRIKE> <CALL|PUT>", e.g. "ONGC 25 AUG 230 PUT" -- used uniformly for
    both monthly stock options and weekly index options; unlike Zerodha's
    tradingsymbol it carries no year, so the year is inferred as the
    nearest DD-MMM on or after `as_of` (an always-open position's expiry
    can't be in the past). Returns None for anything that doesn't match
    (futures rows, malformed input)."""
    m = _DHAN_POSITION_NAME_RE.match(raw_name.strip().upper())
    if not m:
        return None
    month = _MONTH_ABBR.get(m.group("mmm"))
    if month is None:
        return None
    day = int(m.group("dd"))
    reference = as_of or date.today()
    try:
        expiry_date = date(reference.year, month, day)
    except ValueError:
        return None
    if expiry_date < reference:
        try:
            expiry_date = date(reference.year + 1, month, day)
        except ValueError:
            return None
    return {
        "symbol": m.group("symbol"),
        "expiry_date": expiry_date,
        "strike_price": float(m.group("strike")),
        "option_type": OptionType.CE if m.group("type") == "CALL" else OptionType.PE,
    }


def parse_dhan_positions_csv(file, as_of: date | None = None) -> list[dict]:
    """Dhan's positions export -- `Name` is decoded via
    parse_dhan_position_name above (no separate company-name matching
    needed, unlike parse_dhan_csv for holdings -- Dhan's positions export
    already embeds the exact NSE symbol). `Qty` keeps its sign. Numbers
    are quoted with Indian-style grouping, same as the holdings export."""
    df = pd.read_csv(file)
    positions = []
    for _, row in df.iterrows():
        name = row.get("Name")
        if pd.isna(name) or not str(name).strip():
            continue
        raw_name = str(name).strip()
        qty = _to_float(row.get("Qty"))
        avg_price = _to_float(row.get("Avg Price"))
        ltp = _to_float(row.get("LTP"))
        if qty is None or avg_price is None:
            continue
        decoded = parse_dhan_position_name(raw_name, as_of) or {}
        positions.append(
            {
                "raw_name": raw_name,
                "symbol": decoded.get("symbol"),
                "expiry_date": decoded.get("expiry_date"),
                "strike_price": decoded.get("strike_price"),
                "option_type": decoded.get("option_type"),
                "qty": qty,
                "avg_price": avg_price,
                "ltp": ltp,
            }
        )
    return positions


def dhan_holdings_from_api(rows: list[dict]) -> list[dict]:
    """Translates GET /v2/holdings rows (src/data_providers/dhan_provider.py's
    get_holdings()) into the same holding-dict shape parse_zerodha_csv/
    parse_dhan_csv produce, so holdings_to_records/merge_holdings/
    compute_portfolio_view are reused unchanged regardless of source.
    `tradingSymbol` is already the exact NSE symbol -- no match_symbol()
    fuzzy matching needed here, unlike the Dhan CSV export's human company
    name. Skips rows with no quantity (a holding fully sold off today)."""
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


_DHAN_DERIVATIVE_SYMBOL_RE = re.compile(r"^([A-Z]+)")


def _dhan_underlying_symbol(trading_symbol: str) -> str | None:
    """Best-effort underlying extraction from Dhan's derivative
    `tradingSymbol` -- takes the leading alphabetic run (e.g. "NIFTY" out
    of whatever expiry/strike/type suffix Dhan appends). Dhan's docs don't
    show a sample derivative tradingSymbol value, so this is confirmed and
    adjusted against a real authenticated response during testing rather
    than a fixed, verified format."""
    m = _DHAN_DERIVATIVE_SYMBOL_RE.match(trading_symbol.strip().upper())
    return m.group(1) if m else None


# Confirmed against a real GET /v2/positions response: drvOptionType comes
# back as the full word ("PUT"/"CALL"), not the CE/PE code Dhan uses
# elsewhere (e.g. option_contracts.option_type, or the Dhan CSV export's
# own "CALL"/"PUT" -- see parse_dhan_position_name) -- both spellings are
# accepted here for safety.
_DHAN_OPTION_TYPES = {"PUT": OptionType.PE, "PE": OptionType.PE, "CALL": OptionType.CE, "CE": OptionType.CE}


def dhan_positions_from_api(rows: list[dict], ltp_by_security_id: dict[str, float]) -> list[dict]:
    """Translates GET /v2/positions rows into the same position-dict shape
    the CSV parsers produce. Unlike the CSV path, expiry/strike/type come
    straight from Dhan's own drvExpiryDate/drvStrikePrice/drvOptionType --
    no regex instrument-name decoding needed. `netQty` is already signed
    (positive long, negative short), matching this app's convention. `ltp`
    comes from a separate Market Quote call (dhan_provider.get_ltp_by_security_id)
    since the positions payload itself carries no live price. Skips closed
    (netQty == 0) rows -- Dhan still lists those for the trading day."""
    positions = []
    for row in rows:
        qty = row.get("netQty") or 0
        if not qty:
            continue
        trading_symbol = str(row.get("tradingSymbol") or "").strip()
        security_id = str(row.get("securityId") or "")
        cost_price = row.get("costPrice")
        if cost_price:
            avg_price = float(cost_price)
        elif qty > 0:
            avg_price = float(row.get("buyAvg") or 0)
        else:
            avg_price = float(row.get("sellAvg") or 0)
        expiry_raw = row.get("drvExpiryDate")
        option_type_raw = str(row.get("drvOptionType") or "").strip().upper()
        strike_raw = row.get("drvStrikePrice")
        positions.append(
            {
                "raw_name": trading_symbol or security_id,
                "symbol": _dhan_underlying_symbol(trading_symbol) if trading_symbol else None,
                "expiry_date": date.fromisoformat(str(expiry_raw)[:10]) if expiry_raw else None,
                "strike_price": float(strike_raw) if strike_raw else None,
                "option_type": _DHAN_OPTION_TYPES.get(option_type_raw),
                "qty": float(qty),
                "avg_price": avg_price,
                "ltp": ltp_by_security_id.get(security_id),
            }
        )
    return positions


def zerodha_holdings_from_api(rows: list[dict]) -> list[dict]:
    """Translates GET /portfolio/holdings rows (src/data_providers/
    zerodha_provider.py's get_holdings()) into the same holding-dict shape
    parse_zerodha_csv/parse_dhan_csv produce, so holdings_to_records/
    merge_holdings/compute_portfolio_view are reused unchanged regardless
    of source. `tradingsymbol` is already the exact NSE trading symbol --
    same as the CSV export's own `Instrument` column -- so it's trusted
    directly, no match_symbol() fuzzy matching needed. Skips rows with no
    quantity (a holding fully sold off today).

    **A real bug this fixed**: Kite's own `quantity` field is the *free*
    (non-pledged) quantity only -- confirmed live against a real account
    with several holdings pledged as margin (GILT5YBEES, LIQUIDCASE,
    LTGILTCASE, NIFTYBEES): each came back `quantity: 0` (matching Kite's
    own web UI, which shows "Qty. 0" plus a separate "P: <pledged qty>"
    badge for these), even though they're still fully owned, still have a
    real `average_price`, and still show real Invested/Cur. val/P&L in
    Kite's own UI. Treating `quantity` alone as "the" quantity silently
    dropped every fully-pledged holding entirely (`qty == 0` -> skipped).
    Summing `quantity + t1_quantity + collateral_quantity` reconstructs
    the true total owned quantity -- verified against the same live
    account: `average_price * (this sum)` matches Kite's own displayed
    "Invested" amount exactly for a fully-pledged holding (e.g.
    GILT5YBEES: avg 64.30 * 7500 = 4,82,250.00, matching to the rupee)."""
    holdings = []
    for row in rows:
        qty = (row.get("quantity") or 0) + (row.get("t1_quantity") or 0) + (row.get("collateral_quantity") or 0)
        symbol = str(row.get("tradingsymbol") or "").strip().upper()
        if not qty or not symbol:
            continue
        avg_price = float(row.get("average_price") or 0)
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


def zerodha_positions_from_api(rows: list[dict]) -> list[dict]:
    """Translates GET /portfolio/positions (`net`) rows into the same
    position-dict shape parse_zerodha_positions_csv produces. Kite
    Connect's own `tradingsymbol` is in the *exact same format* as the
    CSV positions export's `Instrument` column, so the existing
    parse_zerodha_option_instrument decoder is reused as-is -- no new
    regex needed here, unlike Dhan, whose API and CSV paths needed
    separate decoders. `quantity` is already signed (positive long,
    negative short), matching this app's convention. `last_price` comes
    straight from the row -- unlike Dhan, whose positions response omits
    LTP without a separate "Data APIs" subscription, Kite's response
    already includes it, so no fallback-LTP step is needed for this
    broker. Skips closed (quantity == 0) rows -- Kite still lists those
    for the trading day."""
    positions = []
    for row in rows:
        qty = row.get("quantity") or 0
        if not qty:
            continue
        raw_name = str(row.get("tradingsymbol") or "").strip()
        avg_price = float(row.get("average_price") or 0)
        ltp = row.get("last_price")
        decoded = parse_zerodha_option_instrument(raw_name) or {}
        positions.append(
            {
                "raw_name": raw_name,
                "symbol": decoded.get("symbol"),
                "expiry_date": decoded.get("expiry_date"),
                "strike_price": decoded.get("strike_price"),
                "option_type": decoded.get("option_type"),
                "qty": float(qty),
                "avg_price": avg_price,
                "ltp": float(ltp) if ltp is not None else None,
            }
        )
    return positions


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
    they were."""
    lookups: dict[tuple[str, date], dict[tuple[float, str], float]] = {}
    for key, rows in option_chains.items():
        lookups[key] = {
            (round(float(r["strike_price"]), 4), r["option_type"]): price
            for r in rows
            if (price := r.get("last_price") or r.get("close")) is not None
        }
    filled = []
    for p in positions:
        if p["ltp"] is None and p["symbol"] and p["expiry_date"] and p["option_type"] and p["strike_price"] is not None:
            lookup = lookups.get((p["symbol"], p["expiry_date"]), {})
            ltp = lookup.get((round(p["strike_price"], 4), p["option_type"].value))
            if ltp is not None:
                p = {**p, "ltp": ltp}
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
    into -- "stock" (the default -- includes an unknown/unclassified
    symbol, plus ETF/Fund, same fallback convention pages/6_Portfolio.py's
    now-retired _is_etf_or_fund helper used for the Holdings ETF/MF split),
    "index" (company_type Index only), or "other" (symbol is None -- an
    undecoded F&O contract or an unmatched holding, nothing to classify
    by).

    **A real bug this fixed**: ETF/Fund used to be lumped in with Index
    here, so gilt/liquid/gold ETFs (BANKBEES, GILT5YBEES, GOLDBEES,
    LIQUIDCASE, ...) showed up in "Index Trades" alongside genuine index
    positions (NIFTY, FINNIFTY) -- confirmed live against a real
    portfolio. An ETF someone deliberately wants shown alongside Index
    Trades still can be, via PortfolioTradeMeta.bucket_override (see
    group_into_trades) -- this function only decides the *default*."""
    if symbol is None:
        return "other"
    if company_type_by_symbol.get(symbol) == CompanyType.INDEX:
        return "index"
    return "stock"


def classify_position_bucket(
    option_type: OptionType | None, symbol: str | None, company_type_by_symbol: dict[str, CompanyType]
) -> str:
    """Which of My Positions' three tables (Stock Options / Index Options
    / Others) a position leg sorts into. "other" covers everything that
    isn't a decoded option contract -- an undecoded F&O row, a futures
    position, or a stock/ETF bought/sold as a position rather than a
    holding -- since `option_type` and `symbol` are only ever set together
    (parse_zerodha_option_instrument/parse_dhan_position_name/
    dhan_positions_from_api/zerodha_positions_from_api all take both
    fields from the same decode-or-nothing result). A decoded option's
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
    "Trade"), `leg_count`, and `total_pnl` (sum over legs with a known
    pnl; None if none are priced)."""
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
        priced = [leg for leg in trade_legs if leg.get("pnl") is not None]
        total_pnl = sum(leg["pnl"] for leg in priced) if priced else None
        result.append(
            {
                "trade_id": trade_id,
                "legs": trade_legs,
                "bucket": bucket,
                "default_underlying_label": default_label,
                "underlying_label": underlying_label,
                "trade_type": trade_type,
                "leg_count": len(trade_legs),
                "total_pnl": total_pnl,
            }
        )
    return result


def compute_positions_view(positions: list[dict]) -> list[dict]:
    """Adds pnl/pnl_pct to each position, recomputed from qty/avg_price/
    ltp rather than trusted from the file (the two sample broker exports
    disagree on what their own "P&L %"/"Chg." columns even mean -- Dhan's
    is direction-aware, Zerodha's is a raw price change -- so neither is
    trustworthy as-is). pnl = (ltp - avg_price) * qty, which is direction-
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
    for portfolio_repo.replace_broker_positions."""
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
    no real name attached (Zerodha's CSV export uses the exact NSE symbol
    as its own "Instrument" field, so `raw_name == symbol` for every
    Zerodha-sourced holding -- see parse_zerodha_csv() above).

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
