"""Per-user portfolio holdings (migration 0012, portfolio_name added in
0014), positions (migration 0016), and broker API connections (migration
0017). All reads/writes go through the calling user's own client -- RLS
scopes every row to auth.uid() = user_id, same as
saved_filters/alerts/user_settings."""
from __future__ import annotations

from supabase import Client

from src.models.portfolio import (
    BrokerConnection,
    PortfolioHolding,
    PortfolioPosition,
    PortfolioPositionMeta,
    PortfolioTradeGroup,
    PortfolioTradeMeta,
)


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
    every broker's holdings, positions, and manual Trade
    grouping/metadata within it. Does NOT touch broker_connections --
    that's an account-wide credential since migration 0029, not scoped to
    an individual portfolio; disconnecting it is a separate action in
    Settings' "Data Provider" section."""
    client.table("portfolio_holdings").delete().eq("user_id", user_id).eq("portfolio_name", portfolio_name).execute()
    client.table("portfolio_positions").delete().eq("user_id", user_id).eq("portfolio_name", portfolio_name).execute()
    client.table("portfolio_trade_groups").delete().eq("user_id", user_id).eq("portfolio_name", portfolio_name).execute()
    client.table("portfolio_trade_meta").delete().eq("user_id", user_id).eq("portfolio_name", portfolio_name).execute()
    client.table("portfolio_position_meta").delete().eq("user_id", user_id).eq("portfolio_name", portfolio_name).execute()


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


def get_broker_connection(client: Client, user_id: str, broker: str) -> BrokerConnection | None:
    """The saved API connection for this account's chosen broker, if any
    -- None means the user hasn't connected it (account-wide since
    migration 0029; no longer scoped to an individual portfolio)."""
    resp = (
        client.table("broker_connections")
        .select("*")
        .eq("user_id", user_id)
        .eq("broker", broker)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    return BrokerConnection.model_validate(rows[0]) if rows else None


def upsert_broker_connection(client: Client, connection: BrokerConnection) -> None:
    """Saves or replaces this account's API credentials for one broker --
    used both for the initial "Save & Sync" and for "Update credentials"
    (which re-saves both fields and resets token_saved_at)."""
    payload = [connection.model_dump(mode="json", exclude_none=True)]
    client.table("broker_connections").upsert(payload, on_conflict="user_id,broker").execute()


def delete_broker_connection(client: Client, user_id: str, broker: str) -> None:
    """"Disconnect" -- removes the saved API credentials only. Any
    holdings/positions already synced from this broker are left as-is."""
    client.table("broker_connections").delete().eq("user_id", user_id).eq("broker", broker).execute()


DEFAULT_PORTFOLIO_NAME = "My Portfolio"


def get_or_default_portfolio_name(client: Client, user_id: str) -> str:
    """The one portfolio_name this account's live broker sync should
    target -- on request, once Data Provider became account-wide and CSV
    upload (the old multi-portfolio "+ New portfolio" flow) was dropped:
    reuses whatever single portfolio_name this account's holdings/
    positions already share (a fresh sync should land in the same place
    as the last one, not silently spawn a second portfolio), or falls
    back to DEFAULT_PORTFOLIO_NAME for a brand-new account with nothing
    saved yet. If more than one portfolio_name still exists (e.g. from
    before this account-wide collapse), the alphabetically-first one is
    used, deterministically rather than depending on set ordering --
    existing data under any other name is left untouched, just no longer
    reachable from a live sync."""
    names = {h.portfolio_name for h in list_holdings(client, user_id)} | {
        p.portfolio_name for p in list_positions(client, user_id)
    }
    return min(names) if names else DEFAULT_PORTFOLIO_NAME


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
    client: Client,
    user_id: str,
    portfolio_name: str,
    trade_id: str,
    *,
    underlying_label: str | None,
    trade_type: str,
    bucket_override: str | None = None,
) -> None:
    """Saves (upserts) one trade's underlying label / trade type / bucket
    override -- the Analyse Trade page's edit form. `underlying_label=None`
    clears back to the auto-computed default (still writes a row, so a
    previously-corrected label can be explicitly reset without leaving a
    stale non-null value behind); same for `bucket_override=None`, which
    reverts to classify_underlying_bucket's computed Stock/Index/Other
    table."""
    payload = [
        {
            "user_id": user_id,
            "portfolio_name": portfolio_name,
            "trade_id": trade_id,
            "underlying_label": underlying_label,
            "trade_type": trade_type,
            "bucket_override": bucket_override,
        }
    ]
    client.table("portfolio_trade_meta").upsert(payload, on_conflict="user_id,portfolio_name,trade_id").execute()


def list_position_meta(client: Client, user_id: str) -> list[PortfolioPositionMeta]:
    """Every leg's trade_date/stop_loss override across every one of the
    user's portfolios -- same shape as list_trade_groups (keyed by
    (portfolio_name, broker, raw_name)), for My CSP. A leg with no row
    here has no trade_date entered yet and no stop_loss computed yet --
    see src.services.portfolio_service.csp_target_pnl/csp_stop_loss."""
    resp = client.table("portfolio_position_meta").select("*").eq("user_id", user_id).execute()
    return [PortfolioPositionMeta.model_validate(r) for r in (resp.data or [])]


def set_position_trade_date(
    client: Client, user_id: str, portfolio_name: str, broker: str, raw_name: str, trade_date
) -> None:
    """Saves (upserts) one leg's Trade Date -- My CSP's own edit form.
    Only touches `trade_date`; any already-saved `stop_loss` for this leg
    is left untouched (omitted from the payload entirely, not sent as
    None -- same partial-upsert convention `upsert_broker_connection`
    already relies on)."""
    payload = [
        {
            "user_id": user_id,
            "portfolio_name": portfolio_name,
            "broker": broker,
            "raw_name": raw_name,
            "trade_date": trade_date.isoformat() if trade_date else None,
        }
    ]
    client.table("portfolio_position_meta").upsert(payload, on_conflict="user_id,portfolio_name,broker,raw_name").execute()


def set_position_stop_loss(
    client: Client, user_id: str, portfolio_name: str, broker: str, raw_name: str, stop_loss: float | None
) -> None:
    """Saves (upserts) one leg's ratcheted Stop Loss -- called by My CSP
    on every render once it recomputes a leg's stop loss
    (`portfolio_service.csp_stop_loss`), so the next render's "existing
    stop loss" reflects it. Only touches `stop_loss`; any already-saved
    `trade_date` is left untouched, same partial-upsert convention as
    set_position_trade_date above."""
    payload = [
        {
            "user_id": user_id,
            "portfolio_name": portfolio_name,
            "broker": broker,
            "raw_name": raw_name,
            "stop_loss": stop_loss,
        }
    ]
    client.table("portfolio_position_meta").upsert(payload, on_conflict="user_id,portfolio_name,broker,raw_name").execute()
