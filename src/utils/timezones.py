from __future__ import annotations

from datetime import datetime

import pytz

IST = pytz.timezone("Asia/Kolkata")


def now_ist() -> datetime:
    return datetime.now(IST)


def to_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = pytz.utc.localize(dt)
    return dt.astimezone(IST)


def format_ist(dt: datetime | None, fmt: str = "%d %b %Y, %I:%M %p IST") -> str:
    if dt is None:
        return "—"
    return to_ist(dt).strftime(fmt)
