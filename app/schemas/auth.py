"""Auth request/response contracts.

No schema here exposes `password_hash`. That is deliberate and should stay
true — the read model for a user is `UserRead`, and it has no hash field to
leak.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.user import UserRole

PASSWORD_MIN_LENGTH = 10


def _validate_password(value: str) -> str:
    """Length first, then variety.

    Length carries most of the strength, so the floor is 10 rather than the
    conventional 8; the character-class checks are there because enterprise
    procurement questionnaires ask for them.
    """
    if len(value) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"Password must be at least {PASSWORD_MIN_LENGTH} characters")
    if not re.search(r"[A-Za-z]", value):
        raise ValueError("Password must contain a letter")
    if not re.search(r"\d", value):
        raise ValueError("Password must contain a digit")
    return value


class SignupRequest(BaseModel):
    """Creates a tenant and its first admin in one call.

    Self-serve tenant creation is a pilot-phase convenience. Before GA this
    should move behind an invite so a stranger cannot mint tenants.
    """

    company_name: str = Field(min_length=2, max_length=255)
    full_name: str = Field(min_length=2, max_length=255)
    email: EmailStr
    password: str
    phone: str | None = Field(default=None, max_length=32)

    _check_password = field_validator("password")(_validate_password)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str

    _check_password = field_validator("new_password")(_validate_password)


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str

    _check_password = field_validator("new_password")(_validate_password)


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    #: Plain on the way out: validated at sign-up, and a stored address the
    #: validator later rejects must not make the session endpoint fail.
    email: str
    full_name: str
    phone: str | None
    role: UserRole
    is_active: bool
    is_email_verified: bool
    #: The portal asks for a new password before anything else when set.
    must_change_password: bool = False
    #: Somebody reports to them, so the portal shows their team's pages and
    #: approvals. Not a role: it follows the org chart (`User.has_reports`).
    is_manager: bool = Field(default=False, validation_alias="has_reports")
    employee_id: uuid.UUID | None
    last_login_at: datetime | None
    created_at: datetime


class TenantRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    plan: str
    data_region: str
    enabled_modules: dict


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds until the access token expires


class AuthResponse(TokenPair):
    """What the frontend gets on signup, login and refresh.

    User and tenant ship with the tokens so the client can render the shell
    without a second round-trip.
    """

    user: UserRead
    tenant: TenantRead


class SessionRead(BaseModel):
    """The signed-in user and their workspace, for the web app's session bootstrap."""

    user: UserRead
    tenant: TenantRead
