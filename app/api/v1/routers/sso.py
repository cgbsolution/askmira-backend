"""SSO and identity-provider endpoints (slice 04).

The ACS and callback endpoints are unauthenticated by nature — they are where
authentication *happens*. Everything they trust is verified cryptographically
before it is read.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import select

from app.core import cookies
from app.core.config import settings
from app.core.crypto import encrypt_secret
from app.core.deps import (
    CurrentUser,
    DbSession,
    has_direct_reports,
    require_roles,
    tenant_scoped,
)
from app.core.security import create_access_token, create_refresh_token
from app.models.audit import AuditAction
from app.models.identity import IdentityProvider, IdpType, SsoLoginAttempt
from app.models.tenant import Tenant
from app.models.user import UserRole
from app.schemas.auth import AuthResponse, TenantRead, UserRead
from app.services import audit
from app.services.identity import oidc as oidc_service
from app.services.identity import saml as saml_service
from app.services.identity import sso as sso_service
from app.services.identity import sso_admin

router = APIRouter(prefix="/sso", tags=["sso"])

ADMIN_ROLES = (UserRole.ADMIN,)

# How long a started login may sit before its state is refused.
LOGIN_ATTEMPT_TTL = timedelta(minutes=10)


# ── Contracts ───────────────────────────────────────────────────────────────


class ProviderCreate(BaseModel):
    """No role or group attribute mapping — deliberately.

    Roles are assigned in AskMira. Offering a field for an IdP role claim would
    invite exactly the escalation path this design refuses.
    """

    name: str = Field(min_length=1, max_length=128)
    idp_type: IdpType
    enabled: bool = True

    entity_id: str | None = None
    sso_url: str | None = None
    slo_url: str | None = None
    x509_cert: str | None = None

    issuer: str | None = None
    discovery_url: str | None = None
    client_id: str | None = None
    client_secret: str | None = Field(
        default=None, description="Stored encrypted; never returned by any endpoint."
    )

    email_attribute: str = "email"
    first_name_attribute: str = "given_name"
    last_name_attribute: str = "family_name"
    employee_id_attribute: str | None = None

    enforce_sso: bool = False
    allowed_email_domains: list[str] = Field(default_factory=list)
    jit_provisioning: bool = True


class ProviderRead(BaseModel):
    """Note the absence of any secret field. There is no read path for one."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    idp_type: IdpType
    enabled: bool
    entity_id: str | None
    sso_url: str | None
    issuer: str | None
    discovery_url: str | None
    client_id: str | None
    enforce_sso: bool
    allowed_email_domains: list[str]
    jit_provisioning: bool
    has_client_secret: bool = False


class DiscoverRequest(BaseModel):
    email: EmailStr


class DiscoverResponse(BaseModel):
    """What the sign-in screen needs after the user types their email.

    Returning `sso_required` lets the screen hide the password field entirely
    rather than letting someone type a password that will be refused.
    """

    sso_available: bool
    sso_required: bool = False
    provider_id: uuid.UUID | None = None
    provider_name: str | None = None
    idp_type: IdpType | None = None
    start_url: str | None = None


# ── Provider administration ─────────────────────────────────────────────────


@router.get(
    "/providers",
    response_model=list[ProviderRead],
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def list_providers(user: CurrentUser, session: DbSession) -> list[ProviderRead]:
    stmt = tenant_scoped(
        select(IdentityProvider).order_by(IdentityProvider.name), IdentityProvider, user
    )
    rows = (await session.scalars(stmt)).all()
    return [
        ProviderRead.model_validate(row).model_copy(
            update={"has_client_secret": bool(row.client_secret_encrypted)}
        )
        for row in rows
    ]


@router.post(
    "/providers",
    response_model=ProviderRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(UserRole.ADMIN))],
)
async def create_provider(
    payload: ProviderCreate, user: CurrentUser, session: DbSession, request: Request
) -> ProviderRead:
    """Configure an identity provider.

    Enabling `enforce_sso` requires a break-glass account to already be set on
    the tenant. Without one, a misconfigured IdP locks every administrator out
    with no way back in — an outage that cannot be fixed from inside the
    product.
    """
    if payload.enforce_sso:
        tenant = await session.get(Tenant, user.tenant_id)
        if not (tenant.settings or {}).get("break_glass_email"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "Set a break-glass admin account before enforcing SSO. Without one, "
                    "a misconfigured identity provider locks everyone out of this "
                    "workspace permanently."
                ),
            )

    data = payload.model_dump(exclude={"client_secret"})
    provider = IdentityProvider(
        tenant_id=user.tenant_id,
        client_secret_encrypted=(
            encrypt_secret(payload.client_secret, user.tenant_id) if payload.client_secret else None
        ),
        **data,
    )
    session.add(provider)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.PERMISSION_CHANGE,
        entity_type="identity_provider",
        entity_id=provider.id,
        actor=user,
        changes={
            "name": provider.name,
            "type": provider.idp_type.value,
            "enforce_sso": provider.enforce_sso,
            "domains": provider.allowed_email_domains,
        },
        summary=f"Configured identity provider {provider.name}",
        request=request,
    )
    await session.commit()
    await session.refresh(provider)
    return ProviderRead.model_validate(provider).model_copy(
        update={"has_client_secret": bool(provider.client_secret_encrypted)}
    )


