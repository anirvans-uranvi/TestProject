"""Central, env-driven application configuration."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Supabase
    supabase_url: str = Field(default="", alias="SUPABASE_URL")
    supabase_anon_key: str = Field(default="", alias="SUPABASE_ANON_KEY")
    supabase_service_role_key: str = Field(default="", alias="SUPABASE_SERVICE_ROLE_KEY")

    # Providers
    market_data_provider: Literal["dhan", "mock"] = Field(default="mock", alias="MARKET_DATA_PROVIDER")
    fundamentals_provider: Literal["manual", "mock"] = Field(default="mock", alias="FUNDAMENTALS_PROVIDER")
    dhan_client_id: str = Field(default="", alias="DHAN_CLIENT_ID")
    dhan_access_token: str = Field(default="", alias="DHAN_ACCESS_TOKEN")

    # Thresholds (defaults; per-user overrides live in user_settings table)
    default_dividend_yield_threshold: float = Field(default=3.0, alias="DEFAULT_DIVIDEND_YIELD_THRESHOLD")
    default_peg_threshold: float = Field(default=1.0, alias="DEFAULT_PEG_THRESHOLD")
    default_stale_data_threshold_minutes: int = Field(default=30, alias="DEFAULT_STALE_DATA_THRESHOLD_MINUTES")

    # Scheduling
    intraday_refresh_interval_minutes: int = Field(default=15, alias="INTRADAY_REFRESH_INTERVAL_MINUTES")

    # Misc
    app_timezone: str = Field(default="Asia/Kolkata", alias="APP_TIMEZONE")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @property
    def has_service_role(self) -> bool:
        return bool(self.supabase_service_role_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
