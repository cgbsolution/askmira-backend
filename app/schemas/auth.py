"""Auth request/response contracts.

No schema here exposes `password_hash`. That is deliberate and should stay
true — the read model for a user is `UserRead`, and it has no hash field to
leak.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

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


class LoginRequest(BaseModel):
    """Sign in with a work email or a mobile number, and a password.

    `email` is the older field the mobile app still sends; `login` takes either.
    `workspace` is the company the sign-in page was opened for, which narrows a
    mobile number to that company's people.
    """

    login: str | None = Field(default=None, max_length=320)
    email: str | None = Field(default=None, max_length=320)
    workspace: str | None = Field(default=None, max_length=63)
    password: str

    @model_validator(mode="after")
    def _has_identifier(self) -> LoginRequest:
        if not (self.login or self.email or "").strip():
            raise ValueError("Enter your work email or mobile number.")
        return self

    @property
    def identifier(self) -> str:
        return (self.login or self.email or "").strip()


class MfaChallengeRead(BaseModel):
    """The password was right; a code from the authenticator app is needed next."""

    mfa_required: bool = True
    mfa_token: str
    expires_in: int
    message: str = "Enter the code from your authenticator app, or a recovery code."


class MfaLoginRequest(BaseModel):
    mfa_token: str
    code: str


class MfaCodeRequest(BaseModel):
    code: str


class MfaDisableRequest(BaseModel):
    password: str
    code: str


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
    #: The workspace requires two-step sign-in of this person and it is not set
    #: up: the portal sends them to set it up before anything else.
    two_step_required: bool = False
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
    #: Separate products that replace a module for this workspace, e.g. {"interview": true}.
    linked_products: dict = {}


class CompanyBrand(BaseModel):
    """The company the signed-in person works for, as the portal and app draw it.

    Their legal entity's name and logo (a sister company, a client the group
    staffs); the workspace's logo when that company has none; the workspace's
    name for somebody with no company.
    """

    name: str
    #: A path on the API, or None: then the portal and the app show the name.
    logo_url: str | None = None
    legal_entity_id: uuid.UUID | None = None


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds until the access token expires


class AuthResponse(TokenPair):
    """What the frontend gets on login and refresh.

    User and tenant ship with the tokens so the client can render the shell
    without a second round-trip.
    """

    user: UserRead
    tenant: TenantRead
    company: CompanyBrand | None = None


class SessionRead(BaseModel):
    """The signed-in user and their workspace, for the web app's session bootstrap."""

    user: UserRead
    tenant: TenantRead
    #: Their own company's name and logo, drawn at the top of the portal and app.
    company: CompanyBrand | None = None
