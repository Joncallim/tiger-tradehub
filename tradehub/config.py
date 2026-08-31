from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_API_TOKEN_LENGTH = 24


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    api_token: SecretStr = Field(alias="TRADEHUB_API_TOKEN")
    preview_api_token: SecretStr | None = Field(default=None, alias="TRADEHUB_PREVIEW_API_TOKEN")
    # Scoped credential for the deterministic autonomous-PAPER runner (issue
    # #51): authorizes the execution API but is never a Tiger credential.
    autonomy_api_token: SecretStr | None = Field(default=None, alias="TRADEHUB_AUTONOMY_TOKEN")
    dry_run: bool = Field(default=True, alias="TRADEHUB_DRY_RUN")
    bind_host: str = Field(default="127.0.0.1", alias="TRADEHUB_BIND_HOST")
    port: int = Field(default=8787, alias="TRADEHUB_PORT")
    database_path: Path = Field(default=Path("data/tradehub.db"), alias="TRADEHUB_DATABASE_PATH")

    symbol_allowlist: set[str] = Field(default_factory=set, alias="TRADEHUB_SYMBOL_ALLOWLIST")
    max_notional_usd: float = Field(default=1000.0, alias="TRADEHUB_MAX_NOTIONAL_USD")
    max_quantity: float = Field(default=100.0, alias="TRADEHUB_MAX_QUANTITY")
    confirmation_ttl_seconds: int = Field(default=300, alias="TRADEHUB_CONFIRMATION_TTL_SECONDS")

    tiger_id: str | None = Field(default=None, alias="TIGEROPEN_TIGER_ID")
    tiger_account: str | None = Field(default=None, alias="TIGEROPEN_ACCOUNT")
    tiger_private_key_path: Path | None = Field(default=None, alias="TIGEROPEN_PRIVATE_KEY_PATH")
    tiger_private_key: SecretStr | None = Field(default=None, alias="TIGEROPEN_PRIVATE_KEY")
    tiger_license: str | None = Field(default=None, alias="TIGEROPEN_LICENSE")
    tiger_sandbox: bool = Field(default=False, alias="TIGEROPEN_SANDBOX")

    telegram_bot_token: SecretStr | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_allowed_chat_ids: set[int] = Field(
        default_factory=set, alias="TELEGRAM_ALLOWED_CHAT_IDS"
    )

    @field_validator("api_token")
    @classmethod
    def validate_api_token(cls, value: SecretStr) -> SecretStr:
        token = value.get_secret_value()
        if not token or token == "change-me":
            raise ValueError("TRADEHUB_API_TOKEN must be set to a strong random token")
        if len(token) < MIN_API_TOKEN_LENGTH:
            raise ValueError(
                f"TRADEHUB_API_TOKEN must be at least {MIN_API_TOKEN_LENGTH} characters"
            )
        return value

    @field_validator("preview_api_token")
    @classmethod
    def validate_preview_api_token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) < MIN_API_TOKEN_LENGTH:
            raise ValueError(
                f"TRADEHUB_PREVIEW_API_TOKEN must be at least {MIN_API_TOKEN_LENGTH} characters"
            )
        return value

    @property
    def preview_token(self) -> str:
        """Preview-only capability; production research deployments set it separately."""
        return (self.preview_api_token or self.api_token).get_secret_value()

    def require_distinct_preview_capability(self) -> None:
        if self.preview_api_token is None:
            raise ValueError("TRADEHUB_PREVIEW_API_TOKEN must be configured separately")
        if self.preview_token == self.api_token.get_secret_value():
            raise ValueError("TRADEHUB_PREVIEW_API_TOKEN must differ from TRADEHUB_API_TOKEN")

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


def secret_value(value: SecretStr | None) -> str | None:
    return value.get_secret_value() if value is not None else None


@lru_cache
def get_settings() -> Settings:
    settings = Settings()  # type: ignore[call-arg]
    settings.require_distinct_preview_capability()
    return settings
