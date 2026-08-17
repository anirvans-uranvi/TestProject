"""Per-user portfolio holdings (migration 0012, portfolio_name added in
0014), positions (migration 0016), and broker API connections (migration
0017). All reads/writes go through the calling user's own client -- RLS
scopes every row to auth.uid() = user_id, same as
saved_filters/alerts/user_settings."""
from __future__ import annotations

from supabase import Client

from src.models.portfolio import BrokerConnection, PortfolioHolding, PortfolioPosition, PortfolioTradeGroup, PortfolioTradeMeta


def list_holdings(client: Client, user_id: str) -> list[PortfolioHolding]:
    """Every holding across every one of the user's portfolios -- the
    Portfolio page groups these by portfolio_name itself (one tab each)
    rather than querying per portfolio."""
    resp = client.table("portfolio_holdings").select("*").eq("user_id", user_id).execute()
    return [PortfolioHolding.model_validate(r) for r in (resp.data or [])]


def list_portfolio_symbols(client: Client, user_id: str) -> list[str]:
    """Distinct resolved symbols across every one of the user's
    portfolios (any broker, any portfolio_name) -- used to widen Stock
    Detail's and Options' symbol pickers (and the Portfolio page's own
    search-icon gating) to cover portfolio-only stocks (ETFs, non-Nifty50
    stocks) alongside the current Nifty50 constituents, once a symbol is
    actually resolved. Unresolved rows (symbol is NULL) are excluded --
    there's nothing else in the app to look them up by."""
    resp = client.table("portfolio_holdings").select("symbol").eq("user_id", user_id).execute()
    return sorted({r["symbol"] for r in (resp.data or []) if r.get("symbol")})


def replace_broker_holdings(
    client: Client, user_id: str, portfolio_name: str, broker: str, holdings: list[PortfolioHolding]
) -> None:
    """Full sync for one broker within one portfolio: deletes every
    existing row for (user_id, portfolio_name, broker), then inserts the
    freshly parsed set. A re-upload represents that broker's current
    holdings within this portfolio, not a merge -- positions no longer in
    the file should disappear. Other brokers within the same portfolio,
    and every other portfolio entirely, are untouched -- this is also how
    a brand-new portfolio gets created: there's simply nothing to delete
    yet for a `portfolio_name` that's never been used before."""
    (
        client.table("portfolio_holdings")
        .delete()
        .eq("user_id", user_id)
        .eq("portfolio_name", portfolio_name)
        .eq("broker", broker)
        .execute()
    )
    if not holdings:
        return
    payload = [h.model_dump(mode="json", exclude={"uploaded_at"}) for h in holdings]
    client.table("portfolio_holdings").insert(payload).execute()


def delete_portfolio(client: Client, user_id: str, portfolio_name: str) -> None:
    """Permanently deletes every row for (user_id, portfolio_name) --
    every broker's holdings, positions, saved API connection, and manual
    Trade grouping/metadata within it. Used by the My Broker page's
    "Delete this portfolio" control; every other portfolio is untouched."""
    client.table("portfolio_holdings").delete().eq("user_id", user_id).eq("portfolio_name", portfolio_name).execute()
    client.table("portfolio_positions").delete().eq("user_id", user_id).eq("portfolio_name", portfolio_name).execute()
    client.table("broker_connections").delete().eq("user_id", user_id).eq("portfolio_name", portfolio_name).execute()
    client.table("portfolio_trade_groups").delete().eq("user_id", user_id).eq("portfolio_name", portfolio_name).execute()
    client.table("portfolio_trade_meta").delete().eq("user_id", user_id).eq("portfolio_name", portfolio_name).execute()


def list_positions(client: Client, user_id: str) -> list[PortfolioPosition]:
    """Every F&O position across every one of the user's portfolios --
    same shape as list_holdings, grouped by portfolio_name on the page
    side rather than queried per portfolio."""
    resp = client.table("portfolio_positions").select("*").eq("user_id", user_id).execute()
    return [PortfolioPosition.model_validate(r) for r in (resp.data or [])]


def replace_broker_positions(
    client: Client, user_id: str, portfolio_name: str, broker: str, positions: list[PortfolioPosition]
) -> None:
    """Full sync for one broker's positions within one portfolio -- same
    delete-then-insert replace semantics as replace_broker_holdings, and
    entirely independent of it (uploading holdings never touches positions
    and vice versa)."""
    (
        client.table("portfolio_positions")
        .delete()
        .eq("user_id", user_id)
        .eq("portfolio_name", portfolio_name)
        .eq("broker", broker)
        .execute()
    )
    if not positions:
        return
    payload = [p.model_dump(mode="json", exclude={"uploaded_at"}) for p in positions]
    client.table("portfolio_positions").insert(payload).execute()


