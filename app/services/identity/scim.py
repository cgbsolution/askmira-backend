"""SCIM 2.0 provisioning.

═══════════════════════════════════════════════════════════════════════════════
DE-PROVISIONING BEHAVIOUR — READ THIS FIRST
═══════════════════════════════════════════════════════════════════════════════

This is the question every security reviewer asks, so the answer is written
where the code is rather than in a document nobody opens.

When the corporate directory de-provisions a user — a SCIM DELETE, or a PATCH
setting `active` to false — AskMira:

  DOES:
    - mark the `User` inactive, so no new session can be established
    - stamp `tokens_valid_from`, which invalidates every access and refresh
      token already issued to them, on every device, immediately
    - write an audit entry recording the directory as the actor

  DOES NOT:
    - delete the `Employee` record
    - delete payslips, payroll runs, attendance days, leave history, issued
      letters or audit entries

That distinction is deliberate and not negotiable. Payroll and attendance
history are statutory records; an employee leaving does not entitle anyone to
destroy the evidence of what they were paid. A SCIM DELETE from a directory is
an *access* decision, not a records-retention decision. Actual erasure is a
separate, deliberate flow under the retention policy work in slice 26, with its
own approval.
═══════════════════════════════════════════════════════════════════════════════

Everything else here is ordinary SCIM: RFC 7644 shapes, real PATCH semantics
(clients depend on them), paginated lists, and SCIM-shaped errors rather than
this application's own error envelope.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_filter
from app.models.audit import AuditAction
from app.models.identity import ScimToken
from app.models.user import User, UserRole
from app.services import audit

__all__ = [
    "SCIM_USER_SCHEMA",
    "ScimError",
    "apply_patch",
    "authenticate_token",
    "create_scim_token",
    "deactivate_user",
    "get_user",
    "list_users",
    "provision_user",
    "scim_error",
    "to_scim_user",
]

SCIM_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
SCIM_LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
SCIM_ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
SCIM_PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"


class ScimError(Exception):
    """A SCIM-shaped error.

    Carries `scim_type` because clients branch on it — returning this
    application's default error envelope makes a directory integration fail in
    ways that are very hard to diagnose from the directory side.
    """

    def __init__(self, status: int, detail: str, scim_type: str | None = None) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.scim_type = scim_type


def scim_error(status: int, detail: str, scim_type: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schemas": [SCIM_ERROR_SCHEMA],
        "status": str(status),
        "detail": detail,
    }
    if scim_type:
        body["scimType"] = scim_type
    return body


# ── Tokens ──────────────────────────────────────────────────────────────────


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create_scim_token(
    session: AsyncSession, user: User, name: str, request=None
) -> tuple[ScimToken, str]:
    """Mint a SCIM bearer token.

    Returns `(record, plaintext)`. The plaintext is shown once and is then
    unrecoverable — only its hash is stored.
    """
    plaintext = f"scim_{secrets.token_urlsafe(40)}"
    record = ScimToken(
        tenant_id=user.tenant_id,
        name=name,
        token_hash=_hash_token(plaintext),
        token_hint=f"{plaintext[:9]}…{plaintext[-4:]}",
        created_by_user_id=user.id,
    )
    session.add(record)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.PERMISSION_CHANGE,
        entity_type="scim_token",
        entity_id=record.id,
        actor=user,
        changes={"name": name, "hint": record.token_hint},
        summary=f"Created SCIM provisioning token '{name}'",
        request=request,
    )
    await session.commit()
    await session.refresh(record)
    return record, plaintext


async def authenticate_token(session: AsyncSession, authorization: str | None) -> ScimToken:
    """Resolve a bearer token to its tenant.

    Deliberately not tenant-scoped: the token *is* the tenant context here,
    which is why it is looked up by a unique hash rather than by a value the
    caller could enumerate.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise ScimError(401, "A bearer token is required")

    token = authorization.split(" ", 1)[1].strip()
    record = await session.scalar(
        select(ScimToken).where(
            ScimToken.token_hash == _hash_token(token),
            ScimToken.active.is_(True),
            ScimToken.revoked_at.is_(None),
        )
    )
    if record is None:
        raise ScimError(401, "The bearer token is not valid")

    record.last_used_at = datetime.now(UTC)
    return record


# ── Serialisation ───────────────────────────────────────────────────────────


