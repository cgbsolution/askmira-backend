"""SSO orchestration: tenant resolution, JIT provisioning, enforcement.

Three rules here are load-bearing:

1. **An IdP claim never grants a role.** `provision_user` assigns
   `UserRole.EMPLOYEE` on creation and never reads a role, group or permission
   attribute. On a subsequent login the user's existing role is left exactly as
   it is. An IdP asserting `role=admin` changes nothing.

2. **JIT creates a `User`, never an `Employee`.** A user with no employee
   record is a valid state — contractors, auditors, an implementation partner.
   Linking happens only on an explicit employee-id attribute match; no match
   leaves the user unlinked and surfaces it to HR rather than guessing.

3. **`enforce_sso` always leaves a way in.** A tenant with SSO enforced still
   permits its designated break-glass account to use a password, and that login
   raises a high-severity audit entry. A misconfigured IdP that locks every
   admin out of a tenant is an outage nobody can fix from inside the product.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_secret
from app.core.deps import tenant_filter
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.identity import IdentityProvider, SamlAssertionSeen, SsoLoginAttempt
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services import audit

__all__ = [
    "SsoError",
    "assert_password_login_allowed",
    "consume_assertion_id",
    "provider_for_email",
    "provision_user",
    "purge_expired_assertions",
]


class SsoError(RuntimeError):
    """SSO configuration or policy prevented this login."""


def _domain_of(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower() if "@" in email else ""


async def provider_for_email(
    session: AsyncSession, email: str
) -> tuple[IdentityProvider, Tenant] | None:
    """Route an email address to its tenant's identity provider.

    This is what lets the sign-in screen ask only for an email: the user types
    it, and we work out which IdP to bounce them to. Without it they would have
    to know and pick their own tenant, which nobody does correctly.

    Deliberately not tenant-scoped — resolving the tenant *is* the job here,
    and it runs before any authentication exists.
    """
    domain = _domain_of(email)
    if not domain:
        return None

    stmt = (
        select(IdentityProvider, Tenant)
        .join(Tenant, Tenant.id == IdentityProvider.tenant_id)
        .where(
            IdentityProvider.enabled.is_(True),
            Tenant.is_active.is_(True),
            IdentityProvider.allowed_email_domains.any(domain),
        )
    )
    row = (await session.execute(stmt)).first()
    return (row[0], row[1]) if row else None


async def assert_password_login_allowed(session: AsyncSession, user: User, request=None) -> None:
    """Refuse a password login when the tenant enforces SSO.

    Break-glass is the exception, and it is audited at high severity — a
    break-glass login is either a genuine emergency or an incident, and both
    want to be visible.

    Raises:
        SsoError: when the tenant enforces SSO and this is not the break-glass
            account.
    """
    provider = await session.scalar(
        select(IdentityProvider).where(
            tenant_filter(IdentityProvider, user.tenant_id),
            IdentityProvider.enabled.is_(True),
            IdentityProvider.enforce_sso.is_(True),
        )
    )
    if provider is None:
        return

    tenant = await session.get(Tenant, user.tenant_id)
    break_glass_email = (tenant.settings or {}).get("break_glass_email") if tenant else None

    if break_glass_email and user.email.lower() == str(break_glass_email).lower():
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.LOGIN,
            entity_type="user",
            entity_id=user.id,
            actor=user,
            summary=(
                "BREAK-GLASS password sign-in while SSO is enforced. Either a genuine "
                "identity-provider outage or an incident — review it."
            ),
            changes={"severity": "high", "reason": "break_glass", "sso_enforced": True},
            request=request,
        )
        return

    raise SsoError(
        "This workspace requires single sign-on. Sign in through your company's identity provider."
    )


async def provision_user(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    provider: IdentityProvider,
    email: str,
    first_name: str | None,
    last_name: str | None,
    external_employee_code: str | None,
    request=None,
) -> User:
    """Find or create the user behind a successful SSO login.

    Never assigns or changes a role from provider data — see rule 1 in the
    module docstring.
    """
    email = email.strip().lower()
    if not email:
        raise SsoError("The identity provider did not supply an email address")

    domain = _domain_of(email)
    if provider.allowed_email_domains and domain not in [
        d.lower() for d in provider.allowed_email_domains
    ]:
        raise SsoError(
            f"The domain '{domain}' is not permitted for this workspace's single sign-on"
        )

    user = await session.scalar(
        select(User).where(tenant_filter(User, tenant_id), func.lower(User.email) == email)
    )

    if user is not None:
        if not user.is_active:
            raise SsoError("This account has been deactivated. Contact your admin.")
        # The existing role is left exactly as it is. An IdP cannot promote or
        # demote anyone.
        user.last_login_at = datetime.now(UTC)
        user.is_email_verified = True
        if user.employee_id is None and external_employee_code:
            await _try_link_employee(session, user, external_employee_code, request=request)
        return user

    if not provider.jit_provisioning:
        raise SsoError(
            "No account exists for this address and just-in-time provisioning is off "
            "for this workspace. Ask your admin to create the account."
        )
    # The address may already sign in to another workspace; one login per person.
    elsewhere = await session.scalar(
        select(User.id).where(func.lower(User.email) == email).limit(1)
    )
    if elsewhere is not None:
        raise SsoError(
            "This email already signs in to another AskMira workspace, so an account "
            "cannot be created for it here. Contact your admin."
        )

    full_name = " ".join(part for part in (first_name, last_name) if part) or email
    user = User(
        tenant_id=tenant_id,
        email=email,
        # No password. SSO users authenticate through the provider; a
        # placeholder hash is never verifiable because `verify_password`
        # rejects a malformed hash.
        password_hash="!sso-no-password",
        full_name=full_name,
        # Least privilege on creation, always. Roles are assigned in AskMira.
        role=UserRole.EMPLOYEE,
        is_active=True,
        is_email_verified=True,
        last_login_at=datetime.now(UTC),
    )
    session.add(user)
    await session.flush()

    if external_employee_code:
        await _try_link_employee(session, user, external_employee_code, request=request)

    await audit.record(
        session,
        tenant_id=tenant_id,
        action=AuditAction.CREATE,
        entity_type="user",
        entity_id=user.id,
        actor=user,
        changes={
            "email": email,
            "provider": provider.name,
            "role_assigned": UserRole.EMPLOYEE.value,
            "note": "Role assigned by AskMira, not by the identity provider",
        },
        summary=f"Provisioned {email} on first sign-in via {provider.name}",
        request=request,
    )
    return user


async def _try_link_employee(
    session: AsyncSession, user: User, employee_code: str, request=None
) -> None:
    """Link a user to an employee by code, or leave it for HR.

    A near-miss is not a match. Guessing here attaches someone's payslips to
    the wrong login, which is worse than an unlinked account.
    """
    employee = await session.scalar(
        select(Employee).where(
            tenant_filter(Employee, user.tenant_id),
            Employee.employee_code == employee_code,
            Employee.deleted_at.is_(None),
        )
    )
    if employee is None:
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.CREATE,
            entity_type="user",
            entity_id=user.id,
            actor=user,
            changes={"unmatched_employee_code": employee_code},
            summary=(
                f"SSO user {user.email} could not be linked to an employee: no record "
                f"with code {employee_code}. Left unlinked for HR to resolve."
            ),
            request=request,
        )
        return

    user.employee_id = employee.id


# ── Replay protection ───────────────────────────────────────────────────────


async def consume_assertion_id(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    assertion_id: str,
    not_on_or_after: datetime,
) -> None:
    """Record an assertion id, refusing one already used.

    The insert is what enforces it: the unique constraint means two concurrent
    replays cannot both succeed.

    Raises:
        SsoError: if this assertion has already been consumed.
    """
    existing = await session.scalar(
        select(SamlAssertionSeen.id).where(
            tenant_filter(SamlAssertionSeen, tenant_id),
            SamlAssertionSeen.assertion_id == assertion_id,
        )
    )
    if existing is not None:
        raise SsoError(
            "This authentication assertion has already been used. A replayed assertion "
            "is refused even inside its validity window."
        )

    session.add(
        SamlAssertionSeen(
            tenant_id=tenant_id,
            assertion_id=assertion_id,
            not_on_or_after=not_on_or_after,
        )
    )
    await session.flush()


async def purge_expired_assertions(session: AsyncSession) -> int:
    """Drop assertion ids past their validity window.

    Beyond `not_on_or_after` the assertion is refused on expiry anyway, so
    keeping the id buys nothing and the table would grow without bound.
    """
    result = await session.execute(
        delete(SamlAssertionSeen).where(SamlAssertionSeen.not_on_or_after < datetime.now(UTC))
    )
    await session.commit()
    return result.rowcount or 0


async def purge_expired_login_attempts(session: AsyncSession) -> int:
    result = await session.execute(
        delete(SsoLoginAttempt).where(
            or_(
                SsoLoginAttempt.expires_at < datetime.now(UTC),
                SsoLoginAttempt.consumed_at.isnot(None),
            )
        )
    )
    await session.commit()
    return result.rowcount or 0


def provider_client_secret(provider: IdentityProvider) -> str | None:
    """Decrypt the stored client secret for use in a token exchange."""
    return decrypt_secret(provider.client_secret_encrypted, provider.tenant_id)
