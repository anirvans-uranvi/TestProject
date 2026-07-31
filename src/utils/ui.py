"""Shared Streamlit UI fragments: status badges, disclaimer, chart theming."""
from __future__ import annotations

import streamlit as st

from src.models.enums import MarketState, ScreenerStatus, Theme

STATUS_STYLE = {
    ScreenerStatus.GREEN: ("#059669", "🟢", "Green"),  # emerald-600
    ScreenerStatus.AMBER: ("#f59e0b", "🟠", "Amber"),  # amber-500
    ScreenerStatus.RED: ("#dc2626", "🔴", "Red"),  # red-600
    ScreenerStatus.UNAVAILABLE: ("#94a3b8", "⚪", "Unavailable"),  # slate-400
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

# "Classic Institutional" palette: Tailwind's stock slate scale (present
# in the v2 default build used below) as the one dominant/primary color
# -- kept as one source of truth so hand-rendered HTML classes
# (bg-slate-900) and the CSS override (var(--accent-900)) stay visually
# identical. Deliberately separate from STATUS_STYLE above: those colors
# are domain-meaningful (Green/Amber/Red/Unavailable classification) and
# are never touched by the accent/design-system work below. Slate is the
# primary/branding color -- render_pill()'s "positive"/"negative" tones
# (emerald/red, via _surface_classes) are the only gains/losses colors,
# and are never used for branding or primary actions.
ACCENT = {
    50: "#f8fafc", 100: "#f1f5f9", 200: "#e2e8f0", 300: "#cbd5e1", 400: "#94a3b8",
    500: "#64748b", 600: "#475569", 700: "#334155", 800: "#1e293b", 900: "#0f172a",
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
    classed custom HTML (e.g. render_stat_grid). Cheap/idempotent to
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
  --accent-50:{ACCENT[50]}; --accent-100:{ACCENT[100]}; --accent-200:{ACCENT[200]};
  --accent-300:{ACCENT[300]}; --accent-800:{ACCENT[800]}; --accent-900:{ACCENT[900]};
}}
button[kind="secondary"], button[kind="secondaryFormSubmit"], [data-testid="stDownloadButton"] button {{
  border-radius:8px !important; border:1px solid var(--accent-200) !important;
  color:var(--accent-800) !important; background:var(--accent-100) !important; font-weight:600 !important;
}}
button[kind="secondary"]:hover, button[kind="secondaryFormSubmit"]:hover, [data-testid="stDownloadButton"] button:hover {{
  background:var(--accent-200) !important; border-color:var(--accent-300) !important; color:var(--accent-900) !important;
}}
button[kind="primary"] {{ background:var(--accent-900) !important; border-color:var(--accent-900) !important; color:#ffffff !important; }}
button[kind="primary"]:hover {{ background:var(--accent-800) !important; border-color:var(--accent-800) !important; }}
[data-testid="stTextInput"] input, [data-testid="stNumberInputContainer"],
[data-testid="stTextArea"] textarea, [data-testid="stSelectbox"] [role="group"], [data-testid="stMultiSelect"] {{
  border-radius:8px !important; border-color:var(--accent-300) !important;
}}
[data-testid="stTextInput"] input:focus, [data-testid="stTextArea"] textarea:focus {{
  border-color:var(--accent-900) !important; box-shadow:0 0 0 1px var(--accent-900) !important;
}}
[data-testid="stSidebar"] {{ background:var(--accent-50); border-right:1px solid var(--accent-200); }}
[data-testid="stTabs"] {{ border-bottom:1px solid var(--accent-200); }}
[data-testid="stTab"][aria-selected="true"] {{ color:var(--accent-900) !important; border-bottom:2px solid var(--accent-900) !important; }}
[data-testid="stForm"] {{ border:1px solid var(--accent-200) !important; border-radius:12px !important; padding:1.25rem !important; background:#ffffff; }}
[data-testid="stExpander"] {{ border:1px solid var(--accent-200) !important; border-radius:10px !important; }}
[data-testid="stMetricValue"] {{ color:var(--accent-900) !important; }}
[data-testid="stCheckbox"] input, [data-testid="stRadioOption"] input {{ accent-color:var(--accent-900); }}
[data-testid="stDataFrame"] {{ border:1px solid var(--accent-200); border-radius:8px; overflow:hidden; }}
[data-testid="stHeading"] h1, [data-testid="stHeading"] h2, [data-testid="stHeading"] h3 {{ color:var(--accent-900); }}
</style>
"""

# Dark variant additionally overrides the top-level app/sidebar/heading
# containers, since .streamlit/config.toml's [theme] section can only
# express one static base (light) -- without this, "dark" would leave
# dark-styled widgets floating on Streamlit's own light page background.
_GLOBAL_CSS_DARK = f"""
<style>
:root {{
  --accent-50:{ACCENT[900]}; --accent-100:{ACCENT[800]}; --accent-200:{ACCENT[700]};
  --accent-300:{ACCENT[600]}; --accent-800:{ACCENT[100]}; --accent-900:{ACCENT[50]};
}}
[data-testid="stAppViewContainer"], [data-testid="stMain"], [data-testid="stHeader"] {{
  background:{ACCENT[900]} !important; color:{ACCENT[100]} !important;
}}
[data-testid="stSidebar"] {{ background:{ACCENT[800]} !important; border-right:1px solid var(--accent-200); }}
[data-testid="stSidebarContent"] {{ color:{ACCENT[100]} !important; }}
[data-testid="stHeading"] h1, [data-testid="stHeading"] h2, [data-testid="stHeading"] h3 {{ color:{ACCENT[100]} !important; }}
button[kind="secondary"], button[kind="secondaryFormSubmit"], [data-testid="stDownloadButton"] button {{
  border-radius:8px !important; border:1px solid var(--accent-200) !important;
  color:{ACCENT[100]} !important; background:var(--accent-100) !important; font-weight:600 !important;
}}
button[kind="secondary"]:hover, button[kind="secondaryFormSubmit"]:hover, [data-testid="stDownloadButton"] button:hover {{
  background:var(--accent-200) !important; border-color:var(--accent-300) !important; color:#ffffff !important;
}}
button[kind="primary"] {{ background:var(--accent-900) !important; border-color:var(--accent-900) !important; color:{ACCENT[900]} !important; }}
button[kind="primary"]:hover {{ background:var(--accent-800) !important; border-color:var(--accent-800) !important; }}
[data-testid="stTextInput"] input, [data-testid="stNumberInputContainer"],
[data-testid="stTextArea"] textarea, [data-testid="stSelectbox"] [role="group"], [data-testid="stMultiSelect"] {{
  border-radius:8px !important; border-color:var(--accent-300) !important; background:var(--accent-100) !important; color:{ACCENT[100]} !important;
}}
[data-testid="stTextInput"] input:focus, [data-testid="stTextArea"] textarea:focus {{
  border-color:var(--accent-900) !important; box-shadow:0 0 0 1px var(--accent-900) !important;
}}
[data-testid="stTabs"] {{ border-bottom:1px solid var(--accent-200); }}
[data-testid="stTab"][aria-selected="true"] {{ color:#ffffff !important; border-bottom:2px solid var(--accent-900) !important; }}
[data-testid="stForm"] {{ border:1px solid var(--accent-200) !important; border-radius:12px !important; padding:1.25rem !important; background:var(--accent-100) !important; }}
[data-testid="stExpander"] {{ border:1px solid var(--accent-200) !important; border-radius:10px !important; background:var(--accent-100) !important; }}
[data-testid="stMetricValue"] {{ color:#ffffff !important; }}
[data-testid="stCheckbox"] input, [data-testid="stRadioOption"] input {{ accent-color:var(--accent-900); }}
[data-testid="stDataFrame"] {{ border:1px solid var(--accent-200); border-radius:8px; overflow:hidden; }}
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


def _surface_classes(theme: Theme | str) -> dict[str, str]:
    """Tailwind v2 has no dark: variant in this static build, and we can't
    reliably detect the viewer's actual browser theme from Python, so we
    reuse the same user_settings.theme preference that already drives
    plotly_template() to pick a light or dark palette explicitly -- for
    the generic card/pill/stat-tile components below."""
    if Theme(theme) == Theme.DARK:
        return {
            "card_bg": "bg-slate-800", "card_border": "border-slate-700", "card_text": "text-slate-100",
            "muted": "text-slate-400", "pill_neutral_bg": "bg-slate-700", "pill_neutral_text": "text-slate-300",
            "pill_accent_bg": "bg-slate-700", "pill_accent_text": "text-slate-100",
            "pill_accent_border": "border-slate-600",
            "pill_positive_bg": "bg-emerald-900", "pill_positive_text": "text-emerald-300",
            "pill_positive_border": "border-emerald-700",
            "pill_negative_bg": "bg-red-900", "pill_negative_text": "text-red-300",
            "pill_negative_border": "border-red-700",
        }
    return {
        "card_bg": "bg-white", "card_border": "border-slate-200", "card_text": "text-slate-800",
        "muted": "text-slate-500", "pill_neutral_bg": "bg-slate-100", "pill_neutral_text": "text-slate-600",
        "pill_accent_bg": "bg-slate-100", "pill_accent_text": "text-slate-700",
        "pill_accent_border": "border-slate-300",
        "pill_positive_bg": "bg-emerald-50", "pill_positive_text": "text-emerald-700",
        "pill_positive_border": "border-emerald-200",
        "pill_negative_bg": "bg-red-50", "pill_negative_text": "text-red-700",
        "pill_negative_border": "border-red-200",
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
    filter indicators, or (tone="positive"/"negative") gains/losses
    callouts. tone="accent" uses the primary slate palette, "neutral"
    uses gray, "positive"/"negative" use emerald/red -- reserved
    exclusively for financial gain/loss indicators, never branding."""
    c = _surface_classes(theme)
    if tone == "positive":
        bg, txt, border = c["pill_positive_bg"], c["pill_positive_text"], c["pill_positive_border"]
    elif tone == "negative":
        bg, txt, border = c["pill_negative_bg"], c["pill_negative_text"], c["pill_negative_border"]
    elif tone == "accent":
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
    one column below the 768px breakpoint, `cols` columns at/above it."""
    tiles = "".join(render_stat_tile(label, value, caption, theme) for label, value, caption in stats)
    return f'<div class="grid grid-cols-1 md:grid-cols-{cols} gap-3">{tiles}</div>'


def render_alert_row(alert_type_label: str, config_summary: str, cooldown_minutes: int, is_active: bool, theme: Theme | str = Theme.SYSTEM) -> str:
    """Formatted alert summary line -- replaces raw Python-dict-dump text
    previously shown on both Stock Detail and the Alerts page."""
    c = _surface_classes(theme)
    pill = render_pill(alert_type_label, tone="accent", theme=theme)
    inactive = "" if is_active else f' <span class="{c["muted"]} italic">(inactive)</span>'
    return f'<div class="flex flex-wrap items-center gap-2">{pill}<span class="{c["card_text"]} text-sm">{config_summary}</span><span class="{c["muted"]} text-xs">· cooldown {cooldown_minutes}min</span>{inactive}</div>'


