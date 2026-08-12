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
    """Manual override of which "Trade" one F&O position leg belongs to
    (see pages/6_Portfolio.py's Positions section) -- keyed by the leg's
    own (portfolio_name, broker, raw_name), the same natural identity a
    broker's export already gives each contract, not any database row id.
    Only legs the user has manually combined/split away from the default
    grouping (one Trade per underlying symbol) get a row here -- see
    supabase/migrations/0020_portfolio_trade_groups.sql for why that makes
    this survive replace_broker_positions's delete+reinsert full sync."""

    model_config = ConfigDict(from_attributes=True)

    user_id: str
    portfolio_name: str
    broker: str
    raw_name: str
    trade_id: str
    updated_at: datetime | None = None


class BrokerConnection(BaseModel):
    """Saved API credentials letting one portfolio pull holdings/positions
    directly from a broker's API (see src/data_providers/dhan_provider.py
    and pages/6_Portfolio.py's "Connect Dhan account" flow) instead of a
    CSV upload. `access_token` is stored as given -- see
    supabase/migrations/0017_broker_connections.sql's docstring for the
    plaintext/RLS-only trade-off this implies. `token_saved_at` is set only
    when credentials are saved/updated (not on every sync), so the UI can
    warn once it's old enough that Dhan's 24-hour token is likely expired."""

    model_config = ConfigDict(from_attributes=True)

    user_id: str
    portfolio_name: str
    broker: str
    client_id: str
    access_token: str
    token_saved_at: datetime | None = None
