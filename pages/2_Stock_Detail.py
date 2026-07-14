from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from src.calculations.moving_averages import moving_average_series
from src.models.alert import Alert
from src.models.enums import AlertType
from src.models.user import UserPosition
from src.repositories import (
    alerts_repo,
    companies_repo,
    dividends_repo,
    fundamentals_repo,
    price_repo,
    settings_repo,
    snapshot_repo,
)
from src.services.explanation import explain_classification
from src.services.threshold_override import recompute_with_user_thresholds
from src.utils.formatting import format_crores, format_inr, format_pct, pass_fail_badge
from src.utils.session import current_user_id, get_user_client_cached, require_login
from src.utils.timezones import format_ist, now_ist
from src.utils.ui import buy_sell_label, plotly_template, render_disclaimer, status_badge

st.set_page_config(page_title="Stock Detail | Nifty 50 Screener", page_icon="🔍", layout="wide")
require_login()

client = get_user_client_cached()
user_id = current_user_id()
user_settings = settings_repo.get_user_settings(client, user_id)

st.title("🔍 Stock Detail")

companies = companies_repo.list_current_constituents(client)
symbol_options = sorted(c.symbol for c in companies)
if not symbol_options:
    st.info("No constituents loaded yet. Apply supabase/seed.sql first.")
    st.stop()

default_symbol = st.session_state.get("selected_symbol", symbol_options[0])
default_index = symbol_options.index(default_symbol) if default_symbol in symbol_options else 0
symbol = st.selectbox("Select a stock", symbol_options, index=default_index)
st.session_state["selected_symbol"] = symbol

row = snapshot_repo.get_latest_screener_row(client, symbol)
if row is None:
    st.warning("No screener data for this symbol yet.")
    st.stop()
row = recompute_with_user_thresholds(row, user_settings)

# ---------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------
col1, col2, col3 = st.columns([2, 1, 1])
with col1:
    st.subheader(f"{row.name} ({row.symbol})")
    st.caption(f"{row.sector or '—'} · {row.industry or '—'}")
with col2:
    st.markdown(status_badge(row.status), unsafe_allow_html=True)
    st.markdown(f"**{buy_sell_label(row.status)}**")
with col3:
    st.metric("Latest price", format_inr(row.latest_price))
    st.caption(f"As of {format_ist(now_ist())} · snapshot {row.snapshot_date}")

render_disclaimer()

st.info(explain_classification(row))

st.subheader("Pass / fail scorecard")
score_cols = st.columns(3)
with score_cols[0]:
    st.metric("A · Dividend yield", format_pct(row.ttm_dividend_yield, signed=False))
    st.markdown(pass_fail_badge(row.criterion_a))
    st.caption(f"Threshold: > {user_settings.dividend_yield_threshold}%")
with score_cols[1]:
    st.metric("B · Momentum", "1D/5D/20D")
    st.markdown(
        f"1D {format_pct(row.return_1d)} · 5D {format_pct(row.return_5d)} · 20D {format_pct(row.return_20d)}"
    )
    st.markdown(pass_fail_badge(row.criterion_b))
with score_cols[2]:
    st.metric("C · PEG", f"{row.peg_ratio:.2f}" if row.peg_ratio is not None else "N/A")
    st.markdown(pass_fail_badge(row.criterion_c))
    st.caption(f"Threshold: <= {user_settings.peg_threshold}")

# ---------------------------------------------------------------------
# Price chart
# ---------------------------------------------------------------------
st.divider()
st.subheader("Price chart")

range_choice = st.radio("Range", ["1M", "3M", "6M", "1Y", "5Y"], horizontal=True, index=3)
range_days = {"1M": 30, "3M": 90, "6M": 182, "1Y": 365, "5Y": 365 * 5}[range_choice]
history_from = date.today() - timedelta(days=range_days + 210)  # pad for 200DMA warmup
history = price_repo.get_price_history(client, symbol, history_from, date.today())

show_ma = st.multiselect("Moving averages", ["20 DMA", "50 DMA", "200 DMA"], default=["20 DMA", "50 DMA"])
position = settings_repo.get_user_position(client, user_id, symbol)

if not history:
    st.info("No price history stored yet for this symbol.")
