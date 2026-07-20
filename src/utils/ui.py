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

# Tailwind's stock indigo palette (present in the v2 default build used
# below) -- kept as one source of truth so hand-rendered HTML classes
# (bg-indigo-600) and the CSS override (var(--accent-600)) stay visually
# identical. Deliberately separate from STATUS_STYLE above: those colors
# are domain-meaningful (Green/Amber/Red/Unavailable classification) and
# are never touched by the accent/design-system work below.
ACCENT = {
    50: "#eef2ff", 100: "#e0e7ff", 200: "#c7d2fe", 300: "#a5b4fc", 400: "#818cf8",
    500: "#6366f1", 600: "#4f46e5", 700: "#4338ca", 800: "#3730a3", 900: "#312e81",
}


def status_badge(status: ScreenerStatus) -> str:
    color, icon, label = STATUS_STYLE[ScreenerStatus(status)]
    return (
        f'<span style="background-color:{color}22;color:{color};border:1px solid {color};'
        f'border-radius:6px;padding:2px 8px;font-weight:600;white-space:nowrap;">'
        f"{icon} {label}</span>"
    )


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


# Tailwind only reaches HTML we hand-render via unsafe_allow_html -- it
# has zero reach into Streamlit's own native React-rendered widgets
# (buttons, inputs, forms, sidebar, tabs, st.metric, st.dataframe,
# st.expander). To make the whole app feel like one cohesive design
# system rather than "one nice Tailwind table surrounded by default gray
# Streamlit chrome," this global <style> override reskins native widgets
# using the same ACCENT palette. Every selector below was confirmed
# empirically against the actually-installed Streamlit version (1.59.1)
# via live DOM inspection, not assumed from older-version documentation
# -- Streamlit's internal class names are emotion-cache hashes that
# change across builds and are NOT safe to target; only data-testid
# attributes, ARIA roles/attributes, and the `kind` attribute Streamlit
# puts on <button> elements are stable across reruns/builds, so those are
# exclusively what's used here.
#
# Unlike the join-bug-prone <div> fragments elsewhere in this file, a
# <style> block is CommonMark "HTML block type 1" -- terminated only by
# its own closing tag, not by a blank/whitespace-only line -- so it's
# safe to write as one big multi-line triple-quoted string, exactly like
# inject_tailwind()'s existing single <link> call.
_GLOBAL_CSS_LIGHT = f"""
<style>
:root {{
  --accent-50:{ACCENT[50]}; --accent-100:{ACCENT[100]}; --accent-600:{ACCENT[600]};
  --accent-700:{ACCENT[700]}; --accent-800:{ACCENT[800]};
}}
button[kind="secondary"], button[kind="secondaryFormSubmit"], [data-testid="stDownloadButton"] button {{
  border-radius:8px !important; border:1px solid var(--accent-600) !important;
  color:var(--accent-700) !important; background:#ffffff !important; font-weight:600 !important;
}}
button[kind="secondary"]:hover, button[kind="secondaryFormSubmit"]:hover, [data-testid="stDownloadButton"] button:hover {{
  background:var(--accent-50) !important; border-color:var(--accent-700) !important; color:var(--accent-800) !important;
}}
button[kind="primary"] {{ background:var(--accent-600) !important; border-color:var(--accent-600) !important; color:#ffffff !important; }}
button[kind="primary"]:hover {{ background:var(--accent-700) !important; border-color:var(--accent-700) !important; }}
[data-testid="stTextInput"] input, [data-testid="stNumberInputContainer"],
[data-testid="stTextArea"] textarea, [data-testid="stSelectbox"] [role="group"], [data-testid="stMultiSelect"] {{
  border-radius:8px !important; border-color:#d1d5db !important;
}}
[data-testid="stTextInput"] input:focus, [data-testid="stTextArea"] textarea:focus {{
  border-color:var(--accent-600) !important; box-shadow:0 0 0 1px var(--accent-600) !important;
}}
[data-testid="stSidebar"] {{ background:#f5f5f8; border-right:1px solid #e5e7eb; }}
[data-testid="stTabs"] {{ border-bottom:1px solid #e5e7eb; }}
[data-testid="stTab"][aria-selected="true"] {{ color:var(--accent-700) !important; border-bottom:2px solid var(--accent-600) !important; }}
[data-testid="stForm"] {{ border:1px solid #e5e7eb !important; border-radius:12px !important; padding:1.25rem !important; background:#ffffff; }}
[data-testid="stExpander"] {{ border:1px solid #e5e7eb !important; border-radius:10px !important; }}
[data-testid="stMetricValue"] {{ color:var(--accent-700) !important; }}
[data-testid="stCheckbox"] input, [data-testid="stRadioOption"] input {{ accent-color:var(--accent-600); }}
[data-testid="stDataFrame"] {{ border:1px solid #e5e7eb; border-radius:8px; overflow:hidden; }}
[data-testid="stHeading"] h1, [data-testid="stHeading"] h2, [data-testid="stHeading"] h3 {{ color:#1f2937; }}
</style>
"""

