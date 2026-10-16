"""Which workspace somebody signs in to, found from what they type.

The sign-in page asks for one thing first — a work email or a mobile number —
then opens that company's own sign-in page, with its name, its logo and its
single sign-on. Finding the workspace is the whole job here, and it runs before
anyone is authenticated, so these lookups are not tenant-scoped: the login
establishes the tenant. Every other query in the product is scoped as usual.

What it gives away, and why that is acceptable: that an address or number has
an AskMira login, and which company it belongs to — the same thing the company
name on a payslip or an offer letter says. The lookup is rate-limited per
connection so it cannot be used to sweep a list, and it never says anything
about the person beyond that.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.employee import Employee
from app.models.identity import IdentityProvider
from app.models.tenant import Tenant
from app.models.user import User

__all__ = [
    "AmbiguousLoginError",
    "branding",
    "find_login",
    "looks_like_email",
    "normalise_mobile",
    "social_providers",
    "workspace_by_slug",
]

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AmbiguousLoginError(Exception):
    """A mobile number shared by more than one login."""


def looks_like_email(value: str) -> bool:
    return bool(_EMAIL.match(value.strip()))


def normalise_mobile(raw: str | None) -> str | None:
    """The national number, digits only: '+91 98765-43210' -> '9876543210'.

    Indian numbers are stored every way people type them; comparing the last
    ten digits is what makes '+91…', '0…' and the bare number the same phone.
    """
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits if 8 <= len(digits) <= 15 else None


def _last_ten(column: Any) -> Any:
    return func.right(func.regexp_replace(column, r"\D", "", "g"), 10)


async def find_login(
    session: AsyncSession, login: str, *, workspace: str | None = None
) -> User | None:
    """The login an email address or a mobile number belongs to.

    Email is unique across the platform. A mobile number is looked up on the
    login and on its employee record; when it matches more than one login it
    is refused rather than guessed, and the person signs in with their email.
    """
    value = login.strip()
    if not value:
        return None
    if looks_like_email(value):
        stmt = select(User).where(func.lower(User.email) == value.lower())
        if workspace:
            stmt = stmt.join(Tenant, Tenant.id == User.tenant_id).where(
                Tenant.slug == workspace.lower()
            )
        return await session.scalar(stmt.limit(1))

    mobile = normalise_mobile(value)
    if mobile is None:
        return None
    key = mobile[-10:]
    stmt = (
        select(User)
        .outerjoin(Employee, Employee.id == User.employee_id)
        .where(
            User.is_active.is_(True),
            or_(_last_ten(User.phone) == key, _last_ten(Employee.phone) == key),
        )
    )
    if workspace:
        stmt = stmt.join(Tenant, Tenant.id == User.tenant_id).where(
            Tenant.slug == workspace.lower()
        )
    matches = list((await session.scalars(stmt.limit(3))).unique().all())
    if len(matches) > 1:
        raise AmbiguousLoginError(value)
    return matches[0] if matches else None


async def workspace_by_slug(session: AsyncSession, slug: str) -> Tenant | None:
    return await session.scalar(select(Tenant).where(Tenant.slug == slug.strip().lower()))


def social_providers() -> list[str]:
    """Which "Continue with …" buttons the sign-in page may show."""
    from app.services.identity import social

    return social.available()


def logo_url(tenant: Tenant) -> str | None:
    if not tenant.logo_key:
        return None
    # The key's hash busts a cached logo when a new one is uploaded.
    version = hashlib.sha256(tenant.logo_key.encode()).hexdigest()[:10]
    return f"{settings.API_V1_PREFIX}/public/workspaces/{tenant.slug}/logo?v={version}"


@dataclass(frozen=True, slots=True)
class Branding:
    name: str
    workspace: str
    logo_url: str | None
    sso: dict[str, Any] | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "workspace": self.workspace,
            "logo_url": self.logo_url,
            "sso": self.sso,
            "social": social_providers(),
        }


async def branding(session: AsyncSession, tenant: Tenant) -> Branding:
    """What a company's sign-in page shows: its name, logo and single sign-on."""
    provider = await session.scalar(
        select(IdentityProvider)
        .where(
            IdentityProvider.tenant_id == tenant.id,
            IdentityProvider.enabled.is_(True),
        )
        .order_by(IdentityProvider.created_at)
        .limit(1)
    )
    sso = None
    if provider is not None:
        sso = {
            "provider_id": str(provider.id),
            "name": provider.name,
            "type": provider.idp_type.value,
            "required": provider.enforce_sso,
            "start_url": f"{settings.API_V1_PREFIX}/sso/start/{provider.id}",
        }
    return Branding(
        name=tenant.name, workspace=tenant.slug, logo_url=logo_url(tenant), sso=sso
    )


def suspended() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="This workspace has been suspended. Contact AskMira support.",
    )
