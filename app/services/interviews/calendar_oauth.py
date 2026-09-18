"""Connecting an interviewer's calendar: the OAuth authorisation-code flow.

═══════════════════════════════════════════════════════════════════════════════
NOTHING IS CONNECTED UNTIL A PROVIDER SAYS SO
═══════════════════════════════════════════════════════════════════════════════

The earlier endpoint accepted raw access tokens posted by the browser, which let
a client store anything and have it shown as a connected calendar. That is gone.
A connection exists only after the provider's own token endpoint has exchanged
an authorisation code for tokens.

The OAuth client is configured per deployment, entirely from the environment,
per provider value in `CalendarProviderKind`:

    CALENDAR_OAUTH_<PROVIDER>_CLIENT_ID
    CALENDAR_OAUTH_<PROVIDER>_CLIENT_SECRET
    CALENDAR_OAUTH_<PROVIDER>_AUTHORIZE_URL
    CALENDAR_OAUTH_<PROVIDER>_TOKEN_URL
    CALENDAR_OAUTH_<PROVIDER>_SCOPES            (space-separated)
    CALENDAR_OAUTH_REDIRECT_URI                 (the portal page that receives the code)

With any of those missing the provider reports "not configured on this
deployment" and nothing is offered. No endpoint URL or scope is written here:
provider knowledge belongs to the calendar adapters (an invariant checks), and a
URL baked into code is a URL nobody reviews when the provider changes it.

The state parameter is a short-lived signed token bound to the user, tenant and
provider, so a code minted for one interviewer cannot be redeemed into another's
account.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.crypto import encrypt_secret
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.interview import CalendarConnection, CalendarProviderKind
from app.models.user import User
from app.services import audit

__all__ = [
    "OAuthClient",
    "authorization_url",
    "complete_connection",
    "connection_status",
    "disconnect",
    "oauth_client",
]

_STATE_TYPE = "calendar_oauth_state"
_STATE_TTL = timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class OAuthClient:
    client_id: str
    client_secret: str
    authorize_url: str
    token_url: str
    scopes: str
    redirect_uri: str


def oauth_client(provider: CalendarProviderKind) -> OAuthClient | None:
    """This deployment's OAuth client for a provider, or None when not configured."""
    prefix = f"CALENDAR_OAUTH_{provider.value.upper()}_"
    values = {
        "client_id": os.environ.get(prefix + "CLIENT_ID", ""),
        "client_secret": os.environ.get(prefix + "CLIENT_SECRET", ""),
        "authorize_url": os.environ.get(prefix + "AUTHORIZE_URL", ""),
        "token_url": os.environ.get(prefix + "TOKEN_URL", ""),
        "scopes": os.environ.get(prefix + "SCOPES", ""),
        "redirect_uri": os.environ.get("CALENDAR_OAUTH_REDIRECT_URI", ""),
    }
    if not all(values.values()):
        return None
    if not all(values[k].startswith("https://") for k in ("authorize_url", "token_url")):
        return None
    return OAuthClient(**values)


def _state_for(user: User, provider: CalendarProviderKind) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "typ": _STATE_TYPE,
            "sub": str(user.id),
            "tid": str(user.tenant_id),
            "provider": provider.value,
            "iat": int(now.timestamp()),
            "exp": int((now + _STATE_TTL).timestamp()),
            "nonce": uuid.uuid4().hex,
        },
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


def _check_state(state: str, user: User, provider: CalendarProviderKind) -> None:
    try:
        claims = jwt.decode(
            state, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM]
        )
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This calendar sign-in link has expired or is not valid. Start again.",
        ) from exc
    if (
        claims.get("typ") != _STATE_TYPE
        or claims.get("sub") != str(user.id)
        or claims.get("tid") != str(user.tenant_id)
        or claims.get("provider") != provider.value
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "This calendar sign-in was started by somebody else or for another "
                "calendar. Nothing was connected."
            ),
        )


async def connection_status(session: AsyncSession, user: User) -> dict[str, Any]:
    """The caller's own calendar connections, stated as they are."""
    rows = {
        row.provider: row
        for row in (
            await session.scalars(
                tenant_scoped(
                    select(CalendarConnection).where(CalendarConnection.user_id == user.id),
                    CalendarConnection,
                    user,
                )
            )
        ).all()
    }
    now = datetime.now(UTC)
    providers = []
    for provider in CalendarProviderKind:
        row = rows.get(provider)
        connected = bool(
            row is not None and row.revoked_at is None and row.access_token_encrypted
        )
        expired = bool(connected and row.expires_at is not None and row.expires_at <= now)
        providers.append(
            {
                "provider": provider.value,
                "configured": oauth_client(provider) is not None,
                "connected": connected,
                "account_email": row.account_email if connected else None,
                "expires_at": row.expires_at.isoformat() if connected and row.expires_at else None,
                "token_expired": expired,
                "last_error": row.last_error if row is not None else None,
                "message": (
                    "Connected."
                    if connected and not expired
                    else "The stored token has expired. Reconnect to keep scheduling from "
                    "this calendar."
                    if expired
                    else "Not connected."
                    if oauth_client(provider) is not None
                    else "Not connected. This deployment has no OAuth client configured for "
                    "this calendar, so it cannot be connected yet."
                ),
            }
        )
    return {
        "providers": providers,
        "note": (
            "Free/busy lookups and invitations only use a calendar that is connected here. "
            "Without one, interviews are still recorded, and the people involved must be "
            "told by hand."
        ),
    }