def get_broker_connection(
    client: Client, user_id: str, portfolio_name: str, broker: str
) -> BrokerConnection | None:
    """The saved API connection for one broker within one portfolio, if
    any -- None means the user hasn't connected this broker's API here
    (they may still have CSV-uploaded holdings/positions for it)."""
    resp = (
        client.table("broker_connections")
        .select("*")
        .eq("user_id", user_id)
        .eq("portfolio_name", portfolio_name)
        .eq("broker", broker)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    return BrokerConnection.model_validate(rows[0]) if rows else None


def upsert_broker_connection(client: Client, connection: BrokerConnection) -> None:
    """Saves or replaces one broker's API credentials for one portfolio --
    used both for the initial "Save & Sync" and for "Update credentials"
    (which re-saves both fields and resets token_saved_at)."""
    payload = [connection.model_dump(mode="json", exclude_none=True)]
    client.table("broker_connections").upsert(payload, on_conflict="user_id,portfolio_name,broker").execute()


def delete_broker_connection(client: Client, user_id: str, portfolio_name: str, broker: str) -> None:
    """"Disconnect" -- removes the saved API credentials only. Any
    holdings/positions already synced from this broker are left as-is,
    same as switching a portfolio away from CSV upload never deletes prior
    data."""
    (
        client.table("broker_connections")
        .delete()
        .eq("user_id", user_id)
        .eq("portfolio_name", portfolio_name)
        .eq("broker", broker)
        .execute()
    )


def list_trade_groups(client: Client, user_id: str) -> list[PortfolioTradeGroup]:
    """Every manual "Trade" grouping override across every one of the
    user's portfolios -- same shape as list_holdings/list_positions,
    grouped by portfolio_name on the page side. Legs with no override row
    here default to being grouped by their own underlying symbol -- see
    src.services.portfolio_service.assign_trade_ids."""
    resp = client.table("portfolio_trade_groups").select("*").eq("user_id", user_id).execute()
    return [PortfolioTradeGroup.model_validate(r) for r in (resp.data or [])]


def set_trade_group(
    client: Client, user_id: str, portfolio_name: str, legs: list[tuple[str, str]], trade_id: str
) -> None:
    """Manually assigns every (broker, raw_name) leg in `legs` to
    `trade_id` -- the "combine" action in the Positions section. Upserts
    one row per leg; a leg already grouped elsewhere is simply
    reassigned."""
    if not legs:
        return
    payload = [
        {
            "user_id": user_id,
            "portfolio_name": portfolio_name,
            "broker": broker,
            "raw_name": raw_name,
            "trade_id": trade_id,
        }
        for broker, raw_name in legs
    ]
    client.table("portfolio_trade_groups").upsert(payload, on_conflict="user_id,portfolio_name,broker,raw_name").execute()


def clear_trade_group_overrides(client: Client, user_id: str, portfolio_name: str, legs: list[tuple[str, str]]) -> None:
    """Reverts every (broker, raw_name) leg in `legs` back to the default
    per-underlying-symbol Trade grouping -- the "split" action."""
    for broker, raw_name in legs:
        (
            client.table("portfolio_trade_groups")
            .delete()
            .eq("user_id", user_id)
            .eq("portfolio_name", portfolio_name)
            .eq("broker", broker)
            .eq("raw_name", raw_name)
            .execute()
        )


def list_trade_meta(client: Client, user_id: str) -> list[PortfolioTradeMeta]:
    """Every trade-level underlying-label/trade-type override across every
    one of the user's portfolios -- same shape as list_trade_groups, but
    keyed by (portfolio_name, trade_id) rather than a leg's (broker,
    raw_name). A trade with no row here uses the defaults: the
    auto-computed underlying label and trade_type "Trade" -- see
    src.services.portfolio_service.group_into_trades."""
    resp = client.table("portfolio_trade_meta").select("*").eq("user_id", user_id).execute()
    return [PortfolioTradeMeta.model_validate(r) for r in (resp.data or [])]


def set_trade_meta(
    client: Client, user_id: str, portfolio_name: str, trade_id: str, *, underlying_label: str | None, trade_type: str
) -> None:
    """Saves (upserts) one trade's underlying label / trade type override
    -- the Analyse Trade page's edit form. `underlying_label=None` clears
    back to the auto-computed default (still writes a row, so a
    previously-corrected label can be explicitly reset without leaving a
    stale non-null value behind)."""
    payload = [
        {
            "user_id": user_id,
            "portfolio_name": portfolio_name,
            "trade_id": trade_id,
            "underlying_label": underlying_label,
            "trade_type": trade_type,
        }
    ]
    client.table("portfolio_trade_meta").upsert(payload, on_conflict="user_id,portfolio_name,trade_id").execute()
