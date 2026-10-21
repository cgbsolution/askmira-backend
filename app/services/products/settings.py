"""Where each product lives, and the older server-wide secret, from the environment.

Per product (INTERVIEW_TOOL_…, EXPENSE_TOOL_…):

    _API_URL  the product's backend, which AskMira calls server to server
    _APP_URL  the product's web app, where Connect and a launch land
    _SECRET   optional: a server-wide secret set on both sides, for workspaces
              linked before Connectors. A workspace that connected from
              Connectors has a secret of its own (products/connections.py).

The addresses are public facts, not secrets. Both products' production
addresses are built in; set the variables only to point somewhere else (a
local or staging copy).
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


#: Production addresses of our own products, used when the environment names none.
DEFAULT_ADDRESSES: dict[str, tuple[str, str]] = {
    "interview": ("https://anytimehire-api-prod.epfassist.com", "https://app.anytimehire.ai"),
    "expense": (
        "https://expanse-backend-api.epfassist.com",
        "https://standalone-expenses-ginie-dashboard.vercel.app",
    ),
}


def addresses(product: str) -> tuple[str, str] | None:
    """The product's (api_url, app_url), or None when this server does not know where it is."""
    prefix = {"interview": "interview_tool", "expense": "expense_tool"}.get(product)
    if prefix is None:
        return None
    s = _settings()
    api_url = getattr(s, f"{prefix}_api_url")
    app_url = getattr(s, f"{prefix}_app_url")
    if api_url and app_url:
        return api_url.rstrip("/"), app_url.rstrip("/")
    return DEFAULT_ADDRESSES.get(product)


def reset_cache() -> None:
    """Re-read the environment (tests change it)."""
    _settings.cache_clear()
