"""OAuth 2.1 authorization server for the MCP endpoint (AI-04).

The flow a connector such as claude.ai follows:

1. It reads `/.well-known/oauth-protected-resource/...` from the 401 the MCP
   endpoint returns, then `/.well-known/oauth-authorization-server`.
2. It registers itself (`register_client`, RFC 7591) as a public client.
3. It sends the person to the web app's consent page with a PKCE challenge.
   Signed in, they approve; `authorize` returns the redirect with a one-time code.
4. It exchanges the code and its PKCE verifier for tokens (`exchange_code`), and
   refreshes them (`refresh`), each refresh rotating both tokens.
5. It calls the MCP endpoint with the access token; `token_identity` resolves it
   to the person and the scopes they approved, narrowed to what their role allows
   today — a person demoted since approving loses the wider scopes at once.

Rules: only `S256` PKCE; redirect URIs must be https, or http on localhost for a
local tool; codes live ten minutes and work once; a code used twice revokes
every grant it produced, per the OAuth security BCP.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlsplit

from fastapi import HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import has_direct_reports, tenant_filter, tenant_scoped
from app.models.audit import AuditAction
from app.models.oauth import OAuthAuthorizationCode, OAuthClient, OAuthGrant
from app.models.user import User
from app.services import audit

__all__ = [
    "ALL_SCOPES",
    "AuthorizeIn",
    "ClientIn",
    "allowed_scopes",
    "authorize",
    "authorize_details",
    "exchange_code",
    "list_connections",
    "refresh",
    "register_client",
    "revoke_connection",
    "revoke_token",
    "token_identity",
]

#: What the MCP tools read. Nothing here writes.
ALL_SCOPES = ("employees:read", "org:read", "leave:read", "attendance:read")
SCOPE_LABELS = {
    "employees:read": "Search the employee directory (names, titles, teams, work email)",
    "org:read": "See departments and locations",
    "leave:read": "See who is out, and when",
    "attendance:read": "See attendance summaries for a date range",
}
CODE_LIFETIME = timedelta(minutes=10)
ACCESS_LIFETIME = timedelta(hours=1)
REFRESH_LIFETIME = timedelta(days=30)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _oauth_error(error: str, description: str, code: int = 400) -> HTTPException:
    return HTTPException(
        status_code=code, detail={"error": error, "error_description": description}
    )


def allowed_scopes(user: User) -> set[str]:
    """Everyone sees the directory, org and who is out in the app; attendance is HR's."""
    scopes = {"employees:read", "org:read", "leave:read"}
    if permissions.is_hr(user):
        scopes.add("attendance:read")
    return scopes


def _redirect_ok(uri: str) -> bool:
    parts = urlsplit(uri)
    if parts.fragment or not parts.netloc:
        return False
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1")


# ── Registration ────────────────────────────────────────────────────────────


class ClientIn(BaseModel):
    client_name: str = Field(default="MCP client", min_length=1, max_length=200)
    redirect_uris: list[str] = Field(min_length=1, max_length=10)
    token_endpoint_auth_method: str = "none"


async def register_client(session: AsyncSession, payload: ClientIn) -> dict[str, Any]:
    """RFC 7591. Public clients only: PKCE stands in for a secret."""
    if payload.token_endpoint_auth_method != "none":
        raise _oauth_error(
            "invalid_client_metadata", "Only public clients (token_endpoint_auth_method none)."
        )
    bad = [uri for uri in payload.redirect_uris if not _redirect_ok(uri)]
    if bad:
        raise _oauth_error(
            "invalid_redirect_uri",
            "Redirect URIs must be https, or http on localhost: " + ", ".join(bad[:3]),
        )
    row = OAuthClient(
        client_id=f"amc_{secrets.token_urlsafe(18)}",
        client_name=payload.client_name.strip(),
        redirect_uris=list(dict.fromkeys(payload.redirect_uris)),
    )
    session.add(row)
    await session.commit()
    return {
        "client_id": row.client_id,
        "client_id_issued_at": int(datetime.now(UTC).timestamp()),
        "client_name": row.client_name,
        "redirect_uris": row.redirect_uris,
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }


async def _client(session: AsyncSession, client_id: str) -> OAuthClient:
    row = await session.scalar(select(OAuthClient).where(OAuthClient.client_id == client_id))
    if row is None:
        raise _oauth_error("invalid_client", "Unknown client.", 401)
    return row


