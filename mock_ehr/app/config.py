"""Mock EHR configuration — independent from the platform backend.

Reads the same repo-root `.env` file but only its own variables. Defaults are
safe for local development; never hardcode real keys here.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# mock_ehr/app/config.py -> parents[2] == repo root
_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), env_file_encoding="utf-8", extra="ignore")

    environment: str = "development"
    mock_ehr_api_host: str = "127.0.0.1"
    mock_ehr_api_port: int = 8001

    # Own database — NEVER the platform DB.
    mock_ehr_database_url: str = "sqlite:///./data/mock_ehr.db"

    # API-key auth (X-API-Key header).
    mock_ehr_api_key: str = "dev-ehr-key"

    # Fault injection (deterministic, off by default).
    mock_ehr_fault_mode: str = "none"  # none | server_error | timeout | rejected | delay
    mock_ehr_fault_delay_seconds: float = 10.0
    # Optional comma-separated path list to target (empty = all endpoints),
    # e.g. "/ehr/appointments" stalls only appointment writes.
    mock_ehr_fault_paths: str = ""

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
