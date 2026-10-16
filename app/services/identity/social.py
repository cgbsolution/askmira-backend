"""'Continue with Google' and 'Continue with Microsoft' on the sign-in page.

Platform-level OAuth apps, registered once for AskMira (see `core/config.py`).
They sign in somebody who **already has** an AskMira login: the provider proves
an email address, and that address is looked up — one login per address across
the platform. Nothing is created here; no login appears because somebody owns
a Google account.

═══════════════════════════════════════════════════════════════════════════════
WHEN AN EMAIL FROM A PROVIDER IS TRUSTED
═══════════════════════════════════════════════════════════════════════════════

- **Google:** only with `email_verified` true. Google says so for every
  Workspace and Gmail address it actually controls.
- **Microsoft:** only with the `xms_edov` claim true — "email domain owner
  verified". Without it, a Microsoft account's email is whatever its tenant's
  admin typed, and anybody can create a tenant: trusting that claim is the
  well-known way to sign in as somebody else. The app registration must emit
  `xms_edov` as an optional ID-token claim; until it does, Microsoft sign-ins
  are refused with that reason.

A workspace that enforces its own single sign-on is not bypassed: its people
are sent back to their company's identity provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, Request, status
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.social_login import SocialLoginAttempt
from app.models.user import User
from app.services import auth as auth_service
from app.services.identity import oidc

__all__ = ["PROVIDERS", "complete", "configured", "start"]

ATTEMPT_TTL = timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class Provider:
    key: str
    label: str
    discovery_url: str


PROVIDERS: dict[str, Provider] = {
    "google": Provider(
        "google", "Google", "https://accounts.google.com/.well-known/openid-configuration"
    ),
    # `organizations`: work and school accounts only. Personal Microsoft
    # accounts are not somebody's employer's identity.
    "microsoft": Provider(
        "microsoft",
        "Microsoft",
        "https://login.microsoftonline.com/organizations/v2.0/.well-known/openid-configuration",
    ),
}


class SocialSettings(BaseSettings):
    """The platform's OAuth apps, from the environment. Unset, no button shows.

    Redirect URI to register with each provider:
    https://<portal>/signin/social/<google|microsoft>
    """

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )

    google_oauth_client_id: str | None = None
    google_oauth_client_secret: str | None = None
    microsoft_oauth_client_id: str | None = None
    microsoft_oauth_client_secret: str | None = None


social_settings = SocialSettings()


def _credentials(key: str) -> tuple[str | None, str | None]:
    if key == "google":
        return (
            social_settings.google_oauth_client_id,
            social_settings.google_oauth_client_secret,
        )
    if key == "microsoft":
        return (
            social_settings.microsoft_oauth_client_id,
            social_settings.microsoft_oauth_client_secret,
        )
    return None, None


def available() -> list[str]:
    """The providers this AskMira has an OAuth app for."""
    return [key for key in PROVIDERS if _credentials(key)[0]]


def configured(key: str) -> Provider:
    provider = PROVIDERS.get(key)
    client_id, _secret = _credentials(key)
    if provider is None or not client_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="That sign-in option is not available here.",
        )
    return provider


def _redirect_is_ours(redirect_uri: str, key: str) -> bool:
    return redirect_uri.split("?", 1)[0].rstrip("/").endswith(f"/signin/social/{key}")


async def start(session: AsyncSession, key: str, redirect_uri: str) -> str:
    """The provider's sign-in URL. State, nonce and PKCE are kept here."""
    provider = configured(key)
    if not _redirect_is_ours(redirect_uri, key):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="That return address is not AskMira's sign-in page.",
        )
    client_id, _secret = _credentials(key)
    try:
        discovery = await oidc.discover(provider.discovery_url)
    except oidc.OidcError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    begun = oidc.start_login(discovery=discovery, client_id=client_id, redirect_uri=redirect_uri)
    session.add(
        SocialLoginAttempt(
            provider=key,
            state=begun.state,
            nonce=begun.nonce,
            code_verifier=begun.code_verifier,
            redirect_uri=redirect_uri,
            expires_at=datetime.now(UTC) + ATTEMPT_TTL,
        )
    )
    await session.commit()
    if key == "google":
        # Let somebody with several Google accounts choose the work one.
        return f"{begun.authorization_url}&prompt=select_account"
    return begun.authorization_url


def _truthy(value: Any) -> bool:
    return value is True or str(value).lower() in ("true", "1")


def trusted_email(key: str, claims: dict[str, Any]) -> str:
    """The address the provider vouches for, or a refusal saying why it does not."""
    email = str(claims.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The provider did not share an email address, so nobody can be signed in.",
        )
    if key == "google" and not _truthy(claims.get("email_verified")):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Google has not verified this email address. Sign in with your password.",
        )
    if key == "microsoft" and not _truthy(claims.get("xms_edov")):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Microsoft did not confirm that your organisation owns this email address, "
                "so it cannot be used to sign in. Sign in with your password."
            ),
        )
    return email


async def complete(
    session: AsyncSession, key: str, *, code: str, state: str, request: Request | None
) -> Any:
    """Finish a 'Continue with …' sign-in: tokens, or a two-step challenge."""
    provider = configured(key)
    now = datetime.now(UTC)
    attempt = await session.scalar(
        select(SocialLoginAttempt).where(
            SocialLoginAttempt.state == state, SocialLoginAttempt.provider == key
        )
    )
    if attempt is None or attempt.used_at is not None or attempt.expires_at <= now:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="This sign-in expired or was already used. Start again.",
        )
    attempt.used_at = now
    await session.commit()

    client_id, secret = _credentials(key)
    try:
        discovery = await oidc.discover(provider.discovery_url)
        identity = await oidc.exchange_code(
            discovery=discovery,
            client_id=client_id,
            client_secret=secret,
            redirect_uri=attempt.redirect_uri,
            code=code,
            code_verifier=attempt.code_verifier,
            expected_nonce=attempt.nonce,
        )
    except oidc.OidcError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    email = trusted_email(key, identity.claims)
    user = await session.scalar(select(User).where(func.lower(User.email) == email))
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No AskMira login uses {email}. Ask your HR team to add you, or sign in "
                "with the email they set up for you."
            ),
        )
    return await auth_service.finish_external_login(
        session, user, method=provider.label, request=request
    )
