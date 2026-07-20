#!/usr/bin/env python
"""Remove leftover mock (source='mock') rows from the shared market-data
tables, as documented in README.md's Limitations section:

    Mock data seeded via scripts/seed_mock_data.py does not get cleaned up
    automatically when you switch to a real provider... a leftover mock
    dividend row inflated one stock's TTM dividend yield roughly 27x.

Dry-run by default (just counts + prints what *would* be deleted). Pass
--confirm to actually delete. After deleting, daily_screener_snapshots
still holds values computed from the old (mock-contaminated) inputs, so
re-run `python scripts/run_refresh.py --mode=screener` afterward to
recompute it from the now-clean price_history/fundamental_snapshots/
dividend_events.

Usage:
    python scripts/cleanup_mock_data.py              # dry run: show counts
    python scripts/cleanup_mock_data.py --confirm     # actually delete

Requires SUPABASE_SERVICE_ROLE_KEY (bypasses RLS, like the other scripts/
jobs that write/delete shared market data).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.repositories.supabase_client import get_service_client  # noqa: E402
from src.utils.logging import get_logger  # noqa: E402

logger = get_logger(__name__)

MOCK_SOURCE_TABLES = ["price_history", "fundamental_snapshots", "dividend_events"]


def count_mock_rows(client, table: str) -> int:
    resp = client.table(table).select("id", count="exact").eq("source", "mock").execute()
    return resp.count or 0


def delete_mock_rows(client, table: str) -> None:
    client.table(table).delete().eq("source", "mock").execute()


def main() -> None:
    parser = argparse.ArgumentParser(description="Remove leftover mock rows from shared market-data tables")
    parser.add_argument("--confirm", action="store_true", help="actually delete (default: dry run, counts only)")
    args = parser.parse_args()

    client = get_service_client()

    counts = {table: count_mock_rows(client, table) for table in MOCK_SOURCE_TABLES}
    total = sum(counts.values())

    print("Mock rows found (source = 'mock'):")
    for table, n in counts.items():
        print(f"  {table:<24} {n:>8,}")
    print(f"  {'TOTAL':<24} {total:>8,}")

    if total == 0:
        print("\nNothing to clean up.")
        return

    if not args.confirm:
        print("\nDry run only -- nothing was deleted. Re-run with --confirm to delete these rows.")
        return

    for table in MOCK_SOURCE_TABLES:
        if counts[table]:
            delete_mock_rows(client, table)
            logger.info("deleted %d mock rows from %s", counts[table], table)

    print(f"\nDeleted {total:,} mock rows.")
    print("Now run `python scripts/run_refresh.py --mode=screener` to recompute "
          "daily_screener_snapshots from the cleaned data.")


if __name__ == "__main__":
    main()
