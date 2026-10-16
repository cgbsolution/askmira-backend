"""Where each product lives and the secret it shares with AskMira, from the environment.

Per product (INTERVIEW_TOOL_…, EXPENSE_TOOL_…):

    _API_URL  the product's backend, which AskMira calls server to server
    _APP_URL  the product's web app, where a launch lands (/sso/askmira)
    _SECRET   a long random value set on both sides; signs every token

A product with any of the three unset is "not configured": it can't be linked,
and the console says so.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

#: A shorter secret is refused: HS256 is only as strong as its key.
MIN_SECRET_LENGTH = 32


class ProductSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )

    interview_tool_api_url: str | None = None
    interview_tool_app_url: str | None = None
    interview_tool_secret: str | None = None
    expense_tool_api_url: str | None = None
    expense_tool_app_url: str | None = None
    expense_tool_secret: str | None = None


@dataclass(frozen=True, slots=True)
class Endpoint:
    api_url: str
    app_url: str
    secret: str


@lru_cache
def _settings() -> ProductSettings:
    return ProductSettings()


def endpoint(product: str) -> Endpoint | None:
    """The product's addresses and secret, or None when it is not fully configured."""
    prefix = {"interview": "interview_tool", "expense": "expense_tool"}.get(product)
    if prefix is None:
        return None
    s = _settings()
    api_url = getattr(s, f"{prefix}_api_url")
    app_url = getattr(s, f"{prefix}_app_url")
    secret = getattr(s, f"{prefix}_secret")
    if not api_url or not app_url or not secret or len(secret) < MIN_SECRET_LENGTH:
        return None
    return Endpoint(api_url=api_url.rstrip("/"), app_url=app_url.rstrip("/"), secret=secret)


def reset_cache() -> None:
    """Re-read the environment (tests change it)."""
    _settings.cache_clear()
