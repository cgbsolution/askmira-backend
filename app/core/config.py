"""Application settings, loaded once from the environment."""

from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Application ─────────────────────────────────────────────────────────
    APP_NAME: str = "AskMira"
    ENVIRONMENT: str = "development"
    DEBUG: bool = True
    API_V1_PREFIX: str = "/api/v1"

    # ── Database ────────────────────────────────────────────────────────────
    # Supabase Postgres. Database only — no GoTrue, no Supabase client SDK.
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/askmira"
    DATABASE_URL_SYNC: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/askmira"
    DB_ECHO: bool = False
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10

    # ── Auth ────────────────────────────────────────────────────────────────
    JWT_SECRET_KEY: str = "change-me"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    ARGON2_TIME_COST: int = 3
    ARGON2_MEMORY_COST: int = 65536
    ARGON2_PARALLELISM: int = 4

    # ── CORS ────────────────────────────────────────────────────────────────
    # `NoDecode` is load-bearing. Without it pydantic-settings JSON-decodes a
    # complex field straight out of the dotenv source, *before* any field
    # validator runs — so the comma-separated form below never reaches
    # `_split_origins`, and a perfectly ordinary
    # `CORS_ORIGINS=http://a,http://b` line fails at import with a JSON decode
    # error. This hands the raw string to the validator, which is what the
    # validator was written for.
    CORS_ORIGINS: Annotated[list[str], NoDecode] = ["http://localhost:3000"]

    # ── Secret encryption at rest (slice 04) ───────────────────────────────
    # A distinct key from JWT_SECRET_KEY on purpose: reusing the token-signing
    # key for data at rest means rotating one forces rotating the other.
    # Required in production; falls back to the JWT key in development.
    SECRET_ENCRYPTION_KEY: str | None = None

    # ── E-signature (slice 03) ──────────────────────────────────────────────
    # Digio is the chosen Indian, Aadhaar-eSign capable provider. Absent
    # credentials, only the noop provider is available, and it is refused in
    # production.
    SIGNATURE_PROVIDER: str = "noop"
    DIGIO_CLIENT_ID: str | None = None
    DIGIO_CLIENT_SECRET: str | None = None
    DIGIO_WEBHOOK_SECRET: str | None = None

    # ── AI (V2+) ────────────────────────────────────────────────────────────
    ANTHROPIC_API_KEY: str | None = None
    AI_MODEL: str = "claude-sonnet-5"

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        """Accept a comma-separated string as well as a JSON list."""
        if isinstance(v, str) and not v.startswith("["):
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        return v

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.lower() in {"production", "prod"}


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