# Dark variant additionally overrides the top-level app/sidebar/heading
# containers, since .streamlit/config.toml's [theme] section can only
# express one static base (light) -- without this, "dark" would leave
# dark-styled widgets floating on Streamlit's own light page background.
_GLOBAL_CSS_DARK = f"""
<style>
:root {{
  --accent-50:{ACCENT[900]}; --accent-100:{ACCENT[800]}; --accent-600:{ACCENT[600]};
  --accent-700:{ACCENT[500]}; --accent-800:{ACCENT[400]};
}}
[data-testid="stAppViewContainer"], [data-testid="stMain"], [data-testid="stHeader"] {{
  background:#111827 !important; color:#f3f4f6 !important;
}}
[data-testid="stSidebar"] {{ background:#1f2937 !important; border-right:1px solid #374151; }}
[data-testid="stSidebarContent"] {{ color:#f3f4f6 !important; }}
[data-testid="stHeading"] h1, [data-testid="stHeading"] h2, [data-testid="stHeading"] h3 {{ color:#f3f4f6 !important; }}
button[kind="secondary"], button[kind="secondaryFormSubmit"], [data-testid="stDownloadButton"] button {{
  border-radius:8px !important; border:1px solid var(--accent-600) !important;
  color:#e0e7ff !important; background:#1f2937 !important; font-weight:600 !important;
}}
button[kind="secondary"]:hover, button[kind="secondaryFormSubmit"]:hover, [data-testid="stDownloadButton"] button:hover {{
  background:var(--accent-50) !important; border-color:var(--accent-700) !important; color:#ffffff !important;
}}
button[kind="primary"] {{ background:var(--accent-600) !important; border-color:var(--accent-600) !important; color:#ffffff !important; }}
button[kind="primary"]:hover {{ background:var(--accent-700) !important; border-color:var(--accent-700) !important; }}
[data-testid="stTextInput"] input, [data-testid="stNumberInputContainer"],
[data-testid="stTextArea"] textarea, [data-testid="stSelectbox"] [role="group"], [data-testid="stMultiSelect"] {{
  border-radius:8px !important; border-color:#4b5563 !important; background:#1f2937 !important; color:#f3f4f6 !important;
}}
[data-testid="stTextInput"] input:focus, [data-testid="stTextArea"] textarea:focus {{
  border-color:var(--accent-600) !important; box-shadow:0 0 0 1px var(--accent-600) !important;
}}
[data-testid="stTabs"] {{ border-bottom:1px solid #374151; }}
[data-testid="stTab"][aria-selected="true"] {{ color:#e0e7ff !important; border-bottom:2px solid var(--accent-600) !important; }}
[data-testid="stForm"] {{ border:1px solid #374151 !important; border-radius:12px !important; padding:1.25rem !important; background:#1f2937 !important; }}
[data-testid="stExpander"] {{ border:1px solid #374151 !important; border-radius:10px !important; background:#1f2937 !important; }}
[data-testid="stMetricValue"] {{ color:#e0e7ff !important; }}
[data-testid="stCheckbox"] input, [data-testid="stRadioOption"] input {{ accent-color:var(--accent-600); }}
[data-testid="stDataFrame"] {{ border:1px solid #374151; border-radius:8px; overflow:hidden; }}
</style>
"""