def to_scim_user(user: User) -> dict[str, Any]:
    """Render a user in SCIM shape.

    `roles` is deliberately omitted. Exposing it would invite a directory to
    write it back, and roles are assigned in AskMira.
    """
    given, _, family = (user.full_name or "").partition(" ")
    return {
        "schemas": [SCIM_USER_SCHEMA],
        "id": str(user.id),
        "externalId": user.employee_id and str(user.employee_id),
        "userName": user.email,
        "name": {"givenName": given or user.email, "familyName": family or ""},
        "displayName": user.full_name,
        "emails": [{"value": user.email, "primary": True, "type": "work"}],
        "active": user.is_active,
        "meta": {
            "resourceType": "User",
            "created": user.created_at.isoformat() if user.created_at else None,
            "lastModified": user.updated_at.isoformat() if user.updated_at else None,
            "location": f"/scim/v2/Users/{user.id}",
        },
    }


# ── Users ───────────────────────────────────────────────────────────────────


async def list_users(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    filter_expression: str | None = None,
    start_index: int = 1,
    count: int = 100,
) -> dict[str, Any]:
    """Paginated list. SCIM indexes from 1, not 0."""
    stmt = select(User).where(tenant_filter(User, tenant_id))

    # Directories overwhelmingly send exactly one filter shape:
    # `userName eq "someone@example.com"`. Supporting that precisely is worth
    # more than a half-working general filter parser.
    if filter_expression:
        match = filter_expression.strip()
        if match.lower().startswith("username eq "):
            value = match[len("userName eq ") :].strip().strip('"')
            stmt = stmt.where(func.lower(User.email) == value.lower())
        elif match.lower().startswith("active eq "):
            value = match.split(" ")[-1].strip().lower()
            stmt = stmt.where(User.is_active.is_(value == "true"))
        else:
            raise ScimError(
                400,
                f"This filter is not supported: {filter_expression}. "
                'Supported: userName eq "…", active eq true|false.',
                scim_type="invalidFilter",
            )

    total = await session.scalar(
        select(func.count()).select_from(stmt.subquery())
    ) or 0

    offset = max(0, start_index - 1)
    rows = (
        await session.scalars(stmt.order_by(User.created_at).offset(offset).limit(count))
    ).all()

    return {
        "schemas": [SCIM_LIST_SCHEMA],
        "totalResults": total,
        "startIndex": start_index,
        "itemsPerPage": len(rows),
        "Resources": [to_scim_user(row) for row in rows],
    }


async def provision_user(
    session: AsyncSession, tenant_id: uuid.UUID, payload: dict[str, Any]
) -> User:
    """Create a user from a SCIM POST.

    Assigns `EMPLOYEE` unconditionally. A `roles` attribute in the payload is
    ignored — a directory does not decide who is an administrator here.
    """
    email = (payload.get("userName") or "").strip().lower()
    if not email:
        raise ScimError(400, "userName is required", scim_type="invalidValue")

    existing = await session.scalar(
        select(User).where(tenant_filter(User, tenant_id), func.lower(User.email) == email)
    )
    if existing is not None:
        raise ScimError(409, f"A user with userName {email} already exists", scim_type="uniqueness")

    name = payload.get("name") or {}
    full_name = (
        payload.get("displayName")
        or " ".join(
            part for part in (name.get("givenName"), name.get("familyName")) if part
        )
        or email
    )

    user = User(
        tenant_id=tenant_id,
        email=email,
        password_hash="!scim-no-password",
        full_name=full_name,
        role=UserRole.EMPLOYEE,
        is_active=bool(payload.get("active", True)),
        is_email_verified=True,
    )
    session.add(user)
    await session.flush()

    await audit.record(
        session,
        tenant_id=tenant_id,
        action=AuditAction.CREATE,
        entity_type="user",
        entity_id=user.id,
        actor_type="system",
        changes={
            "email": email,
            "source": "scim",
            "role_assigned": UserRole.EMPLOYEE.value,
            "note": "Role assigned by AskMira; any roles attribute in the payload is ignored",
        },
        summary=f"SCIM provisioned {email}",
    )
    await session.commit()
    await session.refresh(user)
    return user


async def get_user(session: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID) -> User:
    """One user of the token's workspace, or a SCIM 404."""
    user = await session.scalar(
        select(User).where(tenant_filter(User, tenant_id), User.id == user_id)
    )
    if user is None:
        raise ScimError(404, f"No user with id {user_id}")
    return user


