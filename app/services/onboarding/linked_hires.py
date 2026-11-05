"""Candidates hired in the linked interview tool (AnytimeHire), waiting to be added as joiners.

With AnytimeHire linked, recruiting happens there and a candidate is marked
Hired there. HR then adds them here — the one place people are added — from a
list of those hires, with what AnytimeHire knows already filled in: name,
email, phone, the job's title, its department and location (matched to this
workspace's own by name). Someone already in the directory, by email, is not
listed again.

Only HR is answered (AnytimeHire gives contact details to AskMira's admins
only); the list is asked for live, as the person, and kept nowhere.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.employee import Employee
from app.models.org import Department, Location
from app.models.tenant import Tenant
from app.models.user import User
from app.services.products import data as products
from app.services.products.catalogue import PRODUCTS

__all__ = ["list_hires"]

PRODUCT = "interview"
BRAND = PRODUCTS[PRODUCT].brand


def _match(names: dict[str, str], wanted: Any) -> str | None:
    key = str(wanted or "").strip().lower()
    return names.get(key) if key else None


async def list_hires(session: AsyncSession, user: User) -> dict[str, Any]:
    if not permissions.is_hr(user):
        raise permissions.forbidden("Hired candidates are added as joiners by HR.")
    tenant = await session.get(Tenant, user.tenant_id)
    if not (tenant and (tenant.linked_products or {}).get(PRODUCT)):
        return {"linked": False, "brand": BRAND, "hires": []}
    try:
        answer = await products.ask(session, user, PRODUCT, "hires", {"limit": 200})
    except products.ProductUnavailableError as exc:
        return {"linked": True, "brand": BRAND, "hires": [], "error": str(exc)}

    known = {
        str(email).lower()
        for email in (
            await session.scalars(
                tenant_scoped(select(func.lower(Employee.work_email)), Employee, user)
            )
        ).all()
        if email
    } | {
        str(email).lower()
        for email in (
            await session.scalars(tenant_scoped(select(func.lower(User.email)), User, user))
        ).all()
        if email
    }
    departments = {
        name.strip().lower(): str(id_)
        for id_, name in (
            await session.execute(
                tenant_scoped(select(Department.id, Department.name), Department, user)
            )
        ).all()
        if name
    }
    locations = {
        name.strip().lower(): str(id_)
        for id_, name in (
            await session.execute(tenant_scoped(select(Location.id, Location.name), Location, user))
        ).all()
        if name
    }

    hires = []
    for hire in answer.get("hires") or []:
        if not isinstance(hire, dict):
            continue
        email = str(hire.get("email") or "").strip().lower()
        if not email or email in known:
            continue
        name = str(hire.get("name") or "").strip()
        first, _, last = name.partition(" ")
        hires.append(
            {
                "ref": str(hire.get("ref") or ""),
                "name": name,
                "first_name": first,
                "last_name": last.strip(),
                "email": email,
                "phone": str(hire.get("phone") or "").strip() or None,
                "job": hire.get("job"),
                "department": hire.get("department"),
                "department_id": _match(departments, hire.get("department")),
                "location": hire.get("location"),
                "location_id": _match(locations, hire.get("location")),
                "applied_at": hire.get("applied_at"),
            }
        )
    return {"linked": True, "brand": BRAND, "hires": hires}
