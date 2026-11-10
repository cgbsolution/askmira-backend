"""An order win's roles as AskMiraa requisitions: budget confirmed, approval recorded.

The company loop runs order won → budget confirmed → requisition approved →
candidate recruited. When the person names the delivery department, the order
win's offer says whether its people fit that department's headcount budget this
financial year (for HR: each role at its own cost, with the seats requisitions
already ask for counted), and each role is raised here as a requisition for that
department — the role, its seats, a CTC ceiling of a year of the role's loaded
cost, the order as its reason and its place of work — sent along the
workspace's requisition approval path (or kept as a draft when there is none
yet). Its position opens in AnytimeHire only once it is approved
(hiring/linked_jobs.py), so a role the approvers turn down is never recruited
for. A step of the order that fails later cancels these requisitions with the
rest.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.models.user import User

__all__ = ["cancel", "plan_for", "raise_for_role"]

SUBMIT = "submit"
DRAFT = "draft"


def _yearly(cost_rate: Decimal) -> Decimal:
    return (cost_rate * 12).quantize(Decimal("0.01"))


async def plan_for(
    session: AsyncSession, user: User, wanted: str, roles: list[Any]
) -> tuple[dict[str, Any] | None, list[str], dict[str, Any] | None]:
    """(the department and how its requisitions go, notes for the offer, or a question card)."""
    from app.services.companion import requisition_tools as tools

    wanted = " ".join(wanted.split())
    if not wanted:
        return (
            None,
            [
                "No delivery department was named, so these people are not counted against a "
                "headcount budget or raised as requisitions in AskMiraa."
            ],
            None,
        )
    departments = await tools._departments(session, user)
    found = tools._match(departments, wanted)
    if len(found) != 1:
        names = ", ".join(d.name for d in (found or departments)[:12]) or "none set up yet"
        lead = "Which delivery department" if found else f"There is no department “{wanted}”"
        return None, [], {"type": "notice", "tone": "info", "text": f"{lead}: {names}?"}
    department = found[0]
    hires = sum(r.count for r in roles)
    ceiling = max(_yearly(r.cost_rate) for r in roles)
    notes: list[str] = []
    from app.services.positions import service as positions

    if permissions.is_hr(user):
        answer = await positions.budget_room(
            session,
            user,
            on=date.today(),
            department=department.name,
            # Each role at its own yearly cost, not every seat at the dearest one's.
            seats=[(r.count, _yearly(r.cost_rate)) for r in roles],
            requisitions=True,
        )
        mine = [b for b in answer["budgets"] if b["department_id"] == str(department.id)]
        year = answer["financial_year"]
        only_you = " Only you see this check; the approvers see what follows."
        if not mine:
            notes.append(f"{department.name} has no headcount budget for {year}.")
        elif mine[0]["fits"] is True:
            notes.append(
                f"Inside {department.name}'s {year} headcount budget{tools.counting(mine[0])}: "
                f"these {hires} people fit, each role at its own cost." + only_you
            )
        elif mine[0]["fits"] is False:
            notes.append(
                f"Over {department.name}'s {year} headcount budget{tools.counting(mine[0])}: "
                f"{' '.join(mine[0]['problems'])}" + only_you
            )
    approvers, problem = await tools._approval_path(session, user, ceiling, None)
    if approvers is None:
        how = DRAFT
        notes.append(
            f"Each role is kept as a draft requisition for {department.name}: requisitions "
            f"can't be sent for approval yet ({problem}). Its position opens in AnytimeHire "
            "once it is sent and approved."
        )
    else:
        how = SUBMIT
        notes.append(
            f"Each role is raised as a requisition for {department.name} and sent for approval: "
            + " → ".join(approvers)
            + ". Its position opens in AnytimeHire once it is approved, so nobody recruits for "
            "a role the approvers turn down."
        )
    flags = await positions.off_budget_flags(
        session, user, position_id=None, department_id=department.id, requested_ctc_max=ceiling
    )
    told = [f["message"] for f in flags if f.get("severity") == "blocking_attention"]
    if told:
        notes.append("The approvers will also be told: " + " ".join(told))
    return {"id": str(department.id), "name": department.name, "requisitions": how}, notes, None


async def raise_for_role(
    session: AsyncSession,
    user: User,
    plan: dict[str, Any],
    role: dict[str, Any],
    job_ref: str | None,
    request: Request | None,
) -> dict[str, Any]:
    """Raise one role's requisition; send it for approval if it can go.

    With `job_ref` (a job already opened) it is linked to it, so approving it never
    opens a second one; without, its position opens in AnytimeHire on approval.
    A sub-admin's hiring rights are read as the portal reads them for this change.
    """
    from app.services.companion.actions import _HOME_PATH, _as_portal

    await _as_portal(session, user, request, "/api/v1/hiring/requisitions")
    try:
        if not permissions.is_hr(user):
            raise HTTPException(
                status_code=403,
                detail="Your HR rights don't cover raising requisitions. Ask an admin.",
            )
        return await _raise(session, user, plan, role, job_ref, request)
    finally:
        await _as_portal(session, user, request, _HOME_PATH)


async def _raise(
    session: AsyncSession,
    user: User,
    plan: dict[str, Any],
    role: dict[str, Any],
    job_ref: str | None,
    request: Request | None,
) -> dict[str, Any]:
    from sqlalchemy import func, select

    from app.core.deps import tenant_scoped
    from app.models.org import Location
    from app.schemas.hiring import RequisitionCreate
    from app.services.hiring import requisitions as reqs

    department = plan["department"]
    start = date.fromisoformat(plan["start_date"])
    experience = role.get("experience") or [0, 0]
    place = str(plan.get("location") or "").strip()
    location_id = (
        await session.scalar(
            tenant_scoped(
                select(Location.id).where(func.lower(Location.name) == place.lower()),
                Location,
                user,
            ).limit(1)
        )
        if place and place != "Not stated"
        else None
    )
    allocation = Decimal(str(role.get("allocation") or "100"))
    share = "" if allocation == 100 else f", {allocation.normalize()}% allocation"
    payload = RequisitionCreate(
        title=f"{role['title']} ({plan['account']})"[:255],
        department_id=uuid.UUID(department["id"]),
        openings=int(role["count"]),
        employment_type="full_time",
        budget_ctc_max=_yearly(Decimal(role["cost_rate"])),
        justification=(
            f"Order PO {plan['po_number']} from {plan['account']}: a {plan['months']}-month "
            f"engagement from {start:%d %b %Y}, booked from Ask Miraa."
        ),
        skills=[s for s in role.get("skills") or [] if s][:50],
        experience_min_years=int(experience[0]),
        experience_max_years=int(experience[1]),
        target_joining_date=start if start >= date.today() else None,
        location_id=location_id,
        # What the job in AnytimeHire says once it opens (its responsibilities).
        description=(
            f"Work: {place or 'Not stated'}, {plan.get('work_mode') or 'Not stated'}.\n"
            f"{plan['account']}: {plan['months']}-month engagement from {start:%d %b %Y} "
            f"(PO {plan['po_number']}){share}."
        ),
    )
    requisition = await reqs.create_requisition(session, user, payload, request=request)
    requisition.linked_job_ref = (job_ref or "")[:64] or None
    requisition.linked_job_at = datetime.now(UTC) if job_ref else None
    await session.flush()
    submitted = False
    told = ""
    if department["requisitions"] == SUBMIT:
        sent = await reqs.submit_requisition(
            session, user, requisition_id=requisition.id, request=request
        )
        submitted = True
        told = " ".join(
            str(f["message"]) for f in sent["flags"] if f.get("severity") == "blocking_attention"
        )
    return {
        "requisition_id": str(requisition.id),
        "code": requisition.code,
        "submitted": submitted,
        "told": told,
    }


async def cancel(
    session: AsyncSession, user: User, requisition_id: str, reason: str, request: Request | None
) -> None:
    """Take back a requisition the order raised: cancelled, or closed once it was approved."""
    from app.models.hiring import RequisitionStatus
    from app.services.hiring import requisitions as reqs
    from app.services.hiring import visibility

    requisition = await visibility.load_requisition(session, user, uuid.UUID(requisition_id))
    to = (
        RequisitionStatus.CLOSED
        if requisition.status in (RequisitionStatus.OPEN, RequisitionStatus.ON_HOLD)
        else RequisitionStatus.CANCELLED
    )
    if requisition.status in (RequisitionStatus.CANCELLED, RequisitionStatus.CLOSED):
        return
    await reqs.change_status(
        session, user, requisition_id=requisition.id, to_status=to, reason=reason, request=request
    )