async def deactivate_user(
    session: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID
) -> User:
    """De-provision. Revokes access; preserves history.

    See the module docstring — this is the behaviour security reviewers ask
    about, and the answer is that employment records survive.
    """
    user = await session.scalar(
        select(User).where(tenant_filter(User, tenant_id), User.id == user_id)
    )
    if user is None:
        raise ScimError(404, f"No user with id {user_id}")

    user.is_active = False
    # Every token issued before this instant stops working, on every device.
    user.tokens_valid_from = datetime.now(UTC)

    await audit.record(
        session,
        tenant_id=tenant_id,
        action=AuditAction.UPDATE,
        entity_type="user",
        entity_id=user.id,
        actor_type="system",
        changes={
            "is_active": False,
            "sessions_revoked": True,
            "employee_record": "retained",
            "payroll_history": "retained",
        },
        summary=(
            f"SCIM de-provisioned {user.email}: sessions revoked and login disabled. "
            "Employee record, payroll and attendance history retained — a directory "
            "de-provision is an access decision, not a records decision."
        ),
    )
    await session.commit()
    await session.refresh(user)
    return user


# ── PATCH ───────────────────────────────────────────────────────────────────


async def apply_patch(
    session: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID, payload: dict[str, Any]
) -> User:
    """Apply a SCIM PatchOp.

    Real PATCH semantics, because directories depend on them: Azure AD sends
    `replace` on `active` to de-provision rather than a DELETE, and a service
    that only implements DELETE silently never de-provisions anyone.
    """
    if SCIM_PATCH_SCHEMA not in (payload.get("schemas") or []):
        raise ScimError(
            400, f"A PATCH body must declare the {SCIM_PATCH_SCHEMA} schema",
            scim_type="invalidSyntax",
        )

    user = await session.scalar(
        select(User).where(tenant_filter(User, tenant_id), User.id == user_id)
    )
    if user is None:
        raise ScimError(404, f"No user with id {user_id}")

    operations = payload.get("Operations") or []
    if not operations:
        raise ScimError(400, "A PATCH body needs at least one operation", scim_type="invalidValue")

    deactivated = False

    for operation in operations:
        op = str(operation.get("op", "")).lower()
        if op not in {"add", "replace", "remove"}:
            raise ScimError(400, f"Unsupported op '{op}'", scim_type="invalidSyntax")

        path = str(operation.get("path", "")).strip()
        value = operation.get("value")

        # A pathless replace carries a partial resource in `value`.
        if not path and isinstance(value, dict):
            for key, item in value.items():
                deactivated |= _apply_field(user, key, item)
            continue

        deactivated |= _apply_field(user, path, value, removing=(op == "remove"))

    if deactivated:
        # Same guarantee as an explicit de-provision.
        user.tokens_valid_from = datetime.now(UTC)

    await audit.record(
        session,
        tenant_id=tenant_id,
        action=AuditAction.UPDATE,
        entity_type="user",
        entity_id=user.id,
        actor_type="system",
        changes={
            "operations": [str(o.get("op")) + " " + str(o.get("path", "")) for o in operations],
            "sessions_revoked": deactivated,
            "employee_record": "retained",
        },
        summary=(
            f"SCIM patched {user.email}"
            + (
                "; deactivated, sessions revoked, employment history retained"
                if deactivated
                else ""
            )
        ),
    )
    await session.commit()
    await session.refresh(user)
    return user


def _apply_field(user: User, path: str, value: Any, *, removing: bool = False) -> bool:
    """Apply one field. Returns True when this deactivated the user.

    Unknown paths are ignored rather than rejected: directories send extended
    attributes we have no column for, and failing the whole PATCH over one of
    them breaks provisioning for reasons the directory admin cannot see.

    `roles` and `groups` are ignored **on purpose** — a directory cannot make
    someone an administrator here.
    """
    field = path.split(".")[0].split(":")[-1].lower()

    if field == "active":
        active = False if removing else bool(value)
        was_active = user.is_active
        user.is_active = active
        return was_active and not active

    if field == "displayname":
        user.full_name = "" if removing else str(value)
    elif field == "name":
        if isinstance(value, dict):
            given = value.get("givenName") or ""
            family = value.get("familyName") or ""
            combined = " ".join(part for part in (given, family) if part)
            if combined:
                user.full_name = combined
    elif field == "username":
        if not removing and value:
            user.email = str(value).strip().lower()
    elif field == "emails" and isinstance(value, list) and value:
        primary = next((e for e in value if e.get("primary")), value[0])
        if primary.get("value"):
            user.email = str(primary["value"]).strip().lower()
    elif field == "phonenumbers" and isinstance(value, list) and value:
        user.phone = str(value[0].get("value") or "")[:32]

    # roles, groups, entitlements and anything else: ignored.
    return False