# ── Tenant resolution ───────────────────────────────────────────────────────


@router.post("/discover", response_model=DiscoverResponse)
async def discover_provider(payload: DiscoverRequest, session: DbSession) -> DiscoverResponse:
    """Route an email address to its workspace's identity provider.

    This is what lets the sign-in screen ask for an email and nothing else.
    Unauthenticated by necessity — it runs before anyone is signed in — and it
    reveals only whether a domain has SSO configured, which the IdP's own
    login page would reveal anyway.
    """
    found = await sso_service.provider_for_email(session, payload.email)
    if found is None:
        return DiscoverResponse(sso_available=False)

    provider, _tenant = found
    return DiscoverResponse(
        sso_available=True,
        sso_required=provider.enforce_sso,
        provider_id=provider.id,
        provider_name=provider.name,
        idp_type=provider.idp_type,
        start_url=f"{settings.API_V1_PREFIX}/sso/start/{provider.id}",
    )


# ── OIDC ────────────────────────────────────────────────────────────────────


@router.get("/start/{provider_id}")
async def start_login(
    provider_id: uuid.UUID, session: DbSession, redirect_uri: str
) -> dict[str, str]:
    """Begin a login. Returns the URL to send the browser to.

    `state`, `nonce` and the PKCE verifier are stored server-side. Comparing a
    value the client sent against a value the same client sent proves nothing.
    """
    provider = await session.scalar(
        select(IdentityProvider).where(
            IdentityProvider.id == provider_id, IdentityProvider.enabled.is_(True)
        )
    )
    if provider is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown provider")

    if provider.idp_type is IdpType.SAML:
        if not provider.sso_url:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This SAML provider has no sign-on URL configured",
            )
        # SP-initiated SAML: the browser is redirected to the IdP, which POSTs
        # back to the ACS endpoint.
        return {"authorization_url": provider.sso_url, "type": "saml"}

    if not provider.discovery_url or not provider.client_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This OIDC provider is missing its discovery URL or client id",
        )

    try:
        discovery = await oidc_service.discover(provider.discovery_url)
        start = oidc_service.start_login(
            discovery=discovery, client_id=provider.client_id, redirect_uri=redirect_uri
        )
    except oidc_service.OidcError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    session.add(
        SsoLoginAttempt(
            tenant_id=provider.tenant_id,
            provider_id=provider.id,
            state=start.state,
            nonce=start.nonce,
            code_verifier=start.code_verifier,
            redirect_uri=redirect_uri,
            expires_at=datetime.now(UTC) + LOGIN_ATTEMPT_TTL,
        )
    )
    await session.commit()

    return {"authorization_url": start.authorization_url, "type": "oidc"}