else:
    hist_df = pd.DataFrame([p.model_dump() for p in history]).sort_values("trade_date")
    hist_df["trade_date"] = pd.to_datetime(hist_df["trade_date"])
    has_ohlc = hist_df[["open", "high", "low"]].notna().all(axis=None)

    windows = {"20 DMA": 20, "50 DMA": 50, "200 DMA": 200}
    ma_df = moving_average_series(hist_df.set_index("trade_date")["close"], tuple(windows[w] for w in show_ma) or (20,))

    plot_df = hist_df[hist_df["trade_date"] >= pd.Timestamp(date.today() - timedelta(days=range_days))]

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.75, 0.25], vertical_spacing=0.03)

    if has_ohlc:
        fig.add_trace(
            go.Candlestick(
                x=plot_df["trade_date"], open=plot_df["open"], high=plot_df["high"],
                low=plot_df["low"], close=plot_df["close"], name="Price",
            ),
            row=1, col=1,
        )
    else:
        fig.add_trace(
            go.Scatter(x=plot_df["trade_date"], y=plot_df["close"], mode="lines", name="Close"), row=1, col=1
        )

    for label in show_ma:
        col_name = f"ma_{windows[label]}"
        if col_name in ma_df.columns:
            series = ma_df.loc[ma_df.index >= plot_df["trade_date"].min(), col_name]
            fig.add_trace(go.Scatter(x=series.index, y=series.values, mode="lines", name=label), row=1, col=1)

    if not plot_df.empty:
        high_52w_row = plot_df.loc[plot_df["high"].fillna(plot_df["close"]).idxmax()] if len(plot_df) else None
        low_52w_row = plot_df.loc[plot_df["low"].fillna(plot_df["close"]).idxmin()] if len(plot_df) else None
        if high_52w_row is not None:
            fig.add_hline(
                y=float(high_52w_row["high"] if pd.notna(high_52w_row["high"]) else high_52w_row["close"]),
                line_dash="dot", line_color="green", annotation_text="Period high", row=1, col=1,
            )
        if low_52w_row is not None:
            fig.add_hline(
                y=float(low_52w_row["low"] if pd.notna(low_52w_row["low"]) else low_52w_row["close"]),
                line_dash="dot", line_color="red", annotation_text="Period low", row=1, col=1,
            )

    if position:
        if position.entry_price:
            fig.add_hline(y=position.entry_price, line_dash="dash", line_color="blue", annotation_text="Entry", row=1, col=1)
        if position.target_price:
            fig.add_hline(y=position.target_price, line_dash="dash", line_color="green", annotation_text="Target", row=1, col=1)
        if position.stop_loss:
            fig.add_hline(y=position.stop_loss, line_dash="dash", line_color="red", annotation_text="Stop-loss", row=1, col=1)

    if plot_df["volume"].notna().any():
        fig.add_trace(go.Bar(x=plot_df["trade_date"], y=plot_df["volume"], name="Volume"), row=2, col=1)

    fig.update_layout(
        template=plotly_template(user_settings.theme), height=560, xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )
    st.plotly_chart(fig, use_container_width=True)
    if not has_ohlc:
        st.caption("OHLC data unavailable for this symbol/provider -- showing a line chart of closing prices.")

# ---------------------------------------------------------------------
# Dividend history + fundamentals
# ---------------------------------------------------------------------
st.divider()
div_col, fund_col = st.columns(2)

with div_col:
    st.subheader("Dividend history")
    dividends = dividends_repo.get_dividend_events(client, symbol, date.today() - timedelta(days=365 * 5), date.today())
    if dividends:
        div_df = pd.DataFrame([d.model_dump() for d in dividends])
        div_fig = go.Figure(go.Bar(x=div_df["ex_date"], y=div_df["amount_per_share"], name="Dividend / share"))
        div_fig.update_layout(template=plotly_template(user_settings.theme), height=300)
        st.plotly_chart(div_fig, use_container_width=True)
    else:
        st.caption("No dividend events recorded in the last 5 years.")

with fund_col:
    st.subheader("Fundamentals")
    fundamentals = fundamentals_repo.get_latest_fundamentals(client, symbol)
    st.markdown(f"**PE ratio:** {row.pe_ratio:.2f}" if row.pe_ratio is not None else "**PE ratio:** N/A")
    st.markdown(f"**PEG ratio:** {row.peg_ratio:.2f}" if row.peg_ratio is not None else "**PEG ratio:** N/A")
    st.markdown(f"**TTM dividend yield:** {format_pct(row.ttm_dividend_yield, signed=False)}")
    if fundamentals:
        st.markdown(f"**Market cap:** {format_crores(fundamentals.market_cap)}")
        st.markdown(f"**EPS:** {format_inr(fundamentals.eps)}" if fundamentals.eps is not None else "**EPS:** N/A")
        st.caption(f"Fundamentals source: {fundamentals.source} · as of {fundamentals.as_of_date}" + (" (stale)" if fundamentals.is_stale else ""))
    else:
        st.caption("No fundamentals data stored yet.")

