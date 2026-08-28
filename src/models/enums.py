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


class OptionType(StrEnum):
    """NSE option right: CE = call, PE = put (as used in the F&O bhavcopy's
    OptnTp column)."""

    CE = "CE"
    PE = "PE"


class CompanyType(StrEnum):
    """`companies.company_type` (migration 0018) -- what kind of instrument
    a symbol is, not just "is this an ETF". EQUITY is the default for every
    real Nifty 50 constituent and portfolio-tracked stock. ETF is set by
    src.services.portfolio_service.looks_like_etf_name() at company-
    registration time. INDEX covers NIFTY/BANKNIFTY/SENSEX, seeded once by
    migration 0018 so Dhan-synced index option positions (see
    pages/6_My_Broker.py) and this app's F&O ingestion (widened to index
    options, src/data_providers/nse_fo_provider.py) have a companies row to
    reference. FUND has no rows yet -- reserved for a future non-ETF fund
    classification (see looks_like_etf_name()'s own docstring for why
    LIQUIDCASE/GILT5YBEES/LTGILTCASE stay classified as ETF for now rather
    than being split out). latest_screener_view (0018) only ever shows
    EQUITY rows -- ETF/INDEX/FUND are excluded by construction, not a
    separate flag."""

    EQUITY = "Equity"
    ETF = "ETF"
    INDEX = "Index"
    FUND = "Fund"


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
    ALL = "all"  # legacy -- logged by the on-demand manual-refresh Edge
    # Function back when it bundled price+dividend+fundamentals+screener
    # into one invocation; that function now logs PRICE or FUNDAMENTALS
    # per its `mode`, same as the Python cron path's separate --mode
    # values, but old rows with this value still exist
    FO = "fo"  # logged by the on-demand fo-refresh Edge Function
    PORTFOLIO_SYNC = "portfolio_sync"  # logged by a broker portfolio sync
    # (Settings' "Save & Sync"/"Update credentials", or the Portfolio
    # Refresh button on My Trades/My Holdings/My Positions/My CSP -- see
    # src/utils/data_provider_settings.py's sync_broker_portfolio)
    DHAN_INSTRUMENT_MASTER = "dhan_instrument_master"  # logged when
    # src/data_providers/dhan_provider.py's instrument-master loaders
    # download+persist a fresh shared copy to dhan_equity_instruments/
    # dhan_fo_instruments -- provider_name is "equity" or "fo"
    TRADE_HISTORY_SYNC = "trade_history_sync"  # logged by Settings'
    # "Sync Trade History from Dhan" button (src/utils/data_provider_settings.py's
    # _render_dhan_trade_history_sync), pulling GET /v2/trades into
    # portfolio_trade_fills -- separate from PORTFOLIO_SYNC since it's an
    # independent, differently-paced sync (append-only fills, not a
    # current-state snapshot)


class FetchStatus(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
