"""The platform console: the super admin creates and runs every workspace.

═══════════════════════════════════════════════════════════════════════════════
NOBODY ELSE CAN MAKE A WORKSPACE
═══════════════════════════════════════════════════════════════════════════════

There is no public sign-up. A workspace exists because the platform's super
admin created it, with its first admin. Everything inside it — departments,
people, roles, payroll — is then that admin's to set up, and the super admin
has no way into it: this console lists workspaces and their admins' logins,
and reads nothing else of theirs.

Listing every workspace is this file's job, so its queries are not scoped to a
tenant. Every entry point refuses anyone but a super admin signed in to their
own workspace, and every action is written to the audit trail of the workspace
it touched.

A new admin gets a temporary password, shown once to the super admin, and has
to choose their own at first sign-in. Nothing is emailed from here.
"""

from __future__ import annotations

import re
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator
from sqlalchemy import func, select

from app.core import permissions
from app.core.deps import tenant_filter
from app.core.security import hash_password
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.org import LegalEntity, Location
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.org import INDIAN_STATE_CODES
from app.services import audit
from app.services.auth import (
    DEFAULT_ENABLED_MODULES,
    RESERVED_SLUGS,
    create_workspace,
    email_in_use,
)
from app.services.gst.gstin import GST_CODES_BY_STATE, validate_gstin
from app.services.identity.directory import logo_url, normalise_mobile

__all__ = [
    "AdminIn",
    "WorkspaceIn",
    "add_admin",
    "create",
    "list_workspaces",
    "reset_admin_password",
    "set_active",
    "upload_logo",
]

_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


_PAN = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
_TAN = re.compile(r"^[A-Z]{4}[0-9]{5}[A-Z]$")
_CIN = re.compile(r"^[LU][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6}$")
_LLPIN = re.compile(r"^[A-Z]{3}-[0-9]{4}$")
_PINCODE = re.compile(r"^[1-9][0-9]{5}$")
_MOBILE = re.compile(r"^[6-9][0-9]{9}$")

#: What a company may be, as Indian law registers it.
CompanyType = Literal[
    "private_limited",
    "public_limited",
    "llp",
    "partnership",
    "proprietorship",
    "opc",
    "trust_society",
    "government",
    "other",
]
CompanySize = Literal["1-50", "51-200", "201-500", "501-1000", "1001-5000", "5000+"]


def _upper(value: object) -> object:
    return value.strip().upper() if isinstance(value, str) else value


def _blank_to_none(value: object) -> object:
    if isinstance(value, str) and not value.strip():
        return None
    return value