@router.post("/oidc/callback", response_model=AuthResponse)
async def oidc_callback(
    session: DbSession,
    request: Request,
    response: Response,
    code: str = Form(...),
    state: str = Form(...),
) -> AuthResponse:
    """Complete an OIDC login and mint our own tokens.

    The tokens returned here are AskMira's, carrying the role AskMira assigned.
    Nothing from the provider's claims influences that.
    """
    attempt = await session.scalar(select(SsoLoginAttempt).where(SsoLoginAttempt.state == state))
    if attempt is None or attempt.consumed_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This sign-in link is not valid or has already been used",
        )
    if attempt.expires_at < datetime.now(UTC):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="This sign-in attempt expired"
        )

    attempt.consumed_at = datetime.now(UTC)
    provider = await session.get(IdentityProvider, attempt.provider_id)
    if provider is None or not provider.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown provider")

    try:
        discovery = await oidc_service.discover(provider.discovery_url or "")
        identity = await oidc_service.exchange_code(
            discovery=discovery,
            client_id=provider.client_id or "",
            client_secret=sso_service.provider_client_secret(provider),
            redirect_uri=attempt.redirect_uri,
            code=code,
            code_verifier=attempt.code_verifier or "",
            expected_nonce=attempt.nonce or "",
        )
    except oidc_service.OidcError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    result = await _complete_login(
        session,
        request,
        provider=provider,
        email=identity.claim(provider.email_attribute) or identity.claim("email") or "",
        first_name=identity.claim(provider.first_name_attribute),
        last_name=identity.claim(provider.last_name_attribute),
        employee_code=(
            identity.claim(provider.employee_id_attribute)
            if provider.employee_id_attribute
            else None
        ),
    )
    # The web portal signs in with session cookies, like a password sign-in.
    cookies.set_session_cookies(
        response,
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        access_seconds=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    if cookies.wants_cookie_transport(request):
        return result.model_copy(update={"access_token": "", "refresh_token": ""})
    return result


# ── SAML ────────────────────────────────────────────────────────────────────


@router.post("/saml/acs/{provider_id}", response_model=AuthResponse)
async def saml_acs(
    provider_id: uuid.UUID,
    session: DbSession,
    request: Request,
    SAMLResponse: str = Form(...),  # noqa: N803 - the field name is fixed by SAML
    RelayState: str | None = Form(default=None),  # noqa: N803
) -> AuthResponse:
    """The assertion consumer service.

    Handles both SP-initiated and IdP-initiated flows: `InResponseTo` is
    checked only when we actually sent a request.
    """
    provider = await session.scalar(
        select(IdentityProvider).where(
            IdentityProvider.id == provider_id,
            IdentityProvider.enabled.is_(True),
            IdentityProvider.idp_type == IdpType.SAML,
        )
    )
    if provider is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown provider")

    expected_in_response_to: str | None = None
    if RelayState:
        attempt = await session.scalar(
            select(SsoLoginAttempt).where(SsoLoginAttempt.state == RelayState)
        )
        if attempt is not None and attempt.consumed_at is None:
            expected_in_response_to = attempt.state
            attempt.consumed_at = datetime.now(UTC)

    acs_url = str(request.url).split("?")[0]

    try:
        assertion = saml_service.parse_and_validate(
            saml_response_b64=SAMLResponse,
            idp_certificate=provider.x509_cert or "",
            expected_audience=provider.entity_id or acs_url,
            expected_destination=acs_url,
            expected_in_response_to=expected_in_response_to,
        )
    except saml_service.SamlValidationError as exc:
        await audit.record(
            session,
            tenant_id=provider.tenant_id,
            action=AuditAction.LOGIN_FAILED,
            entity_type="identity_provider",
            entity_id=provider.id,
            actor_type="system",
            changes={"severity": "high", "reason": str(exc)},
            summary=f"Rejected a SAML assertion from {provider.name}: {exc}",
            request=request,
        )
        await session.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    # Replay protection. Even a perfectly valid assertion is single-use.
    try:
        await sso_service.consume_assertion_id(
            session,
            tenant_id=provider.tenant_id,
            assertion_id=assertion.assertion_id,
            not_on_or_after=assertion.not_on_or_after,
        )
    except sso_service.SsoError as exc:
        await audit.record(
            session,
            tenant_id=provider.tenant_id,
            action=AuditAction.LOGIN_FAILED,
            entity_type="identity_provider",
            entity_id=provider.id,
            actor_type="system",
            changes={"severity": "high", "reason": "replayed_assertion"},
            summary=f"Refused a replayed SAML assertion {assertion.assertion_id}",
            request=request,
        )
        await session.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    return await _complete_login(
        session,
        request,
        provider=provider,
        email=assertion.first(provider.email_attribute) or assertion.name_id,
        first_name=assertion.first(provider.first_name_attribute),
        last_name=assertion.first(provider.last_name_attribute),
        employee_code=(
            assertion.first(provider.employee_id_attribute)
            if provider.employee_id_attribute
            else None
        ),
    )


async def _complete_login(
    session,
    request: Request,
    *,
    provider: IdentityProvider,
    email: str,
    first_name: str | None,
    last_name: str | None,
    employee_code: str | None,
) -> AuthResponse:
    """Provision or find the user, then mint AskMira's own tokens."""
    try:
        user = await sso_service.provision_user(
            session,
            tenant_id=provider.tenant_id,
            provider=provider,
            email=email,
            first_name=first_name,
            last_name=last_name,
            external_employee_code=employee_code,
            request=request,
        )
    except sso_service.SsoError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

    tenant = await session.get(Tenant, provider.tenant_id)

    await audit.record(
        session,
        tenant_id=provider.tenant_id,
        action=AuditAction.LOGIN,
        entity_type="user",
        entity_id=user.id,
        actor=user,
        changes={"method": "sso", "provider": provider.name, "role": user.role.value},
        summary=f"Signed in via {provider.name}",
        request=request,
    )
    await session.commit()
    await session.refresh(user)
    user.has_reports = await has_direct_reports(session, user)

    return AuthResponse(
        # The role in this token is the one AskMira holds, never one the IdP
        # asserted.
        access_token=create_access_token(
            user_id=str(user.id), tenant_id=str(user.tenant_id), role=user.role.value
        ),
        refresh_token=create_refresh_token(user_id=str(user.id), tenant_id=str(user.tenant_id)),
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user=UserRead.model_validate(user),
        tenant=TenantRead.model_validate(tenant),
    )


# ── Break-glass configuration ───────────────────────────────────────────────


class BreakGlassRequest(BaseModel):
    email: EmailStr


@router.post(
    "/break-glass",
    dependencies=[Depends(require_roles(UserRole.ADMIN))],
)
async def set_break_glass(
    payload: BreakGlassRequest, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, str]:
    """Designate the account that may still use a password under enforced SSO.

    Required before SSO can be enforced. Its logins are audited at high
    severity, because a break-glass sign-in is either a genuine IdP outage or
    an incident and both want to be visible.
    """
    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")

    from app.models.user import User

    target = await session.scalar(
        tenant_scoped(select(User).where(User.email == payload.email.lower()), User, user)
    )
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The break-glass account must be an existing user in this workspace",
        )
    if target.role is not UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The break-glass account must be an administrator",
        )

    tenant.settings = {**(tenant.settings or {}), "break_glass_email": payload.email.lower()}

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.PERMISSION_CHANGE,
        entity_type="tenant",
        entity_id=tenant.id,
        actor=user,
        changes={"break_glass_email": payload.email.lower(), "severity": "high"},
        summary=f"Designated {payload.email} as the break-glass account",
        request=request,
    )
    await session.commit()
    return {"break_glass_email": payload.email.lower()}


