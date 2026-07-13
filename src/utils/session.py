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
    for key in (
        "sb_access_token", "sb_refresh_token", "sb_user_id", "sb_user_email",
        "sb_recovery_pending", "_auth_client",
    ):
        st.session_state.pop(key, None)


def request_password_reset(email: str) -> str | None:
    """Sends a password-reset email. Returns an error message on failure."""
    app_base_url = get_settings().app_base_url
    options = {"redirect_to": app_base_url} if app_base_url else {}
    try:
        _auth_client().auth.reset_password_for_email(email, options=options)
    except Exception as exc:  # noqa: BLE001
        return str(exc)
    return None


def set_new_password(new_password: str) -> str | None:
    """Updates the current session's password -- works for both a normal
    logged-in session (Settings page) and a password-recovery session
    established by handle_recovery_redirect()."""
    try:
        get_user_client_cached().auth.update_user({"password": new_password})
    except Exception as exc:  # noqa: BLE001
        return str(exc)
    st.session_state.pop("sb_recovery_pending", None)
    return None


def is_password_recovery_pending() -> bool:
    return bool(st.session_state.get("sb_recovery_pending"))


def inject_hash_to_query_bridge() -> None:
    """Streamlit can't read a URL's #fragment server-side -- browsers never
    send fragments in the HTTP request. Supabase's confirmation/password
    -reset email links put the session token in the fragment
    (`#access_token=...&type=recovery`), so this reaches into the parent
    page (st.iframe renders in a same-origin iframe) and moves the
    fragment into the query string instead, where st.query_params can see
    it, then reloads. A no-op if there's no such fragment.
    """
    st.iframe(
        """
        <script>
        (function() {
            try {
                var hash = window.parent.location.hash;
                if (hash && hash.indexOf('access_token') !== -1) {
                    var params = new URLSearchParams(hash.substring(1));
                    var url = new URL(window.parent.location.href);
                    params.forEach(function(value, key) { url.searchParams.set(key, value); });
                    url.hash = '';
                    window.parent.location.replace(url.toString());
                }
            } catch (e) {}
        })();
        </script>
        """,
        height=1,
    )


def handle_recovery_redirect() -> None:
    """Call once near the top of app.py, before require_login(). Picks up
    the access_token/refresh_token/type=recovery query params left by
    inject_hash_to_query_bridge() after a user clicks a password-reset
    email link, establishes that session, and flags that a new password
    must be set before the rest of the app is usable (enforced in
    require_login(), which every page already calls).
    """
    params = st.query_params
    if params.get("type") != "recovery":
        return
    access_token = params.get("access_token")
    refresh_token = params.get("refresh_token")
    if not access_token:
        return

    client = _auth_client()
    try:
        client.auth.set_session(access_token, refresh_token or "")
        user_resp = client.auth.get_user(access_token)
    except Exception as exc:  # noqa: BLE001
        st.error(f"This password reset link is invalid or has expired: {exc}")
        st.query_params.clear()
        return

    st.session_state["sb_access_token"] = access_token
    st.session_state["sb_refresh_token"] = refresh_token
    st.session_state["sb_user_id"] = user_resp.user.id
    st.session_state["sb_user_email"] = user_resp.user.email
    st.session_state["sb_recovery_pending"] = True
    st.query_params.clear()


def _render_set_new_password_form() -> None:
    st.title("Set a new password")
    st.caption(f"Resetting password for {current_user_email()}")
    with st.form("set_new_password_form"):
        new_password = st.text_input("New password", type="password")
        confirm_password = st.text_input("Confirm new password", type="password")
        submitted = st.form_submit_button("Update password")
    if submitted:
        if new_password != confirm_password:
            st.error("Passwords do not match.")
        elif len(new_password) < 6:
            st.error("Password must be at least 6 characters.")
        else:
            error = set_new_password(new_password)
            if error:
                st.error(error)
            else:
                st.success("Password updated -- you're now signed in.")
                st.rerun()


def require_login() -> None:
    """Call at the top of every page. Renders a login/sign-up/forgot-
    password form (or, mid-recovery, a mandatory set-new-password form)
    and st.stop()s the page if there's no usable active session."""
    if is_password_recovery_pending():
        _render_set_new_password_form()
        st.stop()

    if is_logged_in():
        return

    st.title("Nifty 50 Momentum & Dividend Screener")
    st.caption("Sign in to view the screener, set alerts, and save your filters.")
    login_tab, signup_tab, forgot_tab = st.tabs(["Sign in", "Create account", "Forgot password?"])

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

    with forgot_tab:
        st.caption("We'll email you a link to set a new password.")
        with st.form("forgot_password_form"):
            email = st.text_input("Email", key="forgot_email")
            submitted = st.form_submit_button("Send reset link")
        if submitted:
            error = request_password_reset(email)
            if error:
                st.error(error)
            else:
                st.success("If an account exists for that email, a password reset link has been sent.")

    st.stop()