def inject_global_styles(theme: Theme | str = Theme.LIGHT) -> None:
    """The native-widget half of the design system -- see the comment
    above _GLOBAL_CSS_LIGHT for why this exists and why every selector is
    testid/ARIA/attribute-based rather than class-based."""
    st.markdown(_GLOBAL_CSS_DARK if Theme(theme) == Theme.DARK else _GLOBAL_CSS_LIGHT, unsafe_allow_html=True)


def inject_design_system(theme: Theme | str = Theme.LIGHT) -> None:
    """Call once per rerun before any page content -- combines the
    Tailwind CDN link (for hand-rendered HTML) with the native-widget CSS
    override (for everything Tailwind can't reach). Idempotent/cheap to
    call repeatedly, same as inject_tailwind() alone always was."""
    inject_tailwind()
    inject_global_styles(theme)


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
        "row_hover": "hover:bg-indigo-50",
        "cell_text": "text-gray-800",
        "cell_border": "border-gray-100",
        "card": "bg-white border-gray-200",
        "muted": "text-gray-500",
    }


def _surface_classes(theme: Theme | str) -> dict[str, str]:
    """Same explicit-branch-on-Theme pattern as _table_theme_classes(),
    for the generic card/pill/stat-tile components below rather than the
    screener table specifically."""
    if Theme(theme) == Theme.DARK:
        return {
            "card_bg": "bg-gray-800", "card_border": "border-gray-700", "card_text": "text-gray-100",
            "muted": "text-gray-400", "pill_neutral_bg": "bg-gray-700", "pill_neutral_text": "text-gray-300",
            "pill_accent_bg": "bg-indigo-900", "pill_accent_text": "text-indigo-200",
            "pill_accent_border": "border-indigo-700",
        }
    return {
        "card_bg": "bg-white", "card_border": "border-gray-200", "card_text": "text-gray-800",
        "muted": "text-gray-500", "pill_neutral_bg": "bg-gray-100", "pill_neutral_text": "text-gray-600",
        "pill_accent_bg": "bg-indigo-50", "pill_accent_text": "text-indigo-700",
        "pill_accent_border": "border-indigo-200",
    }


def render_card(inner_html: str, theme: Theme | str = Theme.SYSTEM, *, extra_classes: str = "") -> str:
    """Generic bordered/padded/shadowed wrapper for static content only
    -- never wrap a native widget's output in this. Streamlit's native
    widgets and hand-rendered HTML are DOM siblings, never nested; a
    st.markdown() call's HTML can never "contain" a later st.button()/
    st.form() call's rendered output."""
    c = _surface_classes(theme)
    return f'<div class="rounded-lg border {c["card_border"]} {c["card_bg"]} {c["card_text"]} p-4 shadow-sm {extra_classes}">{inner_html}</div>'


def render_pill(text: str, tone: str = "accent", theme: Theme | str = Theme.SYSTEM) -> str:
    """Small badge/pill -- alert-type labels, "coming soon" tags, active
    filter indicators. tone="accent" uses the indigo palette, "neutral"
    uses gray."""
    c = _surface_classes(theme)
    if tone == "accent":
        bg, txt, border = c["pill_accent_bg"], c["pill_accent_text"], c["pill_accent_border"]
    else:
        bg, txt, border = c["pill_neutral_bg"], c["pill_neutral_text"], "border-transparent"
    return f'<span class="inline-block {bg} {txt} border {border} rounded-full px-2.5 py-0.5 text-xs font-semibold whitespace-nowrap">{text}</span>'


