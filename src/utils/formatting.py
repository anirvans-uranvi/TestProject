from __future__ import annotations


def _group_indian(int_str: str) -> str:
    if len(int_str) <= 3:
        return int_str
    last3, rest = int_str[-3:], int_str[:-3]
    parts = []
    while len(rest) > 2:
        parts.insert(0, rest[-2:])
        rest = rest[:-2]
    if rest:
        parts.insert(0, rest)
    return ",".join(parts) + "," + last3


def format_inr(value: float | None, decimals: int = 2) -> str:
    if value is None:
        return "—"
    sign = "-" if value < 0 else ""
    value = abs(value)
    int_part = int(value)
    int_str = _group_indian(str(int_part))
    if decimals <= 0:
        return f"{sign}₹{int_str}"
    frac_str = f"{value:.{decimals}f}".split(".")[1]
    return f"{sign}₹{int_str}.{frac_str}"


def format_crores(value_in_rupees: float | None) -> str:
    if value_in_rupees is None:
        return "—"
    crores = value_in_rupees / 1e7
    return f"₹{crores:,.0f} Cr"


def format_pct(value: float | None, decimals: int = 2, signed: bool = True) -> str:
    if value is None:
        return "—"
    sign = "+" if signed and value > 0 else ""
    return f"{sign}{value:.{decimals}f}%"


def direction_arrow(value: float | None) -> str:
    if value is None:
        return "—"
    if value > 0:
        return "▲"
    if value < 0:
        return "▼"
    return "▬"


def pass_fail_badge(value: bool | None) -> str:
    if value is None:
        return "N/A"
    return "✅ Pass" if value else "❌ Fail"


def pass_fail_icon(value: bool | None) -> str:
    """Same pass/fail signal as pass_fail_badge() but symbol-only (no
    'Pass'/'Fail' text), for compact table cells."""
    if value is None:
        return "—"
    return "✅" if value else "❌"