def _requested(scope: str | None, user: User) -> list[str]:
    asked = [s for s in (scope or "").split() if s] or list(ALL_SCOPES)
    unknown = [s for s in asked if s not in ALL_SCOPES]
    if unknown:
        raise _oauth_error("invalid_scope", f"Unknown scope: {', '.join(unknown)}.")
    allowed = allowed_scopes(user)
    return [s for s in ALL_SCOPES if s in asked and s in allowed]


# ── Consent ─────────────────────────────────────────────────────────────────


async def authorize_details(
    session: AsyncSession, user: User, *, client_id: str, redirect_uri: str, scope: str | None
) -> dict[str, Any]:
    """What the consent page shows: who is asking, and for what this person may grant."""
    client = await _client(session, client_id)
    if redirect_uri not in client.redirect_uris:
        raise _oauth_error("invalid_request", "That redirect URI is not registered for the client.")
    granted = _requested(scope, user)
    return {
        "client_name": client.client_name,
        "redirect_host": urlsplit(redirect_uri).hostname,
        "scopes": [{"scope": s, "label": SCOPE_LABELS[s]} for s in granted],
    }


class AuthorizeIn(BaseModel):
    client_id: str = Field(max_length=64)
    redirect_uri: str = Field(max_length=1000)
    response_type: str = "code"
    code_challenge: str = Field(min_length=43, max_length=128)
    code_challenge_method: str = "S256"
    scope: str | None = Field(default=None, max_length=500)
    state: str | None = Field(default=None, max_length=500)
    approve: bool = True


async def authorize(
    session: AsyncSession, user: User, payload: AuthorizeIn, *, request: Request | None = None
) -> dict[str, str]:
    """The person approved (or declined): where to send their browser next."""
    client = await _client(session, payload.client_id)
    if payload.redirect_uri not in client.redirect_uris:
        raise _oauth_error("invalid_request", "That redirect URI is not registered for the client.")
    extra = {"state": payload.state} if payload.state else {}
    if not payload.approve:
        query = urlencode({"error": "access_denied", **extra})
        return {"redirect_to": f"{payload.redirect_uri}?{query}"}
    if payload.response_type != "code" or payload.code_challenge_method != "S256":
        raise _oauth_error("invalid_request", "Only response_type=code with S256 PKCE.")
    scopes = _requested(payload.scope, user)
    if not scopes:
        raise _oauth_error("invalid_scope", "None of the scopes asked for are yours to grant.")
    code = secrets.token_urlsafe(32)
    session.add(
        OAuthAuthorizationCode(
            tenant_id=user.tenant_id,
            code_hash=_hash(code),
            client_id=client.client_id,
            user_id=user.id,
            redirect_uri=payload.redirect_uri,
            code_challenge=payload.code_challenge,
            scopes=scopes,
            expires_at=datetime.now(UTC) + CODE_LIFETIME,
        )
    )
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="oauth_consent",
        actor=user,
        changes={"client": client.client_name, "scopes": scopes},
        summary=f"Allowed {client.client_name} to read AskMira as {user.full_name}",
        request=request,
    )
    await session.commit()
    query = urlencode({"code": code, **extra})
    return {"redirect_to": f"{payload.redirect_uri}?{query}"}


# ── Tokens ──────────────────────────────────────────────────────────────────


def _issue(grant: OAuthGrant) -> dict[str, Any]:
    access = f"amo_{secrets.token_urlsafe(32)}"
    refresh_token = f"amr_{secrets.token_urlsafe(32)}"
    now = datetime.now(UTC)
    grant.access_token_hash = _hash(access)
    grant.access_expires_at = now + ACCESS_LIFETIME
    grant.refresh_token_hash = _hash(refresh_token)
    grant.refresh_expires_at = now + REFRESH_LIFETIME
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": int(ACCESS_LIFETIME.total_seconds()),
        "refresh_token": refresh_token,
        "scope": " ".join(grant.scopes),
    }


def _pkce_ok(verifier: str, challenge: str) -> bool:
    digest = hashlib.sha256(verifier.encode()).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return secrets.compare_digest(computed, challenge)