def _unused_state() -> str:
    return secrets.token_urlsafe(32)


# ── Provider and SCIM administration ────────────────────────────────────────


class ProviderUpdate(BaseModel):
    """Every field optional. `client_secret` is write-only: omit it to keep the
    stored one, send an empty string to clear it. No role or group mapping."""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    enabled: bool | None = None
    entity_id: str | None = None
    sso_url: str | None = None
    slo_url: str | None = None
    x509_cert: str | None = None
    issuer: str | None = None
    discovery_url: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    email_attribute: str | None = None
    first_name_attribute: str | None = None
    last_name_attribute: str | None = None
    employee_id_attribute: str | None = None
    enforce_sso: bool | None = None
    allowed_email_domains: list[str] | None = None
    jit_provisioning: bool | None = None


class ScimTokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)


@router.patch(
    "/providers/{provider_id}",
    response_model=ProviderRead,
    dependencies=[Depends(require_roles(UserRole.ADMIN))],
)
async def update_provider(
    provider_id: uuid.UUID,
    payload: ProviderUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> ProviderRead:
    changes = payload.model_dump(exclude_unset=True, exclude={"client_secret"})
    changes = {key: value for key, value in changes.items() if value is not None}
    provider = await sso_admin.update_provider(
        session,
        user,
        provider_id=provider_id,
        changes=changes,
        client_secret=(
            payload.client_secret if "client_secret" in payload.model_fields_set else None
        ),
        request=request,
    )
    return ProviderRead.model_validate(provider).model_copy(
        update={"has_client_secret": bool(provider.client_secret_encrypted)}
    )


@router.get("/service-provider", dependencies=[Depends(require_roles(*ADMIN_ROLES))])
async def service_provider_details(user: CurrentUser) -> dict[str, str]:
    """What an IdP administrator needs to configure this workspace on their side."""
    base = settings.API_V1_PREFIX
    return {
        "saml_acs_url_template": f"{base}/sso/saml/acs/{{provider_id}}",
        "oidc_redirect_path": f"{base}/sso/oidc/callback",
        "scim_base_path": "/scim/v2",
        "tenant_id": str(user.tenant_id),
        "note": (
            "Paths are relative to this API's public address. Sign-in has been tested "
            "against a mock identity provider only, not a live Azure AD, Okta or Google "
            "Workspace tenant."
        ),
    }


@router.get("/scim-tokens", dependencies=[Depends(require_roles(*ADMIN_ROLES))])
async def list_scim_tokens(user: CurrentUser, session: DbSession) -> list[dict]:
    """Tokens by name and hint. The token itself is never shown again."""
    return await sso_admin.list_tokens(session, user)


@router.post(
    "/scim-tokens",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(UserRole.ADMIN))],
)
async def create_scim_token(
    payload: ScimTokenCreate, user: CurrentUser, session: DbSession, request: Request
) -> dict:
    """Mint a directory token. The response is the only time it is shown."""
    return await sso_admin.create_token(session, user, name=payload.name, request=request)


@router.post(
    "/scim-tokens/{token_id}/revoke",
    dependencies=[Depends(require_roles(UserRole.ADMIN))],
)
async def revoke_scim_token(
    token_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> dict:
    return await sso_admin.revoke_token(session, user, token_id=token_id, request=request)
