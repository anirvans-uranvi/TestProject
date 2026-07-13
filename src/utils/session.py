"""Streamlit session/auth helpers shared by app.py and every page.

Supabase Auth session tokens live in st.session_state (per browser
session, cleared on full app restart) -- never in a cookie or local
storage we control directly. Every read after login uses a client bound
to the user's access token so Postgres RLS applies as that user.
"""
from __future__ import annotations

import streamlit as st
from supabase import Client

from src.config import get_settings
from src.repositories.supabase_client import get_anon_client, get_user_client


def _auth_client() -> Client:
    if "_auth_client" not in st.session_state:
        st.session_state["_auth_client"] = get_anon_client()
    return st.session_state["_auth_client"]


def is_logged_in() -> bool:
    return bool(st.session_state.get("sb_access_token")) and bool(st.session_state.get("sb_user_id"))


def current_user_id() -> str:
    return st.session_state["sb_user_id"]


def current_user_email() -> str | None:
    return st.session_state.get("sb_user_email")


def get_user_client_cached() -> Client:
    return get_user_client(st.session_state["sb_access_token"], st.session_state.get("sb_refresh_token"))


def sign_in(email: str, password: str) -> str | None:
    """Returns an error message on failure, None on success."""
    try:
        resp = _auth_client().auth.sign_in_with_password({"email": email, "password": password})
    except Exception as exc:  # noqa: BLE001
        return str(exc)
    if resp.session is None or resp.user is None:
        return "Invalid credentials"
    st.session_state["sb_access_token"] = resp.session.access_token
    st.session_state["sb_refresh_token"] = resp.session.refresh_token
    st.session_state["sb_user_id"] = resp.user.id
    st.session_state["sb_user_email"] = resp.user.email
    return None


def sign_up(email: str, password: str) -> str | None:
    payload: dict = {"email": email, "password": password}
    app_base_url = get_settings().app_base_url
    if app_base_url:
        payload["options"] = {"email_redirect_to": app_base_url}
    try:
        resp = _auth_client().auth.sign_up(payload)
    except Exception as exc:  # noqa: BLE001
        return str(exc)
    if resp.user is None:
        return "Sign-up failed"
    if resp.session is None:
        return "Check your email to confirm your account, then sign in."
    st.session_state["sb_access_token"] = resp.session.access_token
    st.session_state["sb_refresh_token"] = resp.session.refresh_token
    st.session_state["sb_user_id"] = resp.user.id
    st.session_state["sb_user_email"] = resp.user.email
    return None


def sign_out() -> None:
    for key in ("sb_access_token", "sb_refresh_token", "sb_user_id", "sb_user_email", "_auth_client"):
        st.session_state.pop(key, None)


def require_login() -> None:
    """Call at the top of every page. Renders a login/sign-up form and
    st.stop()s the page if there's no active session."""
    if is_logged_in():
        return

    st.title("Nifty 50 Momentum & Dividend Screener")
    st.caption("Sign in to view the screener, set alerts, and save your filters.")
    login_tab, signup_tab = st.tabs(["Sign in", "Create account"])

    with login_tab:
        with st.form("login_form"):
            email = st.text_input("Email", key="login_email")
            password = st.text_input("Password", type="password", key="login_password")
            submitted = st.form_submit_button("Sign in")
        if submitted:
            error = sign_in(email, password)
            if error:
                st.error(error)
            else:
                st.rerun()

    with signup_tab:
        with st.form("signup_form"):
            email = st.text_input("Email", key="signup_email")
            password = st.text_input("Password", type="password", key="signup_password")
            submitted = st.form_submit_button("Create account")
        if submitted:
            error = sign_up(email, password)
            if error:
                st.info(error) if "confirm" in error.lower() else st.error(error)
            else:
                st.rerun()

    st.stop()