# ---------------------------------------------------------------------
# Classification history
# ---------------------------------------------------------------------
st.divider()
st.subheader("Classification history")
history_rows = snapshot_repo.get_classification_history(client, symbol, days=365)
if history_rows:
    hist_status_df = pd.DataFrame(history_rows)
    status_order = {"green": 3, "amber": 2, "red": 1, "unavailable": 0}
    hist_status_df["status_rank"] = hist_status_df["status"].map(status_order)
    status_fig = go.Figure(
        go.Scatter(
            x=hist_status_df["snapshot_date"], y=hist_status_df["status_rank"], mode="lines+markers",
            marker=dict(
                color=hist_status_df["status"].map({"green": "#0f9d58", "amber": "#f4a623", "red": "#d93025", "unavailable": "#8a8f98"})
            ),
            text=hist_status_df["status"], hovertemplate="%{x}: %{text}",
        )
    )
    status_fig.update_layout(
        template=plotly_template(user_settings.theme), height=250,
        yaxis=dict(tickmode="array", tickvals=[0, 1, 2, 3], ticktext=["Unavailable", "Red", "Amber", "Green"]),
    )
    st.plotly_chart(status_fig, use_container_width=True)
else:
    st.caption("No classification history yet -- daily snapshots accumulate as the refresh job runs.")

st.caption(
    f"Source: prices from the configured price provider, fundamentals from the configured fundamentals "
    f"provider. Snapshot computed {row.snapshot_date}."
)

# ---------------------------------------------------------------------
# Position (entry/target/stop-loss/notes) + risk/reward
# ---------------------------------------------------------------------
st.divider()
st.subheader("Your position notes")
with st.form("position_form"):
    entry = st.number_input("Intended entry price (INR)", value=float(position.entry_price) if position and position.entry_price else 0.0)
    target = st.number_input("Target sell price (INR)", value=float(position.target_price) if position and position.target_price else 0.0)
    stop_loss = st.number_input("Stop-loss price (INR)", value=float(position.stop_loss) if position and position.stop_loss else 0.0)
    holding_days = st.number_input("Expected holding period (days, optional)", min_value=0, value=int(position.holding_period_days) if position and position.holding_period_days else 0)
    notes = st.text_area("Notes", value=position.notes if position and position.notes else "")
    saved = st.form_submit_button("Save position")

if saved:
    new_position = UserPosition(
        user_id=user_id, symbol=symbol,
        entry_price=entry or None, target_price=target or None, stop_loss=stop_loss or None,
        holding_period_days=holding_days or None, notes=notes or None,
    )
    settings_repo.upsert_user_position(client, new_position)
    st.success("Position saved.")
    position = new_position

if position and position.entry_price and position.target_price and position.stop_loss:
    rr = position.risk_reward_ratio
    st.metric("Risk / reward ratio", f"{rr:.2f}" if rr is not None else "N/A")

# ---------------------------------------------------------------------
# Alerts for this stock
# ---------------------------------------------------------------------
st.divider()
st.subheader("Alerts for this stock")
existing_alerts = [a for a in alerts_repo.list_alerts(client, user_id) if a.symbol == symbol]
if existing_alerts:
    for a in existing_alerts:
        c1, c2 = st.columns([4, 1])
        c1.markdown(f"**{a.alert_type}** · active={a.is_active} · cooldown={a.cooldown_minutes}min · config={a.config}")
        if c2.button("Delete", key=f"del_{a.id}"):
            alerts_repo.delete_alert(client, user_id, a.id)
            st.rerun()
else:
    st.caption("No alerts configured for this stock yet.")

with st.expander("➕ Create a new alert"):
    alert_type = st.selectbox("Alert type", [t.value for t in AlertType if t != AlertType.REFRESH_FAILURE])
    config: dict = {}
    if alert_type == AlertType.PRICE_CROSS.value:
        config["level"] = st.number_input("Price level (INR)", value=float(row.latest_price or 0))
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
        config["entry_price"] = st.number_input("Entry price (INR)", value=float(row.latest_price or 0))
    elif alert_type == AlertType.SELL_WATCH.value:
        config["target_price"] = st.number_input("Target price (INR, optional)", value=0.0) or None
        config["stop_loss"] = st.number_input("Stop-loss price (INR, optional)", value=0.0) or None

    cooldown = st.number_input("Cooldown (minutes)", value=60, min_value=1)
    if st.button("Create alert"):
        alerts_repo.create_alert(
            client,
            Alert(user_id=user_id, symbol=symbol, alert_type=AlertType(alert_type), config=config, cooldown_minutes=cooldown),
        )
        st.success("Alert created.")
        st.rerun()
