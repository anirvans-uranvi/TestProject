"""Per-user portfolio holdings (migration 0012, portfolio_name added in
0014). All reads/writes go through the calling user's own client -- RLS
scopes every row to auth.uid() = user_id, same as
saved_filters/alerts/user_settings."""
from __future__ import annotations

from supabase import Client

from src.models.portfolio import PortfolioHolding


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
    every broker within it. Used by the Portfolio page's "Delete this
    portfolio" control; every other portfolio is untouched."""
    client.table("portfolio_holdings").delete().eq("user_id", user_id).eq("portfolio_name", portfolio_name).execute()
