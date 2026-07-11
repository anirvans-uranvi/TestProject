"""Supabase client factories.

Two distinct clients are used deliberately:
  - service client: SUPABASE_SERVICE_ROLE_KEY, bypasses RLS. Only ever used
    server-side (refresh scripts, alert evaluation) -- NEVER imported into
    Streamlit page code that runs in a browser-reachable process.
  - user client: SUPABASE_ANON_KEY + the logged-in user's access token, so
    every query is subject to RLS as that user. This is what pages/*.py use.

Repos take a `Client` as an explicit argument rather than reaching for a
module-level singleton, so callers can't accidentally read/write shared
data with the wrong privilege level.
"""
from __future__ import annotations

from supabase import Client, create_client

from src.config import get_settings


def get_service_client() -> Client:
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_service_role_key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set for server-side access")
    return create_client(settings.supabase_url, settings.supabase_service_role_key)


def get_anon_client() -> Client:
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_anon_key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_ANON_KEY must be set")
    return create_client(settings.supabase_url, settings.supabase_anon_key)


def get_user_client(access_token: str, refresh_token: str | None = None) -> Client:
    """Anon client scoped to a logged-in user's session so RLS applies."""
    client = get_anon_client()
    client.postgrest.auth(access_token)
    if refresh_token:
        try:
            client.auth.set_session(access_token, refresh_token)
        except Exception:  # noqa: BLE001 - session refresh is best-effort here
            pass
    return client
