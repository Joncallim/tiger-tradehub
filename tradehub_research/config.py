from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class ResearchSettings(BaseSettings):
    """Research-only settings. Execution credentials are deliberately absent."""

    model_config = SettingsConfigDict(env_prefix="RESEARCH_", extra="ignore")

    db_path: Path = Path("data/research/research.db")
    busy_timeout_ms: int = 5000