class WorkspaceIn(BaseModel):
    """Everything about a company the platform needs to set it up properly.

    Its legal identity (PAN, TAN, GSTIN, CIN, PF and ESI) goes on its first legal
    entity, its registered office becomes its first location, and the rest is
    its profile. Payroll, statutory filings and Professional Tax read these, so
    collecting them here means the company's admin starts with them in place.
    """

    model_config = ConfigDict(extra="forbid")

    # ── The company ─────────────────────────────────────────────────────────
    company_name: str = Field(min_length=2, max_length=255)
    legal_name: str = Field(min_length=2, max_length=255)
    #: Optional; made from the company name when left out.
    workspace: str | None = Field(default=None, max_length=64)
    company_type: CompanyType = "private_limited"
    industry: str | None = Field(default=None, max_length=120)
    company_size: CompanySize | None = None
    website: str | None = Field(default=None, max_length=255)
    contact_email: EmailStr | None = None
    contact_phone: str | None = Field(default=None, max_length=20)

    # ── Statutory identity ──────────────────────────────────────────────────
    pan: str
    tan: str | None = None
    gstin: str | None = None
    #: CIN for a company, LLPIN for an LLP.
    cin: str | None = None
    pf_registration_no: str | None = Field(default=None, max_length=32)
    esi_registration_no: str | None = Field(default=None, max_length=32)

    # ── Registered office ───────────────────────────────────────────────────
    address_line1: str = Field(min_length=3, max_length=200)
    address_line2: str | None = Field(default=None, max_length=200)
    city: str = Field(min_length=2, max_length=128)
    state_code: str
    pincode: str

    # ── The first admin ─────────────────────────────────────────────────────
    admin_full_name: str = Field(min_length=2, max_length=255)
    admin_email: EmailStr
    admin_phone: str
    admin_designation: str | None = Field(default=None, max_length=120)

    #: Modules to switch on; the rest keep their defaults.
    modules: dict[str, bool] = Field(default_factory=dict)

    _blanks = field_validator(
        "workspace",
        "industry",
        "website",
        "contact_phone",
        "tan",
        "gstin",
        "cin",
        "pf_registration_no",
        "esi_registration_no",
        "address_line2",
        "admin_designation",
        mode="before",
    )(_blank_to_none)
    _caps = field_validator("pan", "tan", "gstin", "cin", "state_code", mode="before")(_upper)

    @field_validator("pan")
    @classmethod
    def _pan(cls, value: str) -> str:
        if not _PAN.match(value):
            raise ValueError("A PAN is five letters, four digits and a letter, like AAACA1234B.")
        return value

    @field_validator("tan")
    @classmethod
    def _tan(cls, value: str | None) -> str | None:
        if value is not None and not _TAN.match(value):
            raise ValueError("A TAN is four letters, five digits and a letter, like BLRA12345C.")
        return value

    @field_validator("cin")
    @classmethod
    def _cin(cls, value: str | None) -> str | None:
        if value is not None and not (_CIN.match(value) or _LLPIN.match(value)):
            raise ValueError("That is not a CIN (21 characters) or an LLPIN (like AAB-1234).")
        return value

    @field_validator("state_code")
    @classmethod
    def _state(cls, value: str) -> str:
        if value not in INDIAN_STATE_CODES:
            raise ValueError(f"'{value}' is not an Indian state or union territory code.")
        return value

    @field_validator("pincode")
    @classmethod
    def _pincode(cls, value: str) -> str:
        value = value.strip()
        if not _PINCODE.match(value):
            raise ValueError("A PIN code is six digits and does not start with 0.")
        return value

    @field_validator("admin_phone")
    @classmethod
    def _admin_phone(cls, value: str) -> str:
        national = normalise_mobile(value)
        if national is None or not _MOBILE.match(national[-10:]):
            raise ValueError("Give the admin's 10-digit Indian mobile number.")
        return national[-10:]

    @field_validator("website")
    @classmethod
    def _website(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        if not value.startswith(("http://", "https://")):
            value = f"https://{value}"
        return value

    @field_validator("modules")
    @classmethod
    def _modules(cls, value: dict[str, bool]) -> dict[str, bool]:
        unknown = sorted(set(value) - set(DEFAULT_ENABLED_MODULES))
        if unknown:
            raise ValueError(f"Unknown module(s): {', '.join(unknown)}.")
        return value

    @model_validator(mode="after")
    def _gstin_matches(self) -> WorkspaceIn:
        if self.gstin is None:
            return self
        verdict = validate_gstin(self.gstin)
        if not verdict.is_valid:
            raise ValueError(f"GSTIN: {verdict.message}")
        if verdict.pan != self.pan:
            raise ValueError(
                f"The GSTIN carries PAN {verdict.pan}, not the company's PAN {self.pan}."
            )
        allowed = GST_CODES_BY_STATE.get(self.state_code, frozenset())
        if verdict.state_code not in allowed:
            raise ValueError(
                f"The GSTIN is registered in {verdict.state_name}, but the registered office "
                f"is in {self.state_code}. Give the GSTIN for the registered office's state."
            )
        return self

    def registered_address(self) -> str:
        parts = [self.address_line1, self.address_line2, self.city]
        return ", ".join(p.strip() for p in parts if p) + f", {self.state_code} {self.pincode}"


class AdminIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str = Field(min_length=2, max_length=255)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=32)


def _require_super(user: User) -> None:
    if user.role is not UserRole.SUPER_ADMIN or getattr(user, "operator_grant_id", None):
        raise permissions.forbidden("Only the platform's super admin manages workspaces.")


def _temporary_password() -> str:
    # Meets the password policy: letters, a digit, length.
    return secrets.token_urlsafe(12) + "-Aa1"


async def _workspace(session: Any, tenant_id: uuid.UUID) -> Tenant:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such workspace.")
    return tenant


def _admin_read(row: User) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "full_name": row.full_name,
        "email": row.email,
        "is_active": row.is_active,
        "must_change_password": bool(row.must_change_password),
        "last_login_at": row.last_login_at.isoformat() if row.last_login_at else None,
    }


