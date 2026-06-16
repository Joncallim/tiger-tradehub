from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    api_token: str = Field(alias="TRADEHUB_API_TOKEN")
    dry_run: bool = Field(default=True, alias="TRADEHUB_DRY_RUN")
    require_approval: bool = Field(default=True, alias="TRADEHUB_REQUIRE_APPROVAL")
    bind_host: str = Field(default="127.0.0.1", alias="TRADEHUB_BIND_HOST")
    port: int = Field(default=8787, alias="TRADEHUB_PORT")
    database_path: Path = Field(default=Path("data/tradehub.db"), alias="TRADEHUB_DATABASE_PATH")

    symbol_allowlist: set[str] = Field(default_factory=set, alias="TRADEHUB_SYMBOL_ALLOWLIST")
    max_notional_usd: float = Field(default=1000.0, alias="TRADEHUB_MAX_NOTIONAL_USD")
    max_quantity: float = Field(default=100.0, alias="TRADEHUB_MAX_QUANTITY")
    allow_market_orders: bool = Field(default=False, alias="TRADEHUB_ALLOW_MARKET_ORDERS")
    confirmation_ttl_seconds: int = Field(default=300, alias="TRADEHUB_CONFIRMATION_TTL_SECONDS")

    tiger_id: str | None = Field(default=None, alias="TIGEROPEN_TIGER_ID")
    tiger_account: str | None = Field(default=None, alias="TIGEROPEN_ACCOUNT")
    tiger_private_key_path: Path | None = Field(default=None, alias="TIGEROPEN_PRIVATE_KEY_PATH")
    tiger_private_key: str | None = Field(default=None, alias="TIGEROPEN_PRIVATE_KEY")
    tiger_sandbox: bool = Field(default=False, alias="TIGEROPEN_SANDBOX")

    telegram_bot_token: str | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_allowed_chat_ids: set[int] = Field(
        default_factory=set, alias="TELEGRAM_ALLOWED_CHAT_IDS"
    )

    @field_validator("symbol_allowlist", mode="before")
    @classmethod
    def parse_symbols(cls, value: object) -> set[str]:
        if value in (None, ""):
            return set()
        if isinstance(value, str):
            return {item.strip().upper() for item in value.split(",") if item.strip()}
        return {str(item).upper() for item in value}  # type: ignore[union-attr]

    @field_validator("telegram_allowed_chat_ids", mode="before")
    @classmethod
    def parse_chat_ids(cls, value: object) -> set[int]:
        if value in (None, ""):
            return set()
        if isinstance(value, str):
            return {int(item.strip()) for item in value.split(",") if item.strip()}
        return {int(item) for item in value}  # type: ignore[union-attr]

    @property
    def tiger_configured(self) -> bool:
        return bool(
            self.tiger_id
            and self.tiger_account
            and (self.tiger_private_key or self.tiger_private_key_path)
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]

