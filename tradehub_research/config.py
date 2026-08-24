from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class ResearchSettings(BaseSettings):
    """Research-only settings. Execution credentials are deliberately absent."""

    model_config = SettingsConfigDict(env_prefix="RESEARCH_", extra="ignore")

    db_path: Path = Path("data/research/research.db")
    busy_timeout_ms: int = 5000
    sec_user_agent: str = ""
    adapter_cache_dir: Path = Path("data/research/raw")
    tiingo_token: str | None = None
    tiingo_license_confirmed: bool = False