async def list_workspaces(session: Any, user: User) -> list[dict[str, Any]]:
    """Every workspace, with its admins and how big it is. Nothing from inside it."""
    _require_super(user)
    tenants = (await session.scalars(select(Tenant).order_by(Tenant.created_at))).all()
    out: list[dict[str, Any]] = []
    for tenant in tenants:
        admins = (
            await session.scalars(
                select(User)
                .where(tenant_filter(User, tenant.id), User.role == UserRole.ADMIN)
                .order_by(User.created_at)
            )
        ).all()
        logins = await session.scalar(
            select(func.count(User.id)).where(
                tenant_filter(User, tenant.id), User.is_active.is_(True)
            )
        )
        employees = await session.scalar(
            select(func.count(Employee.id)).where(
                tenant_filter(Employee, tenant.id), Employee.deleted_at.is_(None)
            )
        )
        last_sign_in = await session.scalar(
            select(func.max(User.last_login_at)).where(tenant_filter(User, tenant.id))
        )
        entity = await session.scalar(
            select(LegalEntity)
            .where(tenant_filter(LegalEntity, tenant.id))
            .order_by(LegalEntity.created_at)
            .limit(1)
        )
        office = await session.scalar(
            select(Location)
            .where(tenant_filter(Location, tenant.id))
            .order_by(Location.created_at)
            .limit(1)
        )
        profile = (tenant.settings or {}).get("profile") or {}
        out.append(
            {
                "id": str(tenant.id),
                "name": tenant.name,
                "workspace": tenant.slug,
                "is_active": tenant.is_active,
                "is_own": tenant.id == user.tenant_id,
                "logo_url": logo_url(tenant),
                "legal_name": entity.name if entity else profile.get("legal_name"),
                "pan": entity.pan if entity else None,
                "gstin": entity.gstin if entity else None,
                "city": office.city if office else None,
                "state_code": office.state_code if office else None,
                "industry": profile.get("industry"),
                "company_size": profile.get("company_size"),
                "created_at": tenant.created_at.isoformat() if tenant.created_at else None,
                "active_logins": int(logins or 0),
                "employees": int(employees or 0),
                "last_sign_in": last_sign_in.isoformat() if last_sign_in else None,
                "modules_on": sorted(k for k, on in (tenant.enabled_modules or {}).items() if on),
                "admins": [_admin_read(a) for a in admins],
            }
        )
    return out


async def create(
    session: Any, user: User, payload: WorkspaceIn, *, request: Request | None
) -> dict[str, Any]:
    """A new workspace and its first admin; the temporary password comes back once."""
    _require_super(user)
    slug = None
    if payload.workspace:
        slug = payload.workspace.strip().lower()
        if not _SLUG.match(slug):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="A workspace address uses lowercase letters, digits and hyphens.",
            )
        taken = await session.scalar(select(Tenant.id).where(Tenant.slug == slug))
        if taken is not None or slug in RESERVED_SLUGS:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Another workspace already has that address.",
            )

    temporary = _temporary_password()
    tenant, admin = await create_workspace(
        session,
        company_name=payload.company_name,
        admin_full_name=payload.admin_full_name,
        admin_email=str(payload.admin_email),
        admin_phone=payload.admin_phone,
        password_hash=hash_password(temporary),
        slug=slug,
        must_change_password=True,
        modules=payload.modules,
        profile={
            "legal_name": payload.legal_name.strip(),
            "company_type": payload.company_type,
            "industry": payload.industry,
            "company_size": payload.company_size,
            "website": payload.website,
            "contact_email": str(payload.contact_email) if payload.contact_email else None,
            "contact_phone": payload.contact_phone,
            "cin": payload.cin,
        },
        legal_entity={
            "name": payload.legal_name.strip(),
            "pan": payload.pan,
            "tan": payload.tan,
            "gstin": payload.gstin,
            "pf_registration_no": payload.pf_registration_no,
            "esi_registration_no": payload.esi_registration_no,
            "registered_address": payload.registered_address()[:512],
        },
        head_office={
            "name": f"{payload.city.strip()} — Head office",
            "city": payload.city.strip(),
            "state_code": payload.state_code,
            "pincode": payload.pincode,
        },
    )
    if payload.admin_designation:
        employee = await session.get(Employee, admin.employee_id)
        if employee is not None:
            employee.designation = payload.admin_designation.strip()
    await audit.record(
        session,
        tenant_id=tenant.id,
        action=AuditAction.CREATE,
        entity_type="tenant",
        entity_id=tenant.id,
        actor_user_id=user.id,
        actor_type="platform",
        summary=f"Workspace {tenant.name} created with admin {admin.email} by the platform",
        request=request,
    )
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="platform_workspace",
        entity_id=tenant.id,
        actor=user,
        summary=f"Created workspace {tenant.name} ({tenant.slug}) with admin {admin.email}",
        request=request,
    )
    await session.commit()
    return {
        "workspace": {"id": str(tenant.id), "name": tenant.name, "workspace": tenant.slug},
        "admin": {"email": admin.email, "temporary_password": temporary},
        "note": (
            "Give the admin this password yourself. It is shown once, and they choose "
            "their own at first sign-in."
        ),
    }


