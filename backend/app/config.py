"""Application configuration.

All secrets come from the environment or a mounted .env file -- never from the
client. This is the single most important difference from the reference
implementation, where callers had to put their own LLM key in the request body.
"""

from __future__ import annotations

import secrets
from functools import lru_cache
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- service -------------------------------------------------------
    environment: Literal["development", "staging", "production"] = "development"
    log_level: str = "INFO"
    port: int = 8080
    service_name: str = "deep-research"

    # ---- auth ----------------------------------------------------------
    # Comma-separated list. Each caller gets its own key so you can revoke one
    # without rotating everybody.
    api_keys: str = ""
    # Disables auth entirely. Guarded so it cannot be switched on in prod.
    auth_disabled: bool = False

    # ---- CORS ----------------------------------------------------------
    # Explicit origins only. "*" together with credentials is rejected by every
    # browser and is what the reference implementation shipped.
    cors_origins: str = "http://localhost:3000"

    # ---- provider credentials -----------------------------------------
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    moonshot_api_key: str = ""
    openrouter_api_key: str = ""
    groq_api_key: str = ""
    google_api_key: str = ""
    deepseek_api_key: str = ""
    tavily_api_key: str = ""

    # ---- research defaults --------------------------------------------
    default_provider: str = "anthropic"
    default_search_api: str = "tavily"
    allow_clarification: bool = False
    max_researcher_iterations: int = 3
    max_react_tool_calls: int = 5
    max_concurrent_research_units: int = 3
    max_structured_output_retries: int = 2
    research_timeout_seconds: int = 900
    # Total characters of prior-report context admitted per run (~4 chars/token).
    # Prior context competes with findings for the same window, so it is capped
    # rather than allowed to crowd out the actual research.
    max_context_characters: int = 24_000

    # ---- job engine ----------------------------------------------------
    job_backend: Literal["memory", "redis"] = "memory"
    redis_url: str = "redis://localhost:6379/0"
    job_retention_seconds: int = 86_400
    max_concurrent_jobs: int = 4

    # ---- webhooks ------------------------------------------------------
    webhook_secret: str = ""
    webhook_timeout_seconds: int = 15
    webhook_max_attempts: int = 5

    # ---- rate limiting -------------------------------------------------
    rate_limit_requests: int = 60
    rate_limit_window_seconds: int = 60

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @property
    def api_key_set(self) -> set[str]:
        return {k.strip() for k in self.api_keys.split(",") if k.strip()}

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @model_validator(mode="after")
    def _guard_production(self) -> Settings:
        """Fail fast rather than boot a production service that is wide open."""
        if not self.is_production:
            return self

        if self.auth_disabled:
            raise ValueError("AUTH_DISABLED cannot be true when ENVIRONMENT=production")
        if not self.api_key_set:
            raise ValueError("API_KEYS must be set when ENVIRONMENT=production")
        if "*" in self.cors_origin_list:
            raise ValueError("CORS_ORIGINS cannot contain '*' when ENVIRONMENT=production")
        if not self.webhook_secret:
            raise ValueError(
                "WEBHOOK_SECRET must be set when ENVIRONMENT=production so callbacks "
                "can be HMAC-signed"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


def generate_api_key(prefix: str = "drk") -> str:
    """Mint a caller key. Used by `python -m app.cli keygen`."""
    return f"{prefix}_{secrets.token_urlsafe(32)}"