def authorization_url(user: User, provider: CalendarProviderKind) -> dict[str, Any]:
    client = oauth_client(provider)
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This deployment has no OAuth client configured for this calendar, so it "
                "cannot be connected. An administrator needs to register one first."
            ),
        )
    query = urlencode(
        {
            "client_id": client.client_id,
            "redirect_uri": client.redirect_uri,
            "response_type": "code",
            "scope": client.scopes,
            "access_type": "offline",
            "prompt": "consent",
            "state": _state_for(user, provider),
        }
    )
    separator = "&" if "?" in client.authorize_url else "?"
    return {"authorization_url": f"{client.authorize_url}{separator}{query}"}


def _account_email(tokens: dict[str, Any], fallback: str) -> str:
    """The signed-in account's email from the OpenID id_token, when one was issued.

    Read without signature verification: the token arrived directly from the
    provider's token endpoint over TLS in response to our own request, which is
    the OpenID Connect rule for when that is acceptable. It is used as a label,
    never as an identity for anything.
    """
    raw = tokens.get("id_token")
    if isinstance(raw, str):
        try:
            claims = jwt.decode(raw, options={"verify_signature": False})
        except jwt.InvalidTokenError:
            claims = {}
        email = claims.get("email") or claims.get("preferred_username")
        if isinstance(email, str) and "@" in email:
            return email[:320]
    return fallback


async def complete_connection(
    session: AsyncSession,
    user: User,
    provider: CalendarProviderKind,
    *,
    code: str,
    state: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Exchange the authorisation code at the provider and store the tokens encrypted."""
    client = oauth_client(provider)
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This calendar is not configured on this deployment. Nothing was connected.",
        )
    _check_state(state, user, provider)

    try:
        async with httpx.AsyncClient(transport=transport, timeout=15.0) as http:
            response = await http.post(
                client.token_url,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": client.redirect_uri,
                    "client_id": client.client_id,
                    "client_secret": client.client_secret,
                },
                headers={"Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The calendar provider could not be reached. Nothing was connected.",
        ) from exc

    tokens: dict[str, Any] = {}
    try:
        tokens = response.json()
    except ValueError:
        tokens = {}
    access_token = tokens.get("access_token")
    if response.status_code >= 400 or not isinstance(access_token, str) or not access_token:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "The calendar provider refused the sign-in"
                + (f": {tokens.get('error_description') or tokens.get('error')}" if tokens else "")
                + ". Nothing was connected."
            ),
        )

    existing = await session.scalar(
        tenant_scoped(
            select(CalendarConnection).where(
                CalendarConnection.user_id == user.id,
                CalendarConnection.provider == provider,
            ),
            CalendarConnection,
            user,
        )
    )
    account_email = _account_email(tokens, user.email)
    connection = existing or CalendarConnection(
        tenant_id=user.tenant_id, user_id=user.id, provider=provider, account_email=account_email
    )
    connection.account_email = account_email
    connection.access_token_encrypted = encrypt_secret(access_token, user.tenant_id)
    refresh = tokens.get("refresh_token")
    if isinstance(refresh, str) and refresh:
        connection.refresh_token_encrypted = encrypt_secret(refresh, user.tenant_id)
    expires_in = tokens.get("expires_in")
    connection.expires_at = (
        datetime.now(UTC) + timedelta(seconds=int(expires_in))
        if isinstance(expires_in, int | float | str) and str(expires_in).isdigit()
        else None
    )
    scope = tokens.get("scope")
    connection.scopes = scope.split() if isinstance(scope, str) else []
    connection.revoked_at = None
    connection.last_error = None
    session.add(connection)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="calendar_connection",
        entity_id=connection.id,
        summary=f"Calendar connected ({provider.value}) for {account_email}",
    )
    return {"connected": True, "provider": provider.value, "account_email": account_email}


async def disconnect(
    session: AsyncSession, user: User, provider: CalendarProviderKind
) -> dict[str, Any]:
    connection = await session.scalar(
        tenant_scoped(
            select(CalendarConnection).where(
                CalendarConnection.user_id == user.id,
                CalendarConnection.provider == provider,
            ),
            CalendarConnection,
            user,
        )
    )
    if connection is None or connection.revoked_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not connected.")
    connection.revoked_at = datetime.now(UTC)
    connection.access_token_encrypted = None
    connection.refresh_token_encrypted = None
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="calendar_connection",
        entity_id=connection.id,
        summary=f"Calendar disconnected ({provider.value})",
    )
    return {"disconnected": True, "provider": provider.value}
