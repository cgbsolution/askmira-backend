"""SSO, SCIM and device integration (slice 04).

No mid-market or enterprise IT function approves a people system without SSO,
and it comes up in the *first* security review rather than the last.

Invariants:

1. Secrets are encrypted at rest. `IdentityProvider.client_secret_encrypted`,
   `ScimToken.token_hash` and `DeviceIntegration.push_token_hash` never hold
   plaintext. The column names say so, and a test greps for violations.
2. **An IdP role claim never grants a role.** Roles are assigned in AskMira.
   There is deliberately no `role_attribute` column: an IdP that claims a user
   is an admin must not make them one, and the cleanest way to guarantee that
   is to have nowhere to put the claim.
3. SCIM de-provisioning deactivates a `User`. It never deletes an `Employee`,
   a payslip, an attendance day, or an audit entry. Exit history has to
   survive an exit.
4. `SamlAssertionSeen` exists solely for replay protection.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class IdpType(str, enum.Enum):
    SAML = "saml"
    OIDC = "oidc"


class DeviceVendor(str, enum.Enum):
    """The vendors that actually turn up in Indian device estates."""

    ESSL = "essl"
    MATRIX = "matrix"
    ZKTECO = "zkteco"
    GENERIC = "generic"
    #: A shared tablet where people punch with their employee code and PIN.
    KIOSK = "kiosk"


class DeviceStatus(str, enum.Enum):
    ACTIVE = "active"
    # Has not reported within its threshold. Silent device failure is how a
    # month of attendance goes missing.
    SILENT = "silent"
    DISABLED = "disabled"


class IdentityProvider(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A tenant's configured SSO provider.

    In this market that is Azure AD / Entra ID most often, then Okta, then
    Google Workspace.

    Note what is **absent**: there is no attribute mapping for roles or groups.
    Roles are assigned inside AskMira. An IdP asserting `role=admin` must not
    escalate anyone, and the surest way to hold that line is to give the claim
    nowhere to land.
    """

    __tablename__ = "identity_providers"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_idp_tenant_name"),)

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    idp_type: Mapped[IdpType] = mapped_column(Enum(IdpType, name="idp_type"), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # ── SAML ────────────────────────────────────────────────────────────────
    entity_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    sso_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    slo_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # The IdP's public signing certificate. Public material, so not encrypted —
    # but it is integrity-critical, and every change is audit-logged.
    x509_cert: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── OIDC ────────────────────────────────────────────────────────────────
    issuer: Mapped[str | None] = mapped_column(String(512), nullable=True)
    discovery_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    client_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Ciphertext only. See `app/core/crypto.py`.
    client_secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Attribute mapping ───────────────────────────────────────────────────
    email_attribute: Mapped[str] = mapped_column(String(128), default="email", nullable=False)
    first_name_attribute: Mapped[str] = mapped_column(
        String(128), default="given_name", nullable=False
    )
    last_name_attribute: Mapped[str] = mapped_column(
        String(128), default="family_name", nullable=False
    )
    # Used to link an SSO user to an existing employee record. No match leaves
    # the user unlinked and surfaces it to HR rather than guessing.
    employee_id_attribute: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # ── Policy ──────────────────────────────────────────────────────────────
    # When true, password login is refused for this tenant — except for the
    # break-glass account. A misconfigured IdP that locks every admin out is an
    # outage that cannot be fixed from inside the product.
    enforce_sso: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    allowed_email_domains: Mapped[list[str]] = mapped_column(
        ARRAY(String(255)), default=list, nullable=False
    )
    # Just-in-time provisioning: create a User on first login. Never creates an
    # Employee — a User with no Employee is a valid state for contractors and
    # auditors.
    jit_provisioning: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    def __repr__(self) -> str:
        return f"<IdentityProvider {self.name} ({self.idp_type.value})>"


class SamlAssertionSeen(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """Assertion ids already consumed, for replay protection.

    A SAML assertion is a bearer credential. Without this table, capturing one
    off the wire lets an attacker log in as that user repeatedly for the whole
    validity window.

    Rows are purged after their `not_on_or_after` passes — beyond that the
    assertion is rejected on expiry anyway.
    """

    __tablename__ = "saml_assertions_seen"
    __table_args__ = (
        UniqueConstraint("tenant_id", "assertion_id", name="uq_saml_assertion_once"),
    )

    assertion_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    not_on_or_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class SsoLoginAttempt(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """Short-lived state for an in-flight OIDC or SAML login.

    Holds the `state`, `nonce` and PKCE verifier. Without server-side storage
    of these, nonce and state validation is theatre — the values have to be
    compared against something the client could not have chosen.
    """

    __tablename__ = "sso_login_attempts"

    provider_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("identity_providers.id", ondelete="CASCADE"),
        nullable=False,
    )
    state: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    nonce: Mapped[str | None] = mapped_column(String(128), nullable=True)
    code_verifier: Mapped[str | None] = mapped_column(String(256), nullable=True)
    redirect_uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ScimToken(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A bearer token the corporate directory uses to reach our SCIM endpoints.

    Only the hash is stored. The plaintext is shown once, at creation, and is
    then unrecoverable — the same rule as any API key.
    """

    __tablename__ = "scim_tokens"

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # SHA-256 of the token. Never the token itself.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    # First and last few characters, so an admin can tell two tokens apart.
    token_hint: Mapped[str] = mapped_column(String(32), nullable=False)

    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class DeviceIntegration(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A biometric or access-control device feeding attendance punches."""

    __tablename__ = "device_integrations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "device_id", name="uq_device_tenant_device_id"),
    )

    device_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    vendor: Mapped[DeviceVendor] = mapped_column(
        Enum(DeviceVendor, name="device_vendor"), default=DeviceVendor.GENERIC, nullable=False
    )

    work_location_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("work_locations.id", ondelete="SET NULL"), nullable=True
    )

    # Push devices authenticate with this token. Hash only.
    push_token_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True
    )
    push_token_hint: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: Whether the device may push by serial number alone (the ADMS/iclock
    #: protocol's only identifier) for firmware that cannot carry the token in
    #: the server path. Off until HR switches it on for this device.
    accepts_serial_push: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    #: A kiosk's run of wrong code-and-PIN entries, and the pause it earned:
    #: one PIN tried against every code is stopped at the tablet, not per person.
    kiosk_failed_attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    kiosk_locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Devices commonly send local wall-clock time with no offset. This is the
    # timezone that assumption uses, and it is recorded on every punch.
    assumed_timezone: Mapped[str] = mapped_column(
        String(64), default="Asia/Kolkata", nullable=False
    )

    status: Mapped[DeviceStatus] = mapped_column(
        Enum(DeviceStatus, name="device_status"), default=DeviceStatus.ACTIVE, nullable=False
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Minutes of silence before the device is flagged.
    silence_threshold_minutes: Mapped[int] = mapped_column(
        Integer, default=180, nullable=False
    )

    @property
    def is_silent(self) -> bool:
        from datetime import UTC, timedelta
        from datetime import datetime as dt

        if self.last_seen_at is None:
            return True
        threshold = dt.now(UTC) - timedelta(minutes=self.silence_threshold_minutes)
        return self.last_seen_at < threshold


class DevicePunchException(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A punch that could not be attributed to an employee.

    Stored rather than discarded, and rather than failing the batch. One
    unmapped employee code must never block an entire site's attendance —
    which is exactly what rejecting the batch would do.
    """

    __tablename__ = "device_punch_exceptions"

    device_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    employee_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    punched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    raw_payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolved_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