def render_stat_tile(label: str, value: str, caption: str | None = None, theme: Theme | str = Theme.SYSTEM) -> str:
    c = _surface_classes(theme)
    cap = f'<div class="{c["muted"]} text-xs mt-0.5">{caption}</div>' if caption else ""
    return f'<div class="{c["card_bg"]} border {c["card_border"]} rounded-lg p-3"><div class="{c["muted"]} text-xs uppercase tracking-wide">{label}</div><div class="{c["card_text"]} text-lg font-semibold">{value}</div>{cap}</div>'


def render_stat_grid(stats: list[tuple[str, str, str | None]], theme: Theme | str = Theme.SYSTEM, cols: int = 2) -> str:
    """`stats` is a list of (label, value, caption) tuples. Responsive:
    one column below the 768px breakpoint, `cols` columns at/above it --
    same md: breakpoint render_screener_table() already established."""
    tiles = "".join(render_stat_tile(label, value, caption, theme) for label, value, caption in stats)
    return f'<div class="grid grid-cols-1 md:grid-cols-{cols} gap-3">{tiles}</div>'


def render_alert_row(alert_type_label: str, config_summary: str, cooldown_minutes: int, is_active: bool, theme: Theme | str = Theme.SYSTEM) -> str:
    """Formatted alert summary line -- replaces raw Python-dict-dump text
    previously shown on both Stock Detail and the Alerts page."""
    c = _surface_classes(theme)
    pill = render_pill(alert_type_label, tone="accent", theme=theme)
    inactive = "" if is_active else f' <span class="{c["muted"]} italic">(inactive)</span>'
    return f'<div class="flex flex-wrap items-center gap-2">{pill}<span class="{c["card_text"]} text-sm">{config_summary}</span><span class="{c["muted"]} text-xs">· cooldown {cooldown_minutes}min</span>{inactive}</div>'


def render_screener_table(
    rows: list[dict],
    theme: Theme | str = Theme.SYSTEM,
    sortable_columns: dict[str, str] | None = None,
    active_sort_key: str | None = None,
    sort_desc: bool = False,
) -> str:
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

    `sortable_columns` maps a column label to the underlying sort key the
    caller understands, purely to decide which header gets the ▲/▼ arrow
    for `active_sort_key`/`sort_desc` -- headers are NOT clickable links.
    An `<a href="?sort=...">` was tried and reverted: this table is
    hand-rendered HTML with no JS bridge back to Python, so a click would
    have to be a real browser navigation -- but this app deliberately
    keeps the Supabase auth session only in `st.session_state` (never a
    cookie/localStorage, see session.py's docstring), so any real
    navigation starts a brand-new, logged-out session. The actual sort
    interaction is native `st.button()`s rendered next to this table
    (Dashboard's sort-button row), which stay on the same WebSocket
    session; this function just mirrors their state visually.
    """
    c = _table_theme_classes(theme)
    if not rows:
        return ""

    columns = [k for k in rows[0] if k != "Symbol"]
    sortable_columns = sortable_columns or {}

    def _header_cell(col: str) -> str:
        key = sortable_columns.get(col)
        is_active = key is not None and key == active_sort_key
        arrow = (" ▼" if sort_desc else " ▲") if is_active else ""
        return (
            f'<th class="px-3 py-2 text-left text-xs font-semibold uppercase tracking-wide '
            f'whitespace-nowrap border-b {c["wrapper_border"]}">{col}{arrow}</th>'
        )

    header_cells = "".join(_header_cell(col) for col in columns)
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
        status_span = f'<span class="shrink-0">{row["Status"]}</span>' if "Status" in row else ""
        cards.append(
            f'<div class="rounded-lg border {c["card"]} p-3 shadow-sm">'
            f'<div class="flex items-center justify-between gap-2 mb-2">'
            f'<span class="{c["cell_text"]} font-semibold text-sm">#{row["#"]} {row["Stock"]}</span>'
            f"{status_span}"
            f"</div>"
            f'<div class="grid grid-cols-2 gap-x-3">{stat_pairs}</div>'
            f"</div>"
        )

    cards_html = f'<div class="md:hidden flex flex-col gap-3">{"".join(cards)}</div>'

    return table_html + cards_html
