#!/usr/bin/env python
"""Refreshes companies + nifty50_constituents from a maintained symbol list.

NSE reconstitutes the index semi-annually (cutoffs Jan 31 / Jul 31). This
script does NOT scrape NSE; it re-applies the CURRENT_CONSTITUENTS list
below (kept in sync by hand -- update it after each reconstitution, e.g.
by cross-checking https://www.nseindia.com/products-services/indices-nifty50-index)
and reconciles it against what's already in Supabase: symbols no longer
present are marked is_current=False with index_effective_to set.

Usage:
    python scripts/fetch_nifty50_constituents.py
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.models.company import Company, Nifty50Constituent  # noqa: E402
from src.repositories import companies_repo  # noqa: E402
from src.repositories.supabase_client import get_service_client  # noqa: E402
from src.utils.logging import get_logger  # noqa: E402

logger = get_logger(__name__)

EFFECTIVE_FROM = date(2026, 7, 11)

# symbol -> (name, sector, industry) -- keep in sync with supabase/seed.sql
CURRENT_CONSTITUENTS: dict[str, tuple[str, str, str]] = {
    "RELIANCE": ("Reliance Industries Ltd", "Energy", "Refineries / Oil & Gas"),
    "HDFCBANK": ("HDFC Bank Ltd", "Financial Services", "Private Bank"),
    "BHARTIARTL": ("Bharti Airtel Ltd", "Telecommunication", "Telecom Services"),
    "ICICIBANK": ("ICICI Bank Ltd", "Financial Services", "Private Bank"),
    "SBIN": ("State Bank of India", "Financial Services", "Public Bank"),
    "TCS": ("Tata Consultancy Services Ltd", "Information Technology", "IT Services"),
    "BAJFINANCE": ("Bajaj Finance Ltd", "Financial Services", "NBFC"),
    "LT": ("Larsen & Toubro Ltd", "Industrials", "Engineering & Construction"),
    "HINDUNILVR": ("Hindustan Unilever Ltd", "Consumer Staples", "FMCG"),
    "SUNPHARMA": ("Sun Pharmaceutical Industries Ltd", "Healthcare", "Pharmaceuticals"),
    "MARUTI": ("Maruti Suzuki India Ltd", "Consumer Discretionary", "Passenger Cars"),
    "INFY": ("Infosys Ltd", "Information Technology", "IT Services"),
    "ADANIPORTS": ("Adani Ports and Special Economic Zone Ltd", "Industrials", "Port Operations"),
    "AXISBANK": ("Axis Bank Ltd", "Financial Services", "Private Bank"),
    "ADANIENT": ("Adani Enterprises Ltd", "Industrials", "Diversified / Trading"),
    "TITAN": ("Titan Company Ltd", "Consumer Discretionary", "Jewellery & Watches"),
    "M&M": ("Mahindra & Mahindra Ltd", "Consumer Discretionary", "Passenger & Commercial Vehicles"),
    "KOTAKBANK": ("Kotak Mahindra Bank Ltd", "Financial Services", "Private Bank"),
    "ITC": ("ITC Ltd", "Consumer Staples", "FMCG / Tobacco"),
    "ULTRACEMCO": ("UltraTech Cement Ltd", "Materials", "Cement"),
    "NTPC": ("NTPC Ltd", "Utilities", "Power Generation"),
    "HCLTECH": ("HCL Technologies Ltd", "Information Technology", "IT Services"),
    "ONGC": ("Oil & Natural Gas Corporation Ltd", "Energy", "Oil Exploration"),
    "BAJAJFINSV": ("Bajaj Finserv Ltd", "Financial Services", "Diversified Financials"),
    "JSWSTEEL": ("JSW Steel Ltd", "Materials", "Steel & Iron"),
    "BEL": ("Bharat Electronics Ltd", "Industrials", "Defence & Aerospace"),
    "BAJAJ-AUTO": ("Bajaj Auto Ltd", "Consumer Discretionary", "Two & Three Wheelers"),
    "NESTLEIND": ("Nestle India Ltd", "Consumer Staples", "FMCG / Food Products"),
    "ETERNAL": ("Eternal Ltd", "Consumer Discretionary", "E-Commerce / Food Delivery"),
    "COALINDIA": ("Coal India Ltd", "Energy", "Mining"),
    "POWERGRID": ("Power Grid Corporation of India Ltd", "Utilities", "Power Transmission"),
    "ASIANPAINT": ("Asian Paints Ltd", "Materials", "Paints"),
    "SHRIRAMFIN": ("Shriram Finance Ltd", "Financial Services", "NBFC"),
    "TATASTEEL": ("Tata Steel Ltd", "Materials", "Steel & Iron"),
    "GRASIM": ("Grasim Industries Ltd", "Materials", "Diversified / Cement"),
    "HINDALCO": ("Hindalco Industries Ltd", "Materials", "Non-Ferrous Metals"),
    "INDIGO": ("InterGlobe Aviation Ltd", "Industrials", "Airlines"),
    "EICHERMOT": ("Eicher Motors Ltd", "Consumer Discretionary", "Two Wheelers"),
    "SBILIFE": ("SBI Life Insurance Company Ltd", "Financial Services", "Insurance"),
    "WIPRO": ("Wipro Ltd", "Information Technology", "IT Services"),
    "JIOFIN": ("Jio Financial Services Ltd", "Financial Services", "NBFC"),
    "TRENT": ("Trent Ltd", "Consumer Discretionary", "Retailing"),
    "TECHM": ("Tech Mahindra Ltd", "Information Technology", "IT Services"),
    "APOLLOHOSP": ("Apollo Hospitals Enterprise Ltd", "Healthcare", "Hospitals"),
    "TMPV": ("Tata Motors Passenger Vehicles Ltd", "Consumer Discretionary", "Passenger Vehicles"),
    "HDFCLIFE": ("HDFC Life Insurance Co Ltd", "Financial Services", "Insurance"),
    "CIPLA": ("Cipla Ltd", "Healthcare", "Pharmaceuticals"),
    "TATACONSUM": ("Tata Consumer Products Ltd", "Consumer Staples", "FMCG / Tea & Coffee"),
    "MAXHEALTH": ("Max Healthcare Institute Ltd", "Healthcare", "Hospitals"),
    "DRREDDY": ("Dr Reddys Laboratories Ltd", "Healthcare", "Pharmaceuticals"),
}


def main() -> None:
    client = get_service_client()

    companies = [
        Company(symbol=sym, name=name, sector=sector, industry=industry)
        for sym, (name, sector, industry) in CURRENT_CONSTITUENTS.items()
    ]
    companies_repo.upsert_companies(client, companies)

    constituents = [
        Nifty50Constituent(
            symbol=sym, company_name=name, sector=sector, index_effective_from=EFFECTIVE_FROM, is_current=True
        )
        for sym, (name, sector, _industry) in CURRENT_CONSTITUENTS.items()
    ]
    companies_repo.upsert_constituents(client, constituents)

    existing = companies_repo.list_current_constituents(client)
    removed = [c.symbol for c in existing if c.symbol not in CURRENT_CONSTITUENTS]
    if removed:
        client.table("nifty50_constituents").update(
            {"is_current": False, "index_effective_to": date.today().isoformat()}
        ).in_("symbol", removed).eq("is_current", True).execute()
        logger.info("marked %d symbol(s) no longer in the index: %s", len(removed), removed)

    logger.info("upserted %d current Nifty 50 constituents", len(CURRENT_CONSTITUENTS))


if __name__ == "__main__":
    main()