async def upload_logo(
    session: Any, user: User, tenant_id: uuid.UUID, data: bytes, *, request: Request | None
) -> dict[str, Any]:
    """The company's logo, shown on its own sign-in page."""
    _require_super(user)
    tenant = await _workspace(session, tenant_id)
    from app.services import branding

    return await branding.save_logo(session, user, tenant, data, request=request)


async def add_admin(
    session: Any, user: User, tenant_id: uuid.UUID, payload: AdminIn, *, request: Request | None
) -> dict[str, Any]:
    """Another admin for a workspace — when its only admin has left, for instance."""
    _require_super(user)
    tenant = await _workspace(session, tenant_id)
    email = str(payload.email).strip().lower()
    if await email_in_use(session, email):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That email already signs in to AskMira. Each person has one login.",
        )
    temporary = _temporary_password()
    admin = User(
        tenant_id=tenant.id,
        email=email,
        password_hash=hash_password(temporary),
        full_name=payload.full_name.strip(),
        phone=payload.phone,
        role=UserRole.ADMIN,
        is_active=True,
        is_email_verified=False,
        must_change_password=True,
    )
    session.add(admin)
    await session.flush()
    await audit.record(
        session,
        tenant_id=tenant.id,
        action=AuditAction.PERMISSION_CHANGE,
        entity_type="user",
        entity_id=admin.id,
        actor_user_id=user.id,
        actor_type="platform",
        summary=f"Admin {admin.email} added by the platform",
        request=request,
    )
    await session.commit()
    return {
        "admin": {"email": admin.email, "temporary_password": temporary},
        "note": "Shown once. Their admin can link this login to an employee record.",
    }


async def reset_admin_password(
    session: Any,
    user: User,
    tenant_id: uuid.UUID,
    admin_id: uuid.UUID,
    *,
    request: Request | None,
) -> dict[str, Any]:
    """A temporary password for a workspace admin who is locked out."""
    _require_super(user)
    tenant = await _workspace(session, tenant_id)
    admin = await session.scalar(
        select(User).where(
            tenant_filter(User, tenant.id), User.id == admin_id, User.role == UserRole.ADMIN
        )
    )
    if admin is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such admin.")
    temporary = _temporary_password()
    admin.password_hash = hash_password(temporary)
    admin.must_change_password = True
    # Every session they had ends now.
    admin.tokens_valid_from = datetime.now(UTC)
    await audit.record(
        session,
        tenant_id=tenant.id,
        action=AuditAction.UPDATE,
        entity_type="user",
        entity_id=admin.id,
        actor_user_id=user.id,
        actor_type="platform",
        summary=f"Temporary password issued to admin {admin.email} by the platform",
        request=request,
    )
    await session.commit()
    return {
        "admin": {"email": admin.email, "temporary_password": temporary},
        "note": "Shown once. Their sessions were ended; they choose a new password at sign-in.",
    }


async def set_active(
    session: Any,
    user: User,
    tenant_id: uuid.UUID,
    *,
    active: bool,
    reason: str,
    request: Request | None,
) -> dict[str, Any]:
    """Suspend or reactivate a workspace. Suspended, nobody in it can sign in or act."""
    _require_super(user)
    tenant = await _workspace(session, tenant_id)
    if tenant.id == user.tenant_id and not active:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The platform's own workspace cannot be suspended from here.",
        )
    if not reason.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Give a reason."
        )
    if tenant.is_active != active:
        tenant.is_active = active
        await audit.record(
            session,
            tenant_id=tenant.id,
            action=AuditAction.UPDATE,
            entity_type="tenant",
            entity_id=tenant.id,
            actor_user_id=user.id,
            actor_type="platform",
            summary=(
                f"Workspace {'reactivated' if active else 'suspended'} by the platform: "
                f"{reason.strip()}"
            ),
            request=request,
        )
        await session.commit()
    return {"id": str(tenant.id), "is_active": tenant.is_active}
