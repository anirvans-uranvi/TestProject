"""Shared Streamlit UI fragments: status badges, disclaimer, chart theming."""
from __future__ import annotations

import streamlit as st

from src.models.enums import MarketState, ScreenerStatus, Theme

STATUS_STYLE = {
    ScreenerStatus.GREEN: ("#0f9d58", "🟢", "Green"),
    ScreenerStatus.AMBER: ("#f4a623", "🟠", "Amber"),
    ScreenerStatus.RED: ("#d93025", "🔴", "Red"),
    ScreenerStatus.UNAVAILABLE: ("#8a8f98", "⚪", "Unavailable"),
}

MARKET_STATE_LABEL = {
    MarketState.OPEN: "🟢 Open",
    MarketState.PRE_OPEN: "🟡 Pre-open",
    MarketState.CLOSED: "⚪ Closed",
    MarketState.DATA_DELAYED: "🟠 Data Delayed",
}

BUY_SELL_LABEL = {
    ScreenerStatus.GREEN: "Model Buy Watch",
    ScreenerStatus.AMBER: "Model Caution",
    ScreenerStatus.RED: "Model Exit / Review",
    ScreenerStatus.UNAVAILABLE: "Model Unavailable",
}

DISCLAIMER = (
    "This dashboard is an analytical tool, not investment advice. "
    "Verify data and consider your risk tolerance before trading."
)


def status_badge(status: ScreenerStatus) -> str:
    color, icon, label = STATUS_STYLE[ScreenerStatus(status)]
    return (
        f'<span style="background-color:{color}22;color:{color};border:1px solid {color};'
        f'border-radius:6px;padding:2px 8px;font-weight:600;white-space:nowrap;">'
        f"{icon} {label}</span>"
    )


# Custom shapes per status (no single emoji matches these precisely):
# green tick in a green square, blue "!" in an amber circle, white cross
# in a red triangle. Built as small inline SVGs for exact control over
# shape/color rather than relying on font-rendered emoji glyphs.
_STATUS_SVG = {
    ScreenerStatus.GREEN: (
        '<svg width="20" height="20" viewBox="0 0 20 20" xmlns="http://www.w3.org/2000/svg">'
        '<rect x="1" y="1" width="18" height="18" rx="2" fill="#0f9d58"/>'
        '<path d="M5 10.3 L8.3 13.6 L15 6.8" stroke="white" stroke-width="2.2" '
        'fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>'
    ),
    ScreenerStatus.AMBER: (
        '<svg width="20" height="20" viewBox="0 0 20 20" xmlns="http://www.w3.org/2000/svg">'
        '<circle cx="10" cy="10" r="9" fill="#f4a623"/>'
        '<rect x="9" y="4.5" width="2" height="7.5" rx="1" fill="#1a56db"/>'
        '<rect x="9" y="13.5" width="2" height="2" rx="1" fill="#1a56db"/></svg>'
    ),
    ScreenerStatus.RED: (
        '<svg width="20" height="20" viewBox="0 0 20 20" xmlns="http://www.w3.org/2000/svg">'
        '<path d="M10 1 L19 18 L1 18 Z" fill="#d93025" stroke="#d93025" stroke-linejoin="round"/>'
        '<path d="M7 9 L13 15 M13 9 L7 15" stroke="white" stroke-width="2" stroke-linecap="round"/></svg>'
    ),
}


def status_dot(status: ScreenerStatus) -> str:
    """Color-coded shape only, no text label -- for compact table cells
    (e.g. the Dashboard screener table) where the row already carries a
    Criteria column and other context. Use status_badge() instead
    wherever the status needs to stand alone (e.g. the Stock Detail
    header), since spelling it out matters more there for accessibility."""
    status = ScreenerStatus(status)
    _color, icon, label = STATUS_STYLE[status]
    svg = _STATUS_SVG.get(status)
    inner = svg if svg else f'<span style="font-size:1.3em;">{icon}</span>'
    return f'<span title="{label}">{inner}</span>'


def market_state_label(state: MarketState) -> str:
    return MARKET_STATE_LABEL[MarketState(state)]


def buy_sell_label(status: ScreenerStatus) -> str:
    return BUY_SELL_LABEL[ScreenerStatus(status)]


def render_disclaimer() -> None:
    st.warning(DISCLAIMER, icon="⚠️")


def plotly_template(theme: Theme | str = Theme.SYSTEM) -> str:
    theme = Theme(theme)
    if theme == Theme.DARK:
        return "plotly_dark"
    return "plotly_white"


# Tailwind's v3+ CDN is JS-based (Play CDN) and relies on a <script> tag
# scanning the DOM at runtime -- but Streamlit's st.markdown(unsafe_allow_html
# =True) inserts HTML via innerHTML, and browsers never execute <script>
# tags inserted that way (a standard DOM security behavior), so the v3
# approach silently does nothing here. A <link rel="stylesheet"> element,
# unlike <script>, IS honored via innerHTML, so we load the older
# fully-precompiled Tailwind v2 static build instead -- no JS execution
# needed, and it covers every utility class used below.
_TAILWIND_CDN_URL = "https://unpkg.com/tailwindcss@2.2.19/dist/tailwind.min.css"


