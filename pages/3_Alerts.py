from __future__ import annotations

import pandas as pd
import streamlit as st

from src.models.alert import Alert
from src.models.enums import AlertType
from src.repositories import alerts_repo, companies_repo, notification_repo
from src.utils.session import current_user_id, get_user_client_cached, require_login
from src.utils.timezones import format_ist
from src.utils.ui import render_disclaimer

st.set_page_config(page_title="Alerts | Nifty 50 Screener", page_icon="🔔", layout="wide")
require_login()

client = get_user_client_cached()
user_id = current_user_id()

st.title("🔔 Alerts")
render_disclaimer()

companies = companies_repo.list_current_constituents(client)
symbol_options = ["Portfolio-wide"] + sorted(c.symbol for c in companies)

st.subheader("Your alerts")
alerts = alerts_repo.list_alerts(client, user_id)
if alerts:
    for a in alerts:
        with st.container(border=True):
            c1, c2, c3, c4 = st.columns([2, 3, 1, 1])
            c1.markdown(f"**{a.symbol or 'Portfolio-wide'}**")
            c2.markdown(f"`{a.alert_type}` · config: `{a.config}` · cooldown {a.cooldown_minutes}min")
            new_active = c3.checkbox("Active", value=a.is_active, key=f"active_{a.id}")
            if new_active != a.is_active:
                alerts_repo.update_alert(client, a.id, {"is_active": new_active})
                st.rerun()
            if c4.button("🗑️ Delete", key=f"delete_{a.id}"):
                alerts_repo.delete_alert(client, user_id, a.id)
                st.rerun()
else:
    st.caption("No alerts yet. Create one below, or from the Stock Detail page.")

st.divider()
st.subheader("➕ Create a new alert")
with st.form("new_alert_form"):
    target = st.selectbox("Applies to", symbol_options)
    symbol = None if target == "Portfolio-wide" else target
    available_types = [AlertType.REFRESH_FAILURE] if symbol is None else [t for t in AlertType if t != AlertType.REFRESH_FAILURE]
    alert_type = st.selectbox("Alert type", [t.value for t in available_types])

    config: dict = {}
    if alert_type == AlertType.PRICE_CROSS.value:
        config["level"] = st.number_input("Price level (INR)", value=1000.0)
        config["direction"] = st.selectbox("Direction", ["above", "below"])
    elif alert_type == AlertType.MOMENTUM_CROSS.value:
        config["period"] = st.selectbox("Period", ["1d", "5d", "20d"])
        config["direction"] = st.selectbox("Direction", ["above_zero", "below_zero"])
    elif alert_type == AlertType.DIVIDEND_YIELD_CROSS.value:
        config["threshold"] = st.number_input("Yield threshold (%)", value=3.0)
        config["direction"] = st.selectbox("Direction", ["above", "below"])
    elif alert_type == AlertType.PEG_CROSS.value:
        config["threshold"] = st.number_input("PEG threshold", value=1.0)
        config["direction"] = st.selectbox("Direction", ["above", "below"])
    elif alert_type == AlertType.BUY_WATCH.value:
        config["entry_price"] = st.number_input("Entry price (INR)", value=1000.0)
    elif alert_type == AlertType.SELL_WATCH.value:
        config["target_price"] = st.number_input("Target price (INR, optional)", value=0.0) or None
        config["stop_loss"] = st.number_input("Stop-loss price (INR, optional)", value=0.0) or None

    cooldown = st.number_input("Cooldown (minutes)", value=60, min_value=1)
    submitted = st.form_submit_button("Create alert")

if submitted:
    alerts_repo.create_alert(
        client,
        Alert(user_id=user_id, symbol=symbol, alert_type=AlertType(alert_type), config=config, cooldown_minutes=cooldown),
    )
    st.success("Alert created.")
    st.rerun()

st.divider()
st.subheader("Notification history")
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
