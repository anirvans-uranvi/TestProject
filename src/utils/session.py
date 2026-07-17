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
from src.models.enums import Theme
from src.repositories.supabase_client import get_anon_client, get_user_client
from src.utils.ui import inject_design_system


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
    """Sends a password-reset email containing a one-time code. Returns an
    error message on failure.

    We deliberately do NOT rely on the email's magic link: Supabase puts
    the recovery session token in the URL fragment
    (`#access_token=...&type=recovery`), which browsers never send to any
    server -- and there is no way to bridge it into Streamlit either,
    because Streamlit's own iframe sandbox (used to run any JS we inject)
    omits `allow-top-navigation`, so a script inside it is flatly denied
    permission to redirect/rewrite the parent page's URL (confirmed
    directly: navigating window.parent.location throws
    `SecurityError: ... does not have permission to navigate the target
    frame`). The 6-digit code Supabase includes in the same email sidesteps
    this entirely -- the user types it into verify_recovery_code() below,
    which is a plain server-side call.
    """
    app_base_url = get_settings().app_base_url
    options = {"redirect_to": app_base_url} if app_base_url else {}
    try:
        _auth_client().auth.reset_password_for_email(email, options=options)
    except Exception as exc:  # noqa: BLE001
        return str(exc)
    return None


def verify_recovery_code(email: str, code: str) -> str | None:
    """Exchanges the one-time code from the reset email for a session,
    and flags that a new password must be set (enforced in require_login(),
    which every page already calls)."""
    try:
        resp = _auth_client().auth.verify_otp({"email": email, "token": code, "type": "recovery"})
    except Exception as exc:  # noqa: BLE001
        return str(exc)
    if resp.session is None or resp.user is None:
        return "Invalid or expired code."
    st.session_state["sb_access_token"] = resp.session.access_token
    st.session_state["sb_refresh_token"] = resp.session.refresh_token
    st.session_state["sb_user_id"] = resp.user.id
    st.session_state["sb_user_email"] = resp.user.email
    st.session_state["sb_recovery_pending"] = True
    return None


def set_new_password(new_password: str) -> str | None:
    """Updates the current session's password -- works for both a normal
    logged-in session (Settings page) and a password-recovery session
    established by verify_recovery_code()."""
    try:
        get_user_client_cached().auth.update_user({"password": new_password})
    except Exception as exc:  # noqa: BLE001
        return str(exc)
    st.session_state.pop("sb_recovery_pending", None)
    return None


def is_password_recovery_pending() -> bool:
    return bool(st.session_state.get("sb_recovery_pending"))


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
    and st.stop()s the page if there's no usable active session.

    Injects Tailwind + the global CSS design system here, before anything
    else -- every page previously called inject_tailwind() itself, but
    only *after* require_login(), which meant the unauthenticated login/
    signup/forgot-password screen (and the mandatory post-recovery
    set-new-password screen) rendered with none of it loaded. This is now
    the single enforcement point instead of relying on every page to
    order its own calls correctly. Uses the light theme unconditionally
    here since there's no signed-in user yet to read a Theme preference
    from; logged-in pages re-inject with the user's actual Theme setting
    afterwards (a later <style> tag wins the cascade)."""
    inject_design_system(Theme.LIGHT)

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
        st.caption("Step 1: we'll email you a 6-digit code (ignore the link in that email).")
        with st.form("forgot_password_form"):
            reset_email = st.text_input("Email", key="forgot_email")
            send_submitted = st.form_submit_button("Send reset code")
        if send_submitted:
            error = request_password_reset(reset_email)
            if error:
                st.error(error)
            else:
                st.success("If an account exists for that email, a 6-digit code has been sent.")

        st.divider()
        st.caption("Step 2: enter the code to set a new password.")
        with st.form("verify_code_form"):
            code_email = st.text_input("Email", key="verify_email")
            code = st.text_input("6-digit code", key="verify_code")
            verify_submitted = st.form_submit_button("Verify code")
        if verify_submitted:
            error = verify_recovery_code(code_email, code)
            if error:
                st.error(error)
            else:
                st.rerun()

    st.stop()
