"""Application settings, loaded once from the environment."""

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import field_validator, model_validator
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
    # Supabase Postgres, plus one Storage bucket for files (below). No GoTrue,
    # no Supabase client SDK.
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/askmira"
    DATABASE_URL_SYNC: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/askmira"
    DB_ECHO: bool = False
    # Supabase's session pooler (port 5432) admits 15 clients per project by
    # default, shared by every process on the database: this API, its scheduler
    # lock, a deploy's second instance, migrations and any script. 5 + 5 here,
    # plus the lock, keeps one instance at 11.
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 5
    #: Seconds a request waits for a connection, from the pool or the pooler,
    #: before it is answered 503. Well inside the 120 s after which the portal's
    #: proxy gives up with a bare 502.
    DB_POOL_TIMEOUT: int = 10

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
    # Where the web portal lives, for links in emails (password reset). Falls
    # back to the first CORS origin, which is the portal in every deployment.
    WEB_APP_URL: str | None = None
    #: Shared with the portal (its `PROXY_SHARED_SECRET`). With it, the client
    #: address the portal forwards is believed; see app/core/proxy.py. Unset,
    #: every request through the portal looks like it comes from Vercel.
    PROXY_SHARED_SECRET: str | None = None
    # Where this API is reached from outside, for OAuth discovery documents
    # (the MCP connector flow). Falls back to the address the request came in on.
    PUBLIC_API_URL: str | None = None

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

    # ── File storage (document vault, letters, filings, resumes) ───────────
    # "supabase" keeps every file in a private Supabase Storage bucket, which is
    # what a deployment uses: a container's own disk is gone on the next
    # deploy. "local" writes under FILE_STORAGE_ROOT, for development and tests;
    # unset means backend/uploads, which is git-ignored.
    STORAGE_BACKEND: Literal["local", "supabase"] = "local"
    FILE_STORAGE_ROOT: str | None = None
    MAX_UPLOAD_BYTES: int = 10 * 1024 * 1024
    #: An uploaded archive of many files, such as the Form 16 Part A ZIP from TRACES.
    MAX_ARCHIVE_BYTES: int = 200 * 1024 * 1024

    # Project Settings -> API. The service-role key bypasses Row Level Security,
    # so it stays on the server and is used for Storage only - never Auth.
    SUPABASE_URL: str | None = None
    SUPABASE_SERVICE_ROLE_KEY: str | None = None
    SUPABASE_STORAGE_BUCKET: str = "askmira-files"

    # "Continue with …" sign-in providers read their own settings in
    # app/services/identity/social.py, the one module that knows them.

    # ── AI (V2+) ────────────────────────────────────────────────────────────
    ANTHROPIC_API_KEY: str | None = None
    AI_MODEL: str = "claude-opus-5-5"

    # ── Email over SMTP (slice 05) ──────────────────────────────────────────
    # The platform relay. A tenant may configure its own relay instead, stored
    # encrypted in `tenant_smtp_settings`; with neither, email is logged by the
    # console adapter and never delivered — and the admin screen says so.
    SMTP_HOST: str | None = None
    SMTP_PORT: int = 587
    SMTP_USERNAME: str | None = None
    SMTP_PASSWORD: str | None = None
    SMTP_USE_STARTTLS: bool = True
    SMTP_USE_SSL: bool = False
    SMTP_FROM: str | None = None
    SMTP_TIMEOUT_SECONDS: int = 20

    # ── Scheduler (cross-cutting gap 4) ─────────────────────────────────────
    # None means "on, except under the test suite". An explicit false turns it
    # off for an instance that should never run jobs (a one-off shell, say).
    SCHEDULER_ENABLED: bool | None = None
    SCHEDULER_TICK_SECONDS: int = 60

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        """Accept a comma-separated string as well as a JSON list."""
        if isinstance(v, str) and not v.startswith("["):
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        return v

    @field_validator("STORAGE_BACKEND", mode="before")
    @classmethod
    def _normalise_backend(cls, v: object) -> object:
        """`Supabase` and ` supabase ` mean what they say; a typo still fails at boot."""
        return v.strip().lower() if isinstance(v, str) else v

    @model_validator(mode="after")
    def _derive_sync_url(self) -> "Settings":
        """Alembic's URL from the app's, when only `DATABASE_URL` is configured.

        Deploys run `alembic upgrade head` before the server starts. A host that
        sets only `DATABASE_URL` would otherwise migrate against the localhost
        default and never start.
        """
        if "DATABASE_URL_SYNC" not in self.model_fields_set and "+asyncpg" in self.DATABASE_URL:
            self.DATABASE_URL_SYNC = self.DATABASE_URL.replace("+asyncpg", "+psycopg2", 1)
        return self

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.lower() in {"production", "prod"}


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
