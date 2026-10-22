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

Deleting a workspace closes it for good without erasing it: its audit trail is
insert-only and its payroll and statutory records must be kept, so the rows
stay where nobody can reach them. See `delete`.
"""

from __future__ import annotations

import re
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import HTTPException, Request, status
from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import String, cast, func, select
from sqlalchemy import update as sql_update

from app.core import permissions
from app.core.deps import tenant_filter
from app.core.security import hash_password
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.identity import DeviceIntegration, DeviceStatus, ScimToken
from app.models.integrations import ApiKey
from app.models.jobboard import CandidatePortalToken, CareersPageConfig
from app.models.oauth import OAuthGrant
from app.models.operators import OperatorGrant
from app.models.opsec import UserSession
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
from app.services.identity.welcome import send_welcome

__all__ = [
    "AdminIn",
    "CompanyDetails",
    "WorkspaceIn",
    "WorkspaceUpdate",
    "add_admin",
    "create",
    "delete",
    "list_workspaces",
    "remove_logo",
    "reset_admin_password",
    "set_active",
    "update",
    "upload_logo",
    "workspace_detail",
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


class CompanyDetails(BaseModel):
    """Everything about a company the platform keeps, checked as a whole.

    Its legal identity (PAN, TAN, GSTIN, CIN, PF and ESI) lives on its first
    legal entity, its registered office is its first location, and the rest is
    its profile. Payroll, statutory filings and Professional Tax read these.
    Creating and editing both pass through this model, so an edit is held to
    the same rules as the form that made the company.
    """

    model_config = ConfigDict(extra="forbid")

    # ── The company ─────────────────────────────────────────────────────────
    company_name: str = Field(min_length=2, max_length=255)
    legal_name: str = Field(min_length=2, max_length=255)
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

    #: Modules to switch on; the rest keep their defaults.
    modules: dict[str, bool] = Field(default_factory=dict)

    _blanks = field_validator(
        "industry",
        "company_size",
        "website",
        "contact_email",
        "contact_phone",
        "tan",
        "gstin",
        "cin",
        "pf_registration_no",
        "esi_registration_no",
        "address_line2",
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
    def _gstin_matches(self) -> CompanyDetails:
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

    def profile(self) -> dict[str, Any]:
        """What is kept on the workspace itself rather than on its legal entity."""
        return {
            "legal_name": self.legal_name.strip(),
            "company_type": self.company_type,
            "industry": self.industry,
            "company_size": self.company_size,
            "website": self.website,
            "contact_email": str(self.contact_email) if self.contact_email else None,
            "contact_phone": self.contact_phone,
            "cin": self.cin,
            "address_line1": self.address_line1.strip(),
            "address_line2": self.address_line2.strip() if self.address_line2 else None,
        }

    def legal_entity(self) -> dict[str, Any]:
        return {
            "name": self.legal_name.strip(),
            "pan": self.pan,
            "tan": self.tan,
            "gstin": self.gstin,
            "pf_registration_no": self.pf_registration_no,
            "esi_registration_no": self.esi_registration_no,
            "registered_address": self.registered_address()[:512],
        }

    def head_office(self) -> dict[str, Any]:
        return {
            "name": f"{self.city.strip()} — Head office",
            "city": self.city.strip(),
            "state_code": self.state_code,
            "pincode": self.pincode,
        }


class WorkspaceIn(CompanyDetails):
    """A new company: its details, its workspace address and its first admin."""

    #: Optional; made from the company name when left out.
    workspace: str | None = Field(default=None, max_length=64)

    # ── The first admin ─────────────────────────────────────────────────────
    admin_full_name: str = Field(min_length=2, max_length=255)
    admin_email: EmailStr
    admin_phone: str
    admin_designation: str | None = Field(default=None, max_length=120)
    #: Email the admin their sign-in page and a link to set their password.
    send_welcome_email: bool = True

    _admin_blanks = field_validator("workspace", "admin_designation", mode="before")(_blank_to_none)

    @field_validator("admin_phone")
    @classmethod
    def _admin_phone(cls, value: str) -> str:
        national = normalise_mobile(value)
        if national is None or not _MOBILE.match(national[-10:]):
            raise ValueError("Give the admin's 10-digit Indian mobile number.")
        return national[-10:]


class WorkspaceUpdate(BaseModel):
    """An edit: any of the company's details. Merged with what is stored, then
    checked as a whole by `CompanyDetails` — so a new GSTIN is checked against
    the PAN already on file."""

    model_config = ConfigDict(extra="forbid")

    workspace: str | None = Field(default=None, max_length=64)
    company_name: str | None = None
    legal_name: str | None = None
    company_type: CompanyType | None = None
    industry: str | None = None
    company_size: CompanySize | None = None
    website: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    pan: str | None = None
    tan: str | None = None
    gstin: str | None = None
    cin: str | None = None
    pf_registration_no: str | None = None
    esi_registration_no: str | None = None
    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    state_code: str | None = None
    pincode: str | None = None
    modules: dict[str, bool] | None = None


class AdminIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str = Field(min_length=2, max_length=255)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=32)
    send_welcome_email: bool = True


#: Who a platform-made login's welcome email says set it up.
_PLATFORM_INVITER = "the AskMira team"


async def _welcome(
    session: Any, admin: User, wanted: bool, request: Request | None
) -> dict[str, Any] | None:
    if not wanted:
        return None
    sent = await send_welcome(
        session, target=admin, inviter_name=_PLATFORM_INVITER, request=request
    )
    return {
        "status": sent.status,
        "detail": sent.detail,
        "sent_to": sent.sent_to,
        "link_expires_at": sent.link_expires_at.isoformat() if sent.link_expires_at else None,
    }


def _require_super(user: User) -> None:
    if user.role is not UserRole.SUPER_ADMIN or getattr(user, "operator_grant_id", None):
        raise permissions.forbidden("Only the platform's super admin manages workspaces.")


def _temporary_password() -> str:
    # Meets the password policy: letters, a digit, length.
    return secrets.token_urlsafe(12) + "-Aa1"


async def _workspace(session: Any, tenant_id: uuid.UUID) -> Tenant:
    tenant = await session.get(Tenant, tenant_id)
    # A deleted workspace is gone as far as this console is concerned.
    if tenant is None or tenant.deleted_at is not None:
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
    tenants = (
        await session.scalars(
            select(Tenant).where(Tenant.deleted_at.is_(None)).order_by(Tenant.created_at)
        )
    ).all()
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
        profile=payload.profile(),
        legal_entity=payload.legal_entity(),
        head_office=payload.head_office(),
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
    welcome = await _welcome(session, admin, payload.send_welcome_email, request)
    await session.commit()
    return {
        "workspace": {"id": str(tenant.id), "name": tenant.name, "workspace": tenant.slug},
        "admin": {"email": admin.email, "temporary_password": temporary},
        "welcome_email": welcome,
        "note": (
            "It is shown once, and they choose their own password at first sign-in. "
            + (
                "They were also emailed a link to set it."
                if welcome and welcome["status"] == "sent"
                else "Give it to the admin yourself."
            )
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


async def _first_entity_and_office(
    session: Any, tenant_id: uuid.UUID
) -> tuple[LegalEntity | None, Location | None]:
    entity = await session.scalar(
        select(LegalEntity)
        .where(tenant_filter(LegalEntity, tenant_id))
        .order_by(LegalEntity.created_at)
        .limit(1)
    )
    office = await session.scalar(
        select(Location)
        .where(tenant_filter(Location, tenant_id))
        .order_by(Location.created_at)
        .limit(1)
    )
    return entity, office


def _details_of(tenant: Tenant, entity: LegalEntity | None, office: Location | None) -> dict:
    """What is on file for a company, in the shape the form edits."""
    profile = (tenant.settings or {}).get("profile") or {}
    return {
        "company_name": tenant.name,
        "legal_name": (entity.name if entity else None) or profile.get("legal_name") or tenant.name,
        "company_type": profile.get("company_type") or "private_limited",
        "industry": profile.get("industry"),
        "company_size": profile.get("company_size"),
        "website": profile.get("website"),
        "contact_email": profile.get("contact_email"),
        "contact_phone": profile.get("contact_phone"),
        "pan": entity.pan if entity else None,
        "tan": entity.tan if entity else None,
        "gstin": entity.gstin if entity else None,
        "cin": profile.get("cin"),
        "pf_registration_no": entity.pf_registration_no if entity else None,
        "esi_registration_no": entity.esi_registration_no if entity else None,
        "registered_address": entity.registered_address if entity else None,
        "address_line1": profile.get("address_line1"),
        "address_line2": profile.get("address_line2"),
        "city": office.city if office else None,
        "state_code": office.state_code if office else None,
        "pincode": office.pincode if office else None,
        "modules": dict(tenant.enabled_modules or {}),
    }


async def workspace_detail(session: Any, user: User, tenant_id: uuid.UUID) -> dict[str, Any]:
    """One company's full profile, its admins and its size. Nothing from inside it."""
    _require_super(user)
    tenant = await _workspace(session, tenant_id)
    entity, office = await _first_entity_and_office(session, tenant.id)
    admins = (
        await session.scalars(
            select(User)
            .where(tenant_filter(User, tenant.id), User.role == UserRole.ADMIN)
            .order_by(User.created_at)
        )
    ).all()
    logins = await session.scalar(
        select(func.count(User.id)).where(tenant_filter(User, tenant.id), User.is_active.is_(True))
    )
    employees = await session.scalar(
        select(func.count(Employee.id)).where(
            tenant_filter(Employee, tenant.id), Employee.deleted_at.is_(None)
        )
    )
    last_sign_in = await session.scalar(
        select(func.max(User.last_login_at)).where(tenant_filter(User, tenant.id))
    )
    return {
        "id": str(tenant.id),
        "name": tenant.name,
        "workspace": tenant.slug,
        "is_active": tenant.is_active,
        "is_own": tenant.id == user.tenant_id,
        "created_at": tenant.created_at.isoformat() if tenant.created_at else None,
        "logo_url": logo_url(tenant),
        "sign_in_path": f"/signin/{tenant.slug}",
        "details": _details_of(tenant, entity, office),
        "admins": [_admin_read(a) for a in admins],
        "active_logins": int(logins or 0),
        "employees": int(employees or 0),
        "last_sign_in": last_sign_in.isoformat() if last_sign_in else None,
    }


