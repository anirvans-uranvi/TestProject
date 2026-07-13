from __future__ import annotations

import streamlit as st

from src.config import get_settings
from src.models.enums import Theme
from src.models.user import UserSettings
from src.repositories import settings_repo
from src.utils.session import current_user_email, current_user_id, get_user_client_cached, require_login, set_new_password
from src.utils.ui import render_disclaimer

st.set_page_config(page_title="Settings | Nifty 50 Screener", page_icon="⚙️", layout="wide")
require_login()

client = get_user_client_cached()
user_id = current_user_id()
app_settings = get_settings()

st.title("⚙️ Settings")
render_disclaimer()

current = settings_repo.get_user_settings(client, user_id)

st.subheader("Screening thresholds")
st.caption("These control what counts as a passing criterion for you. Changes apply immediately across the app.")
with st.form("thresholds_form"):
    dividend_threshold = st.number_input(
        "Dividend yield threshold (%) -- criterion A passes above this",
        value=float(current.dividend_yield_threshold), min_value=0.0, step=0.25,
    )
    peg_threshold = st.number_input(
        "PEG threshold -- criterion C passes above this",
        value=float(current.peg_threshold), min_value=0.0, step=0.1,
    )
    stale_minutes = st.number_input(
        "Stale-data threshold (minutes) -- rows older than this become Unavailable",
        value=int(current.stale_data_threshold_minutes), min_value=1, step=5,
    )
    theme = st.selectbox(
        "Chart theme", [t.value for t in Theme], index=[t.value for t in Theme].index(current.theme)
    )
    submitted = st.form_submit_button("Save settings")

if submitted:
    settings_repo.upsert_user_settings(
        client,
        UserSettings(
            user_id=user_id,
            dividend_yield_threshold=dividend_threshold,
            peg_threshold=peg_threshold,
            stale_data_threshold_minutes=stale_minutes,
            theme=Theme(theme),
        ),
    )
    st.success("Settings saved.")

st.divider()
st.subheader("Notification channels")
st.caption("In-app notifications are always on. Other channels are extension points -- see README.")
st.checkbox("In-app (Streamlit + notification history)", value=True, disabled=True)
st.checkbox("Email", value=False, disabled=True, help="Not implemented yet -- see src/notifications/email_adapter.py")
st.checkbox("Telegram", value=False, disabled=True, help="Not implemented yet -- see src/notifications/telegram_adapter.py")
st.checkbox("Slack", value=False, disabled=True, help="Not implemented yet -- see src/notifications/slack_adapter.py")

st.divider()
st.subheader("Account")
st.markdown(f"**Signed in as:** {current_user_email()}")

with st.expander("Change password"):
    with st.form("change_password_form"):
        new_password = st.text_input("New password", type="password", key="settings_new_password")
        confirm_password = st.text_input("Confirm new password", type="password", key="settings_confirm_password")
        change_submitted = st.form_submit_button("Update password")
    if change_submitted:
        if new_password != confirm_password:
            st.error("Passwords do not match.")
        elif len(new_password) < 6:
            st.error("Password must be at least 6 characters.")
        else:
            error = set_new_password(new_password)
            if error:
                st.error(error)
            else:
                st.success("Password updated.")

st.divider()
st.subheader("Data provider configuration (read-only, set via environment variables)")
st.code(
    f"MARKET_DATA_PROVIDER={app_settings.market_data_provider}\n"
    f"FUNDAMENTALS_PROVIDER={app_settings.fundamentals_provider}",
    language="bash",
)
