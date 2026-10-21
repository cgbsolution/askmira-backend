"""Workspace administration: company settings, logins and roles, modules."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.user import UserRole

#: The modules a workspace can switch on. Keys match `require_module` gates.
MODULE_CATALOGUE: dict[str, str] = {
    "core_hr": "People: directory, org chart, lifecycle and documents",
    "leave": "Time: time off, attendance and shifts",
    "payroll": "Payroll, statutory filings, loans and my pay",
    "hiring": "Hiring: requisitions, candidates, offers and job boards",
    "culture": "Culture: feed, clubs, awards and surveys",
    "rewards": "Rewards and recognition points",
    "analytics": "BI connector and benchmarking",
}

#: Keys of `Tenant.settings` an admin may edit here. Anything else in the bag
#: (the break-glass account, say) has its own, audited, screen.
EDITABLE_TENANT_SETTINGS = ("currency", "timezone", "fiscal_year_start_month", "week_start")


class TenantSettingsUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=255)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    fiscal_year_start_month: int | None = Field(default=None, ge=1, le=12)
    week_start: str | None = Field(default=None, pattern=r"^(monday|sunday|saturday)$")


class ManagedUserRead(BaseModel):
    """A login as an admin sees it. No password material, ever."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: str
    role: UserRole
    is_active: bool
    employee_id: uuid.UUID | None
    employee_name: str | None = None
    employee_code: str | None = None
    last_login_at: datetime | None
    created_at: datetime
    #: Still on what they were given — they have not chosen their own way in
    #: yet — so a welcome email can be resent.
    invite_pending: bool = False
    is_you: bool = False
    is_break_glass: bool = False


class ManagedUserCreate(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=2, max_length=255)
    role: UserRole = UserRole.EMPLOYEE
    employee_id: uuid.UUID | None = None
    #: Email them where to sign in, a link to set their password, and their
    #: onboarding checklist. Off when the address cannot receive mail yet.
    send_welcome_email: bool = True


class WelcomeEmailRead(BaseModel):
    """What became of a login's welcome email."""

    status: Literal["sent", "queued", "failed", "no_relay"]
    detail: str
    sent_to: str
    link_expires_at: datetime | None


class ManagedUserCreated(BaseModel):
    user: ManagedUserRead
    #: Shown once. Only its hash is stored, and the person is asked to change it.
    temporary_password: str
    #: Null when the admin chose not to send one.
    welcome_email: WelcomeEmailRead | None = None


class EmployeeLink(BaseModel):
    #: Null unlinks the login from any employee record.
    employee_id: uuid.UUID | None
    reason: str = Field(min_length=3, max_length=500)


class PasswordResetByAdmin(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class RoleChange(BaseModel):
    role: UserRole
    reason: str = Field(min_length=3, max_length=500)


class ActiveChange(BaseModel):
    active: bool
    reason: str = Field(min_length=3, max_length=500)


class ModuleToggle(BaseModel):
    modules: dict[str, bool] = Field(min_length=1)