def _first_problem(exc: ValidationError) -> str:
    error = exc.errors()[0]
    field = ".".join(str(part) for part in error.get("loc", ()) if part != "__root__")
    message = str(error.get("msg", "is not valid")).removeprefix("Value error, ")
    if error.get("type") == "missing":
        return f"{field.replace('_', ' ')} is required."
    return f"{field.replace('_', ' ')}: {message}" if field else message


async def update(
    session: Any,
    user: User,
    tenant_id: uuid.UUID,
    payload: WorkspaceUpdate,
    *,
    request: Request | None,
) -> dict[str, Any]:
    """Change a company's details. The whole record is checked again, as on creation."""
    _require_super(user)
    tenant = await _workspace(session, tenant_id)
    entity, office = await _first_entity_and_office(session, tenant.id)
    current = _details_of(tenant, entity, office)
    current.pop("registered_address")

    changes = payload.model_dump(exclude_unset=True)
    new_slug = changes.pop("workspace", None)
    if changes.get("modules") is not None:
        changes["modules"] = {**current["modules"], **changes["modules"]}
    merged = {**current, **changes}
    merged["modules"] = {
        key: on
        for key, on in (merged.get("modules") or {}).items()
        if key in DEFAULT_ENABLED_MODULES
    }
    try:
        details = CompanyDetails.model_validate(merged)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=_first_problem(exc)
        ) from exc

    before = {key: current.get(key) for key in changes}

    if new_slug is not None and new_slug.strip().lower() != tenant.slug:
        slug = new_slug.strip().lower()
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
        before["workspace"] = tenant.slug
        changes["workspace"] = slug
        tenant.slug = slug

    tenant.name = details.company_name.strip()
    tenant.enabled_modules = {**(tenant.enabled_modules or {}), **details.modules}
    settings = dict(tenant.settings or {})
    settings["profile"] = {**(settings.get("profile") or {}), **details.profile()}
    tenant.settings = settings

    if entity is None:
        entity = LegalEntity(tenant_id=tenant.id, code="LE01", **details.legal_entity())
        session.add(entity)
        await session.flush()
    else:
        for key, value in details.legal_entity().items():
            setattr(entity, key, value)
    if office is None:
        office = Location(
            tenant_id=tenant.id, legal_entity_id=entity.id, code="HO", **details.head_office()
        )
        session.add(office)
    else:
        for key, value in details.head_office().items():
            if key == "name" and office.name and "Head office" not in office.name:
                continue  # A location the company renamed keeps its name.
            setattr(office, key, value)

    await audit.record(
        session,
        tenant_id=tenant.id,
        action=AuditAction.UPDATE,
        entity_type="tenant",
        entity_id=tenant.id,
        actor_user_id=user.id,
        actor_type="platform",
        changes={"before": before, "after": changes},
        summary=f"Company details changed by the platform: {', '.join(sorted(changes)) or 'none'}",
        request=request,
    )
    await session.commit()
    return await workspace_detail(session, user, tenant.id)