async def exchange_code(
    session: AsyncSession, *, code: str, client_id: str, redirect_uri: str, code_verifier: str
) -> dict[str, Any]:
    row = await session.scalar(
        select(OAuthAuthorizationCode).where(OAuthAuthorizationCode.code_hash == _hash(code))
    )
    now = datetime.now(UTC)
    if row is None or row.client_id != client_id:
        raise _oauth_error("invalid_grant", "Unknown code.")
    if row.used_at is not None:
        # A replayed code: whatever it produced is no longer trusted.
        for grant in (
            await session.scalars(
                select(OAuthGrant).where(
                    tenant_filter(OAuthGrant, row.tenant_id),
                    OAuthGrant.client_id == row.client_id,
                    OAuthGrant.user_id == row.user_id,
                    OAuthGrant.created_at >= row.created_at,
                )
            )
        ).all():
            grant.revoked_at = now
        await session.commit()
        raise _oauth_error("invalid_grant", "This code was already used.")
    if row.expires_at <= now:
        raise _oauth_error("invalid_grant", "The code has expired.")
    if row.redirect_uri != redirect_uri:
        raise _oauth_error("invalid_grant", "The redirect URI does not match.")
    if not _pkce_ok(code_verifier, row.code_challenge):
        raise _oauth_error("invalid_grant", "The PKCE verifier does not match.")
    row.used_at = now
    grant = OAuthGrant(
        tenant_id=row.tenant_id,
        client_id=row.client_id,
        user_id=row.user_id,
        scopes=list(row.scopes),
        access_token_hash="",
        access_expires_at=now,
        refresh_token_hash="",
        refresh_expires_at=now,
    )
    issued = _issue(grant)
    session.add(grant)
    await session.commit()
    return issued


async def refresh(session: AsyncSession, *, refresh_token: str, client_id: str) -> dict[str, Any]:
    grant = await session.scalar(
        select(OAuthGrant).where(OAuthGrant.refresh_token_hash == _hash(refresh_token))
    )
    now = datetime.now(UTC)
    if (
        grant is None
        or grant.client_id != client_id
        or grant.revoked_at is not None
        or grant.refresh_expires_at <= now
    ):
        raise _oauth_error("invalid_grant", "Unknown, expired or revoked refresh token.")
    issued = _issue(grant)
    await session.commit()
    return issued


async def revoke_token(session: AsyncSession, *, token: str) -> None:
    """RFC 7009: revoking either token ends the grant. Unknown tokens are not an error."""
    digest = _hash(token)
    grant = await session.scalar(
        select(OAuthGrant).where(
            (OAuthGrant.access_token_hash == digest) | (OAuthGrant.refresh_token_hash == digest)
        )
    )
    if grant is not None and grant.revoked_at is None:
        grant.revoked_at = datetime.now(UTC)
        await session.commit()


async def token_identity(session: AsyncSession, token: str) -> tuple[User, frozenset[str]]:
    """The person an access token acts as, and the scopes it may use today; or 401."""
    grant = await session.scalar(
        select(OAuthGrant).where(OAuthGrant.access_token_hash == _hash(token))
    )
    now = datetime.now(UTC)
    if grant is None or grant.revoked_at is not None or grant.access_expires_at <= now:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown, expired or revoked token."
        )
    user = await session.scalar(
        select(User).where(tenant_filter(User, grant.tenant_id), User.id == grant.user_id)
    )
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="This person can no longer sign in."
        )
    user.has_reports = await has_direct_reports(session, user)
    grant.last_used_at = now
    await session.commit()
    return user, frozenset(set(grant.scopes) & allowed_scopes(user))


# ── The person's connected apps ─────────────────────────────────────────────


async def list_connections(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    grants = (
        await session.scalars(
            tenant_scoped(
                select(OAuthGrant)
                .where(OAuthGrant.user_id == user.id, OAuthGrant.revoked_at.is_(None))
                .order_by(OAuthGrant.created_at.desc()),
                OAuthGrant,
                user,
            )
        )
    ).all()
    names = {
        c.client_id: c.client_name
        for c in (
            await session.scalars(
                select(OAuthClient).where(OAuthClient.client_id.in_({g.client_id for g in grants}))
            )
        ).all()
    } if grants else {}
    return [
        {
            "id": str(g.id),
            "client_name": names.get(g.client_id, "An MCP client"),
            "scopes": g.scopes,
            "connected_at": g.created_at.isoformat() if g.created_at else None,
            "last_used_at": g.last_used_at.isoformat() if g.last_used_at else None,
        }
        for g in grants
    ]


async def revoke_connection(
    session: AsyncSession, user: User, grant_id: uuid.UUID, *, request: Request | None = None
) -> None:
    grant = await session.scalar(
        tenant_scoped(
            select(OAuthGrant).where(OAuthGrant.id == grant_id, OAuthGrant.user_id == user.id),
            OAuthGrant,
            user,
        )
    )
    if grant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    grant.revoked_at = datetime.now(UTC)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="oauth_grant",
        entity_id=grant.id,
        actor=user,
        summary="Disconnected an MCP client",
        request=request,
    )
    await session.commit()
