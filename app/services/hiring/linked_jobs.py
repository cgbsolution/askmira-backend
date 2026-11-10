"""An approved requisition opens its job in the linked interview tool (AnytimeHire).

With AnytimeHire linked, recruiting happens there; AskMiraa keeps what comes
before it — the requisition, its budget check and its approval chain. When the
last approver signs, the job is opened in AnytimeHire as that approver
(products.act, use=act, kind ``job.open``), with the requisition's id as its
key, so asking twice gives back the job opened the first time and never a
second one.

The approval itself is committed first and never undone by the tool: a role
approved in AskMiraa stays approved whether or not AnytimeHire answered. What
happened is kept on the requisition — the job's reference there, or the tool's
own words — and HR or the hiring manager can try again from the requisition.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.hiring import Requisition, RequisitionStatus
from app.models.org import Department, Location
from app.models.tenant import Tenant
from app.models.user import User
from app.services import audit
from app.services.products import data as products
from app.services.products.catalogue import PRODUCTS

__all__ = ["is_linked", "job_params", "open_job", "open_job_after_approval"]

PRODUCT = "interview"
BRAND = PRODUCTS[PRODUCT].brand

_JOB_TYPE = {
    "full_time": "Full Time",
    "part_time": "Part Time",
    "contract": "Contract",
    "intern": "Internship",
    "internship": "Internship",
}


def _key(requisition: Requisition) -> str:
    return f"askmira-req:{requisition.id}"


async def is_linked(session: AsyncSession, tenant_id: uuid.UUID) -> bool:
    tenant = await session.get(Tenant, tenant_id)
    return bool(tenant and (tenant.linked_products or {}).get(PRODUCT))


def job_params(
    requisition: Requisition, *, department: str | None, location: str | None
) -> dict[str, Any]:
    """The job as AnytimeHire's `job.open` takes it, from the requisition."""
    low = max(0, min(int(requisition.experience_min_years or 0), 60))
    high = max(low, min(int(requisition.experience_max_years or max(low, 2)), 60))
    lines = [
        line.strip(" -•*\t")
        for line in (requisition.description or "").splitlines()
        if len(line.strip(" -•*\t")) >= 3
    ]
    note = " ".join(
        part
        for part in (
            f"AskMiraa requisition {requisition.code}.",
            (requisition.justification or "").strip()[:1500],
        )
        if part
    )
    return {
        "key": _key(requisition),
        "title": requisition.title[:255],
        "department": (department or "General")[:255],
        "location": (location or "India")[:255],
        "job_type": _JOB_TYPE.get(requisition.employment_type, "Full Time"),
        "emp_type": "Contract" if requisition.employment_type == "contract" else "Permanent",
        "experience_min": low,
        "experience_max": high,
        "openings": max(1, min(int(requisition.openings or 1), 500)),
        "skills": [{"name": str(s)[:120]} for s in (requisition.skills or [])[:30] if str(s)],
        "responsibilities": [line[:500] for line in lines[:30]],
        "deadline": (
            requisition.target_close_date.isoformat() if requisition.target_close_date else None
        ),
        "note": note[:2000],
    }


async def _names(session: AsyncSession, user: User, requisition: Requisition) -> tuple:
    department = (
        await session.scalar(
            tenant_scoped(
                select(Department.name).where(Department.id == requisition.department_id),
                Department,
                user,
            )
        )
        if requisition.department_id
        else None
    )
    location = (
        await session.scalar(
            tenant_scoped(
                select(Location.name).where(Location.id == requisition.location_id),
                Location,
                user,
            )
        )
        if requisition.location_id
        else None
    )
    return department, location


async def _open(
    session: AsyncSession, user: User, requisition: Requisition, *, request: Request | None
) -> dict[str, Any]:
    department, location = await _names(session, user, requisition)
    params = job_params(requisition, department=department, location=location)
    now = datetime.now(UTC)
    try:
        answer = await products.act(session, user, PRODUCT, "job.open", params)
    except products.ProductUnavailableError as exc:
        if exc.status == 403 and requisition.linked_job_error:
            # This person may not open jobs there: their answer, never in place of the
            # reason the job did not open for whoever approved it.
            return {"opened": False, "error": str(exc)[:2000]}
        requisition.linked_job_error = str(exc)[:2000]
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            actor=user,
            action=AuditAction.UPDATE,
            entity_type="requisition",
            entity_id=requisition.id,
            summary=f"Requisition {requisition.code} could not be opened in {BRAND}",
            changes={"linked_job_error": requisition.linked_job_error},
            request=request,
        )
        await session.commit()
        return {"opened": False, "error": requisition.linked_job_error}

    job = answer.get("job") if isinstance(answer.get("job"), dict) else {}
    requisition.linked_job_ref = str(job.get("ref") or "")[:64] or None
    requisition.linked_job_at = now
    requisition.linked_job_error = None
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="requisition",
        entity_id=requisition.id,
        summary=f"Requisition {requisition.code} opened in {BRAND}",
        changes={
            "linked_job_ref": requisition.linked_job_ref,
            "existing": bool(answer.get("existing")),
        },
        request=request,
    )
    await session.commit()
    return {
        "opened": True,
        "ref": requisition.linked_job_ref,
        "existing": bool(answer.get("existing")),
    }


async def open_job_after_approval(
    session: AsyncSession,
    user: User,
    *,
    requisition_id: uuid.UUID,
    request: Request | None = None,
) -> dict[str, Any] | None:
    """Called once the final approval is committed. None when nothing was to be done."""
    if not await is_linked(session, user.tenant_id):
        return None
    requisition = await session.scalar(
        tenant_scoped(
            select(Requisition).where(Requisition.id == requisition_id), Requisition, user
        )
    )
    if requisition is None or requisition.status is not RequisitionStatus.OPEN:
        return None
    if requisition.linked_job_ref:
        return {"opened": True, "ref": requisition.linked_job_ref, "existing": True}
    return await _open(session, user, requisition, request=request)


async def open_job(
    session: AsyncSession,
    user: User,
    *,
    requisition_id: uuid.UUID,
    request: Request | None = None,
) -> dict[str, Any]:
    """Try again from the requisition: HR, or its hiring manager or recruiter."""
    requisition = await session.scalar(
        tenant_scoped(
            select(Requisition).where(Requisition.id == requisition_id), Requisition, user
        )
    )
    if requisition is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Requisition not found")
    on_team = user.employee_id is not None and user.employee_id in (
        requisition.hiring_manager_id,
        requisition.recruiter_id,
    )
    if not (permissions.is_hr(user) or on_team):
        raise permissions.forbidden(
            f"Only HR, or the requisition's hiring manager or recruiter, can open it in {BRAND}."
        )
    if not await is_linked(session, user.tenant_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{BRAND} is not linked to this workspace.",
        )
    if await products.role_in(session, user, PRODUCT) not in ("admin", "recruiter"):
        # The job is opened as the person, and only its admins and recruiters open jobs there.
        raise permissions.forbidden(
            f"Opening jobs in {BRAND} is for its admins and recruiters. Ask HR to try again."
        )
    if requisition.status is not RequisitionStatus.OPEN:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Only an approved, open requisition is opened in {BRAND}; this one is "
                f"{requisition.status.value.replace('_', ' ')}."
            ),
        )
    if requisition.linked_job_ref:
        return {"opened": True, "ref": requisition.linked_job_ref, "existing": True}
    return await _open(session, user, requisition, request=request)