async def remove_logo(
    session: Any, user: User, tenant_id: uuid.UUID, *, request: Request | None
) -> None:
    _require_super(user)
    tenant = await _workspace(session, tenant_id)
    if not tenant.logo_key:
        return
    tenant.logo_key = None
    await audit.record(
        session,
        tenant_id=tenant.id,
        action=AuditAction.UPDATE,
        entity_type="tenant",
        entity_id=tenant.id,
        actor_user_id=user.id,
        actor_type="platform",
        summary="Company logo removed by the platform",
        request=request,
    )
    await session.commit()


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
    welcome = await _welcome(session, admin, payload.send_welcome_email, request)
    await session.commit()
    return {
        "admin": {"email": admin.email, "temporary_password": temporary},
        "welcome_email": welcome,
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


#: Where a deleted workspace's logins are renamed to, freeing their emails.
#: `.invalid` is reserved (RFC 2606): nothing is ever delivered there.
_RELEASED_EMAIL_DOMAIN = "deleted.askmira.invalid"

#: What reaches a workspace without a person signing in, revoked on deletion.
_REVOCABLE: tuple[tuple[str, Any], ...] = (
    ("api_keys", ApiKey),
    ("scim_tokens", ScimToken),
    ("mcp_connections", OAuthGrant),
    ("operator_grants", OperatorGrant),
    ("sessions", UserSession),
    ("candidate_links", CandidatePortalToken),
)


async def delete(
    session: Any,
    user: User,
    tenant_id: uuid.UUID,
    *,
    confirm: str,
    reason: str,
    request: Request | None,
) -> dict[str, Any]:
    """Delete a workspace: closed for good, its emails and its address freed.

    Nothing in it is erased. Its audit trail is insert-only and its payroll and
    statutory records have to be kept, so the rows stay, out of everyone's
    reach: every login is switched off and its sessions ended; every key,
    token, grant and device that reaches it without a login is revoked; its
    careers page comes down; and it leaves this console. Each login's email
    and the workspace's address are renamed, so a person from it can be given
    a login elsewhere and the address can be used again.

    Purging the rows at the end of the retention period is a DBA's job,
    outside the app, because the audit log's trigger refuses it from here.
    """
    _require_super(user)
    tenant = await _workspace(session, tenant_id)
    if tenant.id == user.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The platform's own workspace cannot be deleted.",
        )
    if confirm.strip().lower() != tenant.slug:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Type the workspace address, {tenant.slug}, to confirm.",
        )
    if not reason.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Give a reason."
        )

    now = datetime.now(UTC)
    name, slug = tenant.name, tenant.slug
    logins = await session.execute(
        sql_update(User)
        .where(tenant_filter(User, tenant.id))
        .values(
            is_active=False,
            tokens_valid_from=now,
            email=func.concat("deleted-", cast(User.id, String), f"@{_RELEASED_EMAIL_DOMAIN}"),
        )
    )
    revoked: dict[str, int] = {}
    for label, model in _REVOCABLE:
        values: dict[str, Any] = {"revoked_at": now}
        if model is ScimToken:
            values["active"] = False
        if model is UserSession:
            values["revoked_reason"] = "Workspace deleted"
        result = await session.execute(
            sql_update(model)
            .where(tenant_filter(model, tenant.id), model.revoked_at.is_(None))
            .values(**values)
        )
        revoked[label] = result.rowcount
    devices = await session.execute(
        sql_update(DeviceIntegration)
        .where(
            tenant_filter(DeviceIntegration, tenant.id),
            DeviceIntegration.status != DeviceStatus.DISABLED,
        )
        .values(status=DeviceStatus.DISABLED)
    )
    revoked["devices"] = devices.rowcount
    await session.execute(
        sql_update(CareersPageConfig)
        .where(tenant_filter(CareersPageConfig, tenant.id))
        .values(is_published=False)
    )

    tenant.is_active = False
    tenant.deleted_at = now
    tenant.slug = f"deleted-{tenant.id.hex}"

    changes = {"workspace": slug, "logins_closed": logins.rowcount, "revoked": revoked}
    await audit.record(
        session,
        tenant_id=tenant.id,
        action=AuditAction.DELETE,
        entity_type="tenant",
        entity_id=tenant.id,
        actor_user_id=user.id,
        actor_type="platform",
        changes=changes,
        summary=f"Workspace deleted by the platform: {reason.strip()}",
        request=request,
    )
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.DELETE,
        entity_type="platform_workspace",
        entity_id=tenant.id,
        actor=user,
        changes=changes,
        summary=f"Deleted workspace {name} ({slug}): {reason.strip()}",
        request=request,
    )
    await session.commit()
    return {
        "id": str(tenant.id),
        "name": name,
        "deleted_at": now.isoformat(),
        "logins_closed": logins.rowcount,
        "revoked": revoked,
    }
