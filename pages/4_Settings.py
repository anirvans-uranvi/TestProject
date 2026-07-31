from __future__ import annotations

import pandas as pd
import streamlit as st

from src.config import get_settings
from src.models.alert import Alert
from src.models.enums import AlertType, Theme
from src.models.user import UserSettings
from src.repositories import alerts_repo, companies_repo, notification_repo, settings_repo
from src.utils.formatting import alert_type_label, summarize_alert_config
from src.utils.session import (
    current_user_email,
    current_user_id,
    get_user_client_cached,
    require_login,
    set_new_password,
    sign_out,
)
from src.utils.timezones import format_ist
from src.utils.ui import inject_global_styles, render_alert_row, render_disclaimer, render_pill

st.set_page_config(page_title="Settings | Nifty 50 Screener", page_icon="⚙️", layout="wide")
require_login()  # already injects Tailwind + the light-theme CSS design system

client = get_user_client_cached()
user_id = current_user_id()
app_settings = get_settings()

current = settings_repo.get_user_settings(client, user_id)
inject_global_styles(current.theme)  # re-inject with the user's actual theme -- a later <style> tag wins

st.title("⚙️ Settings")
render_disclaimer()

st.subheader("Screening thresholds")
st.caption("These control what counts as a passing criterion for you. Changes apply immediately across the app.")
with st.form("thresholds_form"):
    dividend_threshold = st.number_input(
        "Dividend yield threshold (%) -- criterion A passes above this",
        value=float(current.dividend_yield_threshold), min_value=0.0, step=0.25,
    )
    peg_threshold = st.number_input(
        "PEG threshold -- criterion C passes at or below this",
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
st.subheader("Alerts")
companies = companies_repo.list_current_constituents(client)
alert_symbol_options = ["Portfolio-wide"] + sorted(c.symbol for c in companies)

st.caption("Your alerts")
alerts = alerts_repo.list_alerts(client, user_id)
if alerts:
    for a in alerts:
        with st.container(border=True):
            c1, c2, c3, c4 = st.columns([2, 3, 1, 1])
            c1.markdown(f"**{a.symbol or 'Portfolio-wide'}**")
            c2.markdown(
                render_alert_row(
                    alert_type_label(a.alert_type), summarize_alert_config(a.alert_type, a.config),
                    a.cooldown_minutes, a.is_active, current.theme,
                ),
                unsafe_allow_html=True,
            )
            new_active = c3.checkbox("Active", value=a.is_active, key=f"active_{a.id}")
            if new_active != a.is_active:
                alerts_repo.update_alert(client, a.id, {"is_active": new_active})
                st.rerun()
            if c4.button("🗑️ Delete", key=f"delete_{a.id}"):
                alerts_repo.delete_alert(client, user_id, a.id)
                st.rerun()
else:
    st.caption("No alerts yet. Create one below, or from the Stock Detail page.")

with st.expander("➕ Create a new alert"):
    with st.form("new_alert_form"):
        alert_target = st.selectbox("Applies to", alert_symbol_options)
        alert_symbol = None if alert_target == "Portfolio-wide" else alert_target
        available_types = (
            [AlertType.REFRESH_FAILURE] if alert_symbol is None else [t for t in AlertType if t != AlertType.REFRESH_FAILURE]
        )
        alert_type = st.selectbox("Alert type", [t.value for t in available_types])

        alert_config: dict = {}
        if alert_type == AlertType.PRICE_CROSS.value:
            alert_config["level"] = st.number_input("Price level (INR)", value=1000.0)
            alert_config["direction"] = st.selectbox("Direction", ["above", "below"])
        elif alert_type == AlertType.MOMENTUM_CROSS.value:
            alert_config["period"] = st.selectbox("Period", ["1d", "5d", "20d"])
            alert_config["direction"] = st.selectbox("Direction", ["above_zero", "below_zero"])
        elif alert_type == AlertType.DIVIDEND_YIELD_CROSS.value:
            alert_config["threshold"] = st.number_input("Yield threshold (%)", value=3.0)
            alert_config["direction"] = st.selectbox("Direction", ["above", "below"])
        elif alert_type == AlertType.PEG_CROSS.value:
            alert_config["threshold"] = st.number_input("PEG threshold", value=1.0)
            alert_config["direction"] = st.selectbox("Direction", ["above", "below"])
        elif alert_type == AlertType.BUY_WATCH.value:
            alert_config["entry_price"] = st.number_input("Entry price (INR)", value=1000.0)
        elif alert_type == AlertType.SELL_WATCH.value:
            alert_config["target_price"] = st.number_input("Target price (INR, optional)", value=0.0) or None
            alert_config["stop_loss"] = st.number_input("Stop-loss price (INR, optional)", value=0.0) or None

        alert_cooldown = st.number_input("Cooldown (minutes)", value=60, min_value=1)
        alert_submitted = st.form_submit_button("Create alert")

    if alert_submitted:
        alerts_repo.create_alert(
            client,
            Alert(
                user_id=user_id, symbol=alert_symbol, alert_type=AlertType(alert_type),
                config=alert_config, cooldown_minutes=alert_cooldown,
            ),
        )
        st.success("Alert created.")
        st.rerun()

with st.expander("Notification history"):
    notifications = notification_repo.list_notifications(client, user_id, limit=100)
    if notifications:
        notif_df = pd.DataFrame(
            [
                {
                    "Time (IST)": format_ist(n.triggered_at),
                    "Symbol": n.symbol or "Portfolio-wide",
                    "Channel": n.channel,
                    "Message": n.message,
                    "Read": "✅" if n.read_at else "—",
                }
                for n in notifications
            ]
        )
        st.dataframe(notif_df, use_container_width=True, hide_index=True)

        unread = [n for n in notifications if n.read_at is None]
        if unread and st.button(f"Mark all {len(unread)} unread as read"):
            for n in unread:
                notification_repo.mark_read(client, user_id, n.id)
            st.rerun()
    else:
        st.caption("No notifications yet.")

st.divider()
st.subheader("Notification channels")
st.caption("In-app notifications are always on. Other channels are extension points -- see README.")
st.checkbox("In-app (Streamlit + notification history)", value=True, disabled=True)
_coming_soon = " ".join(
    render_pill(f"{name} · coming soon", tone="neutral", theme=current.theme)
    for name in ("Email", "Telegram", "Slack")
)
st.markdown(_coming_soon, unsafe_allow_html=True)

st.divider()
st.subheader("Account")
st.markdown(f"**Signed in as:** {current_user_email()}")
if st.button("Sign out"):
    sign_out()
    st.rerun()

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
