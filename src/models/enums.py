from __future__ import annotations

from enum import StrEnum


class ScreenerStatus(StrEnum):
    GREEN = "green"
    AMBER = "amber"
    RED = "red"
    UNAVAILABLE = "unavailable"


class MarketState(StrEnum):
    PRE_OPEN = "pre_open"
    OPEN = "open"
    CLOSED = "closed"
    DATA_DELAYED = "data_delayed"


class DividendType(StrEnum):
    INTERIM = "interim"
    FINAL = "final"
    SPECIAL = "special"


class AlertType(StrEnum):
    STATUS_CHANGE = "status_change"
    ENTERS_GREEN = "enters_green"
    LEAVES_GREEN = "leaves_green"
    PRICE_CROSS = "price_cross"
    MOMENTUM_CROSS = "momentum_cross"
    DIVIDEND_YIELD_CROSS = "dividend_yield_cross"
    PEG_CROSS = "peg_cross"
    BUY_WATCH = "buy_watch"
    SELL_WATCH = "sell_watch"
    REFRESH_FAILURE = "refresh_failure"


class NotificationChannel(StrEnum):
    IN_APP = "in_app"
    EMAIL = "email"
    TELEGRAM = "telegram"
    SLACK = "slack"
    BROWSER_PUSH = "browser_push"


class Theme(StrEnum):
    LIGHT = "light"
    DARK = "dark"
    SYSTEM = "system"


class FetchType(StrEnum):
    PRICE = "price"
    INTRADAY_PRICE = "intraday_price"
    FUNDAMENTALS = "fundamentals"
    DIVIDEND = "dividend"
    CONSTITUENTS = "constituents"


class FetchStatus(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
