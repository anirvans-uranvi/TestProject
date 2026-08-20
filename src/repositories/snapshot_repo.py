from __future__ import annotations

from datetime import date

from supabase import Client

from src.models.screener import DailyScreenerSnapshot, ScreenerRow


def upsert_daily_snapshot(client: Client, snapshot: DailyScreenerSnapshot) -> None:
    payload = snapshot.model_dump(mode="json", exclude_none=True)
    payload["data_quality"] = snapshot.data_quality.model_dump(mode="json")
    client.table("daily_screener_snapshots").upsert(payload, on_conflict="symbol,snapshot_date").execute()


def get_latest_screener(client: Client) -> list[ScreenerRow]:
    """Reads latest_screener_view -- one joined row per current constituent."""
    resp = client.table("latest_screener_view").select("*").execute()
    return [ScreenerRow.model_validate(r) for r in (resp.data or [])]


def get_latest_screener_row(client: Client, symbol: str) -> ScreenerRow | None:
    resp = client.table("latest_screener_view").select("*").eq("symbol", symbol).limit(1).execute()
    rows = resp.data or []
    return ScreenerRow.model_validate(rows[0]) if rows else None


def get_latest_prices(client: Client, symbols: list[str]) -> dict[str, float]:
    """Latest `latest_price` per symbol, queried directly against
    daily_screener_snapshots rather than latest_screener_view -- the view
    inner-joins nifty50_constituents.is_current, which would silently
    drop any portfolio-only symbol (an ETF/fund or non-Nifty50 stock)
    even after the refresh pipeline has registered and priced it."""
    if not symbols:
        return {}
    resp = (
        client.table("daily_screener_snapshots")
        .select("symbol, latest_price")
        .in_("symbol", symbols)
        .order("snapshot_date", desc=True)
        .execute()
    )
    prices: dict[str, float] = {}
    for row in resp.data or []:
        symbol = row["symbol"]
        if symbol not in prices and row.get("latest_price") is not None:
            prices[symbol] = float(row["latest_price"])
    return prices


def get_latest_returns_and_pe(client: Client, symbols: list[str]) -> dict[str, dict]:
    """Latest return_1d/return_5d/return_20d/pe_ratio per symbol, queried
    directly against daily_screener_snapshots for the same reason as
    get_latest_prices above (latest_screener_view's inner join on
    nifty50_constituents.is_current would silently drop portfolio-only
    ETFs/funds). Each symbol's dict is taken from its single most recent
    snapshot row as-is (no cross-row carry-forward for a field that's null
    in that row) -- same convention Stock Detail's PE display already
    uses via get_latest_screener_row."""
    if not symbols:
        return {}
    resp = (
        client.table("daily_screener_snapshots")
        .select("symbol, snapshot_date, return_1d, return_5d, return_20d, pe_ratio")
        .in_("symbol", symbols)
        .order("snapshot_date", desc=True)
        .execute()
    )
    result: dict[str, dict] = {}
    for row in resp.data or []:
        symbol = row["symbol"]
        if symbol not in result:
            result[symbol] = {
                "return_1d": row.get("return_1d"),
                "return_5d": row.get("return_5d"),
                "return_20d": row.get("return_20d"),
                "pe_ratio": row.get("pe_ratio"),
            }
    return result


def get_user_live_prices(client: Client, user_id: str, symbols: list[str]) -> dict[str, float]:
    """This account's own cached live LTPs (user_live_prices, migration
    0030) for the given symbols -- written by the "Market Data Refresh"
    button only when the account's Data Provider setting is Dhan/Zerodha
    (src/utils/refresh_bar.py). Callers merge this over get_latest_prices'
    daily_screener_snapshots value (`{**shared, **live}`) so a symbol this
    account hasn't live-priced yet still falls back to the shared value --
    same pattern src/utils/portfolio_page.py's load_live_broker_prices
    already established for the portfolio pages."""
    if not symbols:
        return {}
    resp = (
        client.table("user_live_prices")
        .select("symbol, latest_price")
        .eq("user_id", user_id)
        .eq("option_type", "EQ")
        .in_("symbol", symbols)
        .execute()
    )
    return {row["symbol"]: float(row["latest_price"]) for row in (resp.data or [])}


