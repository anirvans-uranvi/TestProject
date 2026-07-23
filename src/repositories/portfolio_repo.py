"""Per-user portfolio holdings (migration 0012). All reads/writes go
through the calling user's own client -- RLS scopes every row to
auth.uid() = user_id, same as saved_filters/alerts/user_settings."""
from __future__ import annotations

from supabase import Client

from src.models.portfolio import PortfolioHolding


def list_holdings(client: Client, user_id: str) -> list[PortfolioHolding]:
    resp = client.table("portfolio_holdings").select("*").eq("user_id", user_id).execute()
    return [PortfolioHolding.model_validate(r) for r in (resp.data or [])]


def replace_broker_holdings(client: Client, user_id: str, broker: str, holdings: list[PortfolioHolding]) -> None:
    """Full sync for one broker: deletes every existing row for
    (user_id, broker), then inserts the freshly parsed set. A re-upload
    represents the broker's current holdings, not a merge -- positions
    no longer in the file should disappear."""
    client.table("portfolio_holdings").delete().eq("user_id", user_id).eq("broker", broker).execute()
    if not holdings:
        return
    payload = [h.model_dump(mode="json", exclude={"uploaded_at"}) for h in holdings]
    client.table("portfolio_holdings").insert(payload).execute()