def inject_tailwind() -> None:
    """Call once near the top of a page before rendering any Tailwind-
    classed custom HTML (e.g. render_screener_table). Cheap/idempotent to
    call on every page -- it's just a <link> tag, and Streamlit re-runs
    the whole script on every interaction anyway."""
    st.markdown(f'<link rel="stylesheet" href="{_TAILWIND_CDN_URL}">', unsafe_allow_html=True)


def _table_theme_classes(theme: Theme | str) -> dict[str, str]:
    """Tailwind v2 has no dark: variant in this static build, and we can't
    reliably detect the viewer's actual browser theme from Python, so we
    reuse the same user_settings.theme preference that already drives
    plotly_template() to pick a light or dark palette explicitly."""
    if Theme(theme) == Theme.DARK:
        return {
            "wrapper_border": "border-gray-700",
            "header": "bg-gray-800 text-gray-300",
            "row_odd": "bg-gray-900",
            "row_even": "bg-gray-800",
            "row_hover": "hover:bg-gray-700",
            "cell_text": "text-gray-100",
            "cell_border": "border-gray-700",
            "card": "bg-gray-800 border-gray-700",
            "muted": "text-gray-400",
        }
    return {
        "wrapper_border": "border-gray-200",
        "header": "bg-gray-50 text-gray-600",
        "row_odd": "bg-white",
        "row_even": "bg-gray-50",
        "row_hover": "hover:bg-blue-50",
        "cell_text": "text-gray-800",
        "cell_border": "border-gray-100",
        "card": "bg-white border-gray-200",
        "muted": "text-gray-500",
    }


def render_screener_table(rows: list[dict], theme: Theme | str = Theme.SYSTEM) -> str:
    """Tailwind-classed HTML for the Dashboard screener table: a normal
    table on tablet/desktop (md: and up, >=768px), and a stacked list of
    cards on phones (below md:) -- CSS-only responsive switch (`hidden
    md:block` / `md:hidden`), no JS. Column values in `rows` are already
    pre-formatted strings, some containing raw HTML (status icons,
    pass/fail marks); `Symbol` is carried for the caller's own use (e.g. a
    selectbox below the table) and is not rendered here.

    Fixes the real mobile problem this Dashboard had: the previous plain
    `df.to_html()` table had no responsive handling at all -- on a narrow
    viewport it either overflowed the page or squeezed unreadably. The
    desktop table below is also wrapped in `overflow-x-auto` as a safety
    net even at wider sizes.
    """
    c = _table_theme_classes(theme)
    if not rows:
        return ""

    columns = [k for k in rows[0] if k != "Symbol"]

    header_cells = "".join(
        f'<th class="px-3 py-2 text-left text-xs font-semibold uppercase tracking-wide '
        f'whitespace-nowrap border-b {c["wrapper_border"]}">{col}</th>'
        for col in columns
    )
    body_rows = []
    for i, row in enumerate(rows):
        stripe = c["row_odd"] if i % 2 == 0 else c["row_even"]
        cells = "".join(
            f'<td class="px-3 py-2 text-sm whitespace-nowrap border-b {c["cell_border"]}">{row[col]}</td>'
            for col in columns
        )
        body_rows.append(f'<tr class="{stripe} {c["row_hover"]} {c["cell_text"]}">{cells}</tr>')

    table_html = f"""
    <div class="hidden md:block w-full overflow-x-auto rounded-lg border {c['wrapper_border']}">
      <table class="min-w-full">
        <thead class="{c['header']}"><tr>{header_cells}</tr></thead>
        <tbody>{''.join(body_rows)}</tbody>
      </table>
    </div>
    """

    card_fields = [col for col in columns if col not in ("#", "Stock", "Status")]
    cards = []
    for row in rows:
        stat_pairs = "".join(
            f'<div class="{c["muted"]} text-xs">{col}</div><div class="{c["cell_text"]} text-sm mb-1">{row[col]}</div>'
            for col in card_fields
        )
        # Built as one continuous line, not a multi-line/indented f-string
        # -- joining indented multi-line card blocks left a whitespace-only
        # line between each pair of cards, which Streamlit's markdown
        # renderer treats as a blank line, ending the HTML block early.
        # Everything after the first card then got parsed as an indented
        # code block and shown as literal `<div>` text instead of being
        # rendered (only reproduced on narrow/mobile viewports, since the
        # desktop table's <tr> rows are built the same single-line way and
        # never had this problem).
        cards.append(
            f'<div class="rounded-lg border {c["card"]} p-3 shadow-sm">'
            f'<div class="flex items-center justify-between gap-2 mb-2">'
            f'<span class="{c["cell_text"]} font-semibold text-sm">#{row["#"]} {row["Stock"]}</span>'
            f'<span class="shrink-0">{row["Status"]}</span>'
            f"</div>"
            f'<div class="grid grid-cols-2 gap-x-3">{stat_pairs}</div>'
            f"</div>"
        )

    cards_html = f'<div class="md:hidden flex flex-col gap-3">{"".join(cards)}</div>'

    return table_html + cards_html