def upsert_user_live_prices(client: Client, user_id: str, prices: dict[str, float]) -> None:
    """Replaces this account's cached live LTP for exactly the given
    symbols -- deletes any existing rows for them first, then inserts the
    fresh set, matching this app's usual "replace" convention (e.g.
    replace_broker_holdings) rather than a partial per-row upsert. Symbols
    not in `prices` (this account's provider didn't quote them this time)
    are left untouched, not cleared -- a transient miss shouldn't erase a
    still-good previous value. Writes `option_type='EQ'` rows only -- see
    get_user_live_fo_prices/upsert_user_live_fo_prices below for the
    futures/options counterpart (migration 0032)."""
    if not prices:
        return
    client.table("user_live_prices").delete().eq("user_id", user_id).eq("option_type", "EQ").in_(
        "symbol", list(prices)
    ).execute()
    payload = [
        {"user_id": user_id, "symbol": symbol, "latest_price": price, "option_type": "EQ"}
        for symbol, price in prices.items()
    ]
    client.table("user_live_prices").insert(payload).execute()


# A futures/option contract identity -- (symbol, expiry_date, strike_price,
# option_type), option_type one of 'FUT'/'CE'/'PE'. strike_price is
# meaningless for a future (sentinel 0, matching migration 0032's DB
# default) but always present here for a uniform key shape.
FOContractKey = tuple[str, date, float, str]


def get_user_live_fo_prices(client: Client, user_id: str, contracts: list[FOContractKey]) -> dict[FOContractKey, float]:
    """This account's own cached live F&O LTP (user_live_prices,
    migration 0032) for the given contracts -- the futures/options
    counterpart of get_user_live_prices above. Written by "Market Data
    Refresh" only for a Dhan-provider account (src/utils/refresh_bar.py,
    src/data_providers/dhan_provider.py's get_fo_quotes) -- Zerodha has no
    F&O instrument resolver yet, so a Zerodha-provider account simply
    never has rows here. Callers merge this over whatever EOD/broker-sync
    LTP they already have, same {**shared, **live} pattern
    get_user_live_prices' own callers use."""
    if not contracts:
        return {}
    symbols = sorted({c[0] for c in contracts})
    resp = (
        client.table("user_live_prices")
        .select("symbol, expiry_date, strike_price, option_type, latest_price")
        .eq("user_id", user_id)
        .neq("option_type", "EQ")
        .in_("symbol", symbols)
        .execute()
    )
    wanted = set(contracts)
    result: dict[FOContractKey, float] = {}
    for row in resp.data or []:
        key: FOContractKey = (
            row["symbol"],
            date.fromisoformat(row["expiry_date"]) if isinstance(row["expiry_date"], str) else row["expiry_date"],
            float(row["strike_price"]),
            row["option_type"],
        )
        if key in wanted:
            result[key] = float(row["latest_price"])
    return result


def upsert_user_live_fo_prices(client: Client, user_id: str, prices: dict[FOContractKey, float]) -> None:
    """Replaces this account's cached live F&O LTP for exactly the given
    contracts -- same delete-then-insert "replace" convention as
    upsert_user_live_prices, scoped to option_type != 'EQ' rows only so
    the two functions never touch each other's rows even though they
    share one table."""
    if not prices:
        return
    for (symbol, expiry_date, strike_price, option_type), price in prices.items():
        (
            client.table("user_live_prices")
            .delete()
            .eq("user_id", user_id)
            .eq("symbol", symbol)
            .eq("expiry_date", expiry_date.isoformat())
            .eq("strike_price", strike_price)
            .eq("option_type", option_type)
            .execute()
        )
    payload = [
        {
            "user_id": user_id,
            "symbol": symbol,
            "expiry_date": expiry_date.isoformat(),
            "strike_price": strike_price,
            "option_type": option_type,
            "latest_price": price,
        }
        for (symbol, expiry_date, strike_price, option_type), price in prices.items()
    ]
    client.table("user_live_prices").insert(payload).execute()


def get_previous_snapshot(client: Client, symbol: str, before_date: date) -> DailyScreenerSnapshot | None:
    resp = (
        client.table("daily_screener_snapshots")
        .select("*")
        .eq("symbol", symbol)
        .lt("snapshot_date", before_date.isoformat())
        .order("snapshot_date", desc=True)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    return DailyScreenerSnapshot.model_validate(rows[0]) if rows else None


def get_classification_history(client: Client, symbol: str, days: int = 180) -> list[dict]:
    resp = client.rpc("get_classification_history", {"p_symbol": symbol, "p_days": days}).execute()
    return resp.data or []
