"""Client for the on-demand Supabase Edge Function
(supabase/functions/manual-refresh) that does the real fetch-from-Yahoo-
and-write-to-Supabase work the Dashboard's "Manual refresh" button
triggers. This module never touches the service-role key -- it only ever
sends the calling user's own access token, exactly like any other
authenticated request this app makes. The Edge Function holds the
service-role key itself, safely, since it runs server-side inside
Supabase's infrastructure rather than in a user's browser session.
"""
from __future__ import annotations

import httpx

from src.config import get_settings

# ~50 symbols x 2 Yahoo calls each, batched concurrently server-side --
# generous but not unbounded.
TIMEOUT_SECONDS = 90.0


class ManualRefreshError(Exception):
    """Raised on a non-2xx response or network failure. `retriable` is
    True for a cooldown (429) response -- the caller may want to say
    "try again shortly" rather than treating it as a hard failure."""

    def __init__(self, message: str, retriable: bool = False):
        super().__init__(message)
        self.retriable = retriable


def trigger_manual_refresh(access_token: str) -> dict:
    """POSTs to the manual-refresh Edge Function and returns its JSON
    summary: {succeeded, failed, total, symbolsFailed, startedAt, finishedAt}.
    Raises ManualRefreshError on failure."""
    settings = get_settings()
    if not settings.supabase_url:
        raise ManualRefreshError("SUPABASE_URL is not configured")

    url = f"{settings.supabase_url}/functions/v1/manual-refresh"
    try:
        resp = httpx.post(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise ManualRefreshError(f"Could not reach the refresh function: {exc}") from exc

    if resp.status_code == 429:
        try:
            detail = resp.json().get("message", "Please wait before refreshing again.")
        except ValueError:
            detail = "Please wait before refreshing again."
        raise ManualRefreshError(detail, retriable=True)

    if resp.status_code >= 400:
        try:
            detail = resp.json().get("error", resp.text)
        except ValueError:
            detail = resp.text
        raise ManualRefreshError(f"Refresh failed: {detail}")

    return resp.json()


# Downloading + unzipping + parsing one day's ~7MB F&O bhavcopy (and
# upserting ~9,000 rows) is heavier than the cash-market refresh above.
FO_TIMEOUT_SECONDS = 120.0


def trigger_fo_refresh(access_token: str) -> dict:
    """POSTs to the fo-refresh Edge Function (supabase/functions/fo-refresh),
    which checks whether NSE has published a newer F&O bhavcopy than what's
    already loaded and, if so, downloads + ingests it. Returns its JSON
    summary: either {updated: false, message, latestAvailable, latestLoaded}
    when already current, or {updated: true, tradeDate, futuresRows,
    optionRows, ...} after a real ingest. Raises ManualRefreshError on
    failure (reused rather than a parallel exception type, since the
    calling convention -- cooldown/4xx/5xx handling -- is identical)."""
    settings = get_settings()
    if not settings.supabase_url:
        raise ManualRefreshError("SUPABASE_URL is not configured")

    url = f"{settings.supabase_url}/functions/v1/fo-refresh"
    try:
        resp = httpx.post(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=FO_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise ManualRefreshError(f"Could not reach the F&O refresh function: {exc}") from exc

    if resp.status_code == 429:
        try:
            detail = resp.json().get("message", "Please wait before refreshing again.")
        except ValueError:
            detail = "Please wait before refreshing again."
        raise ManualRefreshError(detail, retriable=True)

    if resp.status_code >= 400:
        try:
            detail = resp.json().get("error", resp.text)
        except ValueError:
            detail = resp.text
        raise ManualRefreshError(f"F&O refresh failed: {detail}")

    return resp.json()
