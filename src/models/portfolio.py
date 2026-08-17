from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict

from src.models.enums import OptionType


class PortfolioHolding(BaseModel):
    """One saved row from a broker CSV upload (see portfolio_service.py).
    `symbol` is None when the uploaded instrument name couldn't be
    matched to any known company -- the row is still saved and shown,
    just with an N/A valuation, until the user supplies a symbol."""

    model_config = ConfigDict(from_attributes=True)

    user_id: str
    portfolio_name: str
    broker: str
    raw_name: str
    symbol: str | None = None
    qty: float
    avg_price: float
    investment: float
    uploaded_at: datetime | None = None


class PortfolioPosition(BaseModel):
    """One saved row from a broker's F&O *positions* export (see
    portfolio_service.py's parse_zerodha_positions_csv/parse_dhan_positions_csv).
    `qty` keeps its broker-reported sign -- negative is short, positive is
    long -- since option P&L direction depends on it. `symbol`/`expiry_date`/
    `strike_price`/`option_type` are None when the instrument string couldn't
    be decoded (e.g. a contract format not covered by the parser); the row
    is still saved and shown, just with no contract detail."""

    model_config = ConfigDict(from_attributes=True)

    user_id: str
    portfolio_name: str
    broker: str
    raw_name: str
    symbol: str | None = None
    expiry_date: date | None = None
    strike_price: float | None = None
    option_type: OptionType | None = None
    qty: float
    avg_price: float
    ltp: float | None = None
    uploaded_at: datetime | None = None


class PortfolioTradeGroup(BaseModel):
    """Manual override of which "Trade" one holding or F&O position leg
    belongs to (see pages/7_My_Trades.py and pages/10_Analyse_Trade.py) --
    keyed by the leg's own (portfolio_name, broker, raw_name), the same
    natural identity a broker's export already gives each contract, not
    any database row id. Only legs the user has manually combined/split
    away from the default grouping (one Trade per underlying symbol) get
    a row here -- see supabase/migrations/0020_portfolio_trade_groups.sql
    for why that makes this survive replace_broker_holdings/
    replace_broker_positions's delete+reinsert full sync. See also
    PortfolioTradeMeta, which stores the separate per-trade underlying-
    label/trade-type overrides this table has no room for."""

    model_config = ConfigDict(from_attributes=True)

    user_id: str
    portfolio_name: str
    broker: str
    raw_name: str
    trade_id: str
    updated_at: datetime | None = None


class PortfolioTradeMeta(BaseModel):
    """Trade-level overrides for the "My Trades" / "Analyse Trade" pages --
    keyed by (portfolio_name, trade_id), a different grain from
    PortfolioTradeGroup's per-LEG (broker, raw_name) key (many legs share
    one trade_id). `underlying_label` is free text, not constrained to a
    known symbol -- e.g. correcting a resolved "Tata Motors" to the actual
    post-demerger underlying "Tata Motors Passenger Vehicle"; None means
    "use the auto-computed default" (the joined distinct underlying
    symbols across the trade's own legs). `trade_type` is also free text,
    defaulting to "Trade". `bucket_override` (one of "stock"/"index"/
    "other", or None) lets the user manually pin which My Trades table a
    trade sorts into -- None means "use classify_underlying_bucket's
    computed default" (see supabase/migrations/0024_portfolio_trade_meta_bucket_override.sql
    for why this exists: an ETF's company_type no longer auto-counts as
    "index" the way a real Index does, so a trade that genuinely belongs
    in Index Trades despite being an ETF needs this manual escape hatch).
    See supabase/migrations/0021_portfolio_trade_meta.sql for why a trade
    renamed via merge loses its old meta row rather than carrying it
    forward."""

    model_config = ConfigDict(from_attributes=True)

    user_id: str
    portfolio_name: str
    trade_id: str
    underlying_label: str | None = None
    trade_type: str = "Trade"
    bucket_override: str | None = None
    updated_at: datetime | None = None


class BrokerConnection(BaseModel):
    """Saved API credentials letting one portfolio pull holdings/positions
    directly from a broker's API (see src/data_providers/dhan_provider.py/
    zerodha_provider.py and pages/6_My_Broker.py's "Connect Dhan account"/
    "Connect Zerodha account" flows) instead of a CSV upload. `client_id`
    holds Dhan's actual client ID for a Dhan row, or Zerodha's `api_key`
    for a Zerodha row -- both are "this connection's primary identifying
    credential", so one column serves both brokers. `access_token` is
    stored as given -- see supabase/migrations/0017_broker_connections.sql's
    docstring for the plaintext/RLS-only trade-off this implies -- and is
    `None` for a Zerodha row that has saved its api_key/api_secret but not
    yet completed the Kite Connect login redirect (see
    supabase/migrations/0022_broker_connections_api_secret.sql). `api_secret`
    is Zerodha-only (Dhan's flow needs no third credential); always `None`
    for a Dhan row. `token_saved_at` is set only when credentials are
    saved/updated (not on every sync), so the UI can warn once it's old
    enough that Dhan's 24-hour token is likely expired -- for Zerodha,
    whose token instead expires at a fixed daily time (~6am IST) rather
    than a rolling window, the UI checks against that fixed time instead."""

    model_config = ConfigDict(from_attributes=True)

    user_id: str
    portfolio_name: str
    broker: str
    client_id: str
    access_token: str | None = None
    api_secret: str | None = None
    token_saved_at: datetime | None = None
