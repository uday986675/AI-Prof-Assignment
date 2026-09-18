"""Application configuration.

All values come from environment variables (a local `.env` file is supported).
Secrets (JWT key, AI provider keys, DB credentials) must never be hardcoded.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# The backend package lives at backend/app; .env sits at the repo root.
_ENV_FILE = (__import__("pathlib").Path(__file__).resolve().parents[3] / ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), env_file_encoding="utf-8", extra="ignore")

    # App
    app_environment: str = "development"
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    seed_demo_on_boot: bool = False
    # Database
    database_url: str = "sqlite:///./data/platform.db"

    # Mock EHR
    mock_ehr_url: str = "http://127.0.0.1:8001"
    mock_ehr_database_url: str = "sqlite:///./data/mock_ehr.db"
    mock_ehr_api_key: str = "dev-ehr-key"
    mock_ehr_timeout_seconds: float = 5.0
    ehr_mode: str = "mock"

    # Auth
    jwt_secret: str = "dev-insecure-secret"
    jwt_expires_minutes: int = 720

    # AI
    groq_api_key: str = ""
    gemini_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    gemini_model: str = "gemini-2.0-flash"
    # Optional explicit provider choice ("gemini" | "groq") when both keys are
    # set; empty means auto — Gemini is preferred, Groq is the fallback.
    llm_provider: str = ""

    # Voice — Gemini-first: STT via the same GEMINI_API_KEY (audio understanding),
    # TTS via gemini-2.5-flash-preview-tts. "groq" STT (Whisper) stays available
    # as an explicit fallback via GROQ_API_KEY; Groq is never required.
    # stt_model is separate from the chat LLM model: transcription does not need
    # the smartest (and most quota-constrained) chat model.
    stt_provider: str = "gemini"
    stt_model: str = "gemini-2.5-flash"
    whisper_model: str = "whisper-large-v3-turbo"
    tts_model: str = "gemini-2.5-flash-preview-tts"
    tts_voice_name: str = "Zephyr"

    @property
    def is_production(self) -> bool:
        return self.app_environment.lower() in {"production", "prod"}


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
