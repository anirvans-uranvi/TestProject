from __future__ import annotations

from supabase import Client

from src.models.company import Company, Nifty50Constituent


def list_current_constituents(client: Client) -> list[Company]:
    resp = (
        client.table("nifty50_constituents")
        .select("symbol, companies(symbol, name, sector, industry, isin, updated_at)")
        .eq("is_current", True)
        .execute()
    )
    companies = []
    for row in resp.data or []:
        joined = row.get("companies")
        if joined:
            companies.append(Company.model_validate(joined))
    return companies


def get_company(client: Client, symbol: str) -> Company | None:
    resp = client.table("companies").select("*").eq("symbol", symbol).limit(1).execute()
    rows = resp.data or []
    return Company.model_validate(rows[0]) if rows else None


def upsert_companies(client: Client, companies: list[Company]) -> None:
    if not companies:
        return
    payload = [c.model_dump(mode="json", exclude_none=True) for c in companies]
    client.table("companies").upsert(payload, on_conflict="symbol").execute()


def upsert_constituents(client: Client, constituents: list[Nifty50Constituent]) -> None:
    if not constituents:
        return
    payload = [c.model_dump(mode="json", exclude_none=True) for c in constituents]
    client.table("nifty50_constituents").upsert(payload, on_conflict="symbol,index_effective_from").execute()
