"""Ask Miraa raises a requisition: "Raise the second requisition."

Miraa finds the department, checks its headcount budget for HR — counting the
seats requisitions already ask for, and saying that only they see this check
(a manager sees no budget) — works out the approval path the requisition will
follow and what its approvers will be told, and offers all of it. A sub-admin's
hiring rights are read as the portal reads them. A changed request ("yes, but
at 16 lakh") replaces the earlier offer. Nothing is written until the person
says yes. Then the hiring module's own services raise it and send it
for approval (create_requisition, submit_requisition): the same checks, budget
flags, approval steps and audit rows as the portal. Once it is approved, the
existing hand-off opens the job in AnytimeHire when that is linked
(hiring/linked_jobs.py).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.companion import CompanionAction
from app.models.employee import Employee
from app.models.offer import ApprovalScope
from app.models.org import Department
from app.models.tenant import Tenant
from app.models.user import User
from app.services.companion.budget_tools import rupees_in
from app.services.documents.indian_numbers import group_indian

__all__ = ["EMPLOYMENT_TYPES", "available_to", "carry_out", "propose"]

EMPLOYMENT_TYPES = ("full_time", "part_time", "contract", "intern")
_TYPE_LABEL = {
    "full_time": "Full time",
    "part_time": "Part time",
    "contract": "Contract",
    "intern": "Intern",
}
#: The most seats one requisition may ask for here (the portal allows more).
MAX_OPENINGS = 50
_PATH = "/api/v1/hiring/requisitions"


def _notice(text: str, tone: str = "info") -> dict[str, Any]:
    return {"type": "notice", "tone": tone, "text": text}


def _inr(value: Decimal) -> str:
    return f"₹{group_indian(value.quantize(Decimal('1')))}"


async def available_to(session: AsyncSession, user: User) -> bool:
    """Whether this person may raise requisitions here: HR or a manager, hiring switched on."""
    if not (permissions.is_hr(user) or getattr(user, "has_reports", False)):
        return False
    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None:
        return False
    return bool((tenant.enabled_modules or {}).get("hiring")) or bool(
        (tenant.linked_products or {}).get("interview")
    )


async def _departments(session: AsyncSession, user: User) -> list[Department]:
    rows = await session.scalars(
        tenant_scoped(select(Department).order_by(Department.name), Department, user)
    )
    return list(rows)


def _match(departments: list[Department], wanted: str) -> list[Department]:
    wanted = " ".join(wanted.lower().split())
    if not wanted:
        return []
    exact = [d for d in departments if d.name.lower() == wanted or d.code.lower() == wanted]
    if exact:
        return exact
    return [d for d in departments if wanted in d.name.lower()]


async def _own_department(session: AsyncSession, user: User) -> uuid.UUID | None:
    if user.employee_id is None:
        return None
    return await session.scalar(
        tenant_scoped(
            select(Employee.department_id).where(Employee.id == user.employee_id), Employee, user
        )
    )


async def _approval_path(session: AsyncSession, user: User, ctc: Decimal, grade: str | None):
    """The approvers this requisition would go to, or why it cannot be sent at all."""
    from app.services.hiring import approval_chain
    from app.services.hiring import requisitions as reqs

    rules = await reqs.load_rules(session, user, ApprovalScope.REQUISITION)
    try:
        approval_chain.validate_rules(rules)
        chain = approval_chain.resolve_chain(rules, ctc_annual=ctc, grade=grade)
    except approval_chain.ApprovalChainError as exc:
        return None, str(exc)
    return [step.label for step in chain], None


def counting(budget: dict[str, Any]) -> str:
    """ ", counting the 5 seats already asked for in requisitions" — or nothing."""
    asked = int(budget.get("requisition_seats") or 0)
    if not asked:
        return ""
    seats = "seat" if asked == 1 else "seats"
    return f", counting the {asked} {seats} already asked for in requisitions"


async def _budget_note(
    session: AsyncSession, user: User, department: Department, openings: int, ctc: Decimal
) -> str | None:
    """What the department's headcount budget says about these seats, in a sentence (HR only).

    Seats that requisitions in flight already ask for count too. Said as HR's own
    check: the approvers are shown the requisition's flags, not this.
    """
    if not permissions.is_hr(user):
        return None  # budgets are HR's; the approvers decide
    from app.services.positions import service as positions

    answer = await positions.budget_room(
        session,
        user,
        on=date.today(),
        department=department.name,
        hires=openings,
        ctc_per_hire=ctc,
        requisitions=True,
    )
    year = answer["financial_year"]
    mine = [b for b in answer["budgets"] if b["department_id"] == str(department.id)]
    if not mine:
        return f"{department.name} has no headcount budget for {year}."
    budget = mine[0]
    seats = "seat fits" if openings == 1 else "seats fit"
    only_you = " Only you see this check; the approvers see what follows."
    if budget["fits"] is True:
        return (
            f"Inside {department.name}'s {year} headcount budget{counting(budget)}: {openings} "
            f"more {seats} at {_inr(ctc)} a year each." + only_you
        )
    if budget["fits"] is False:
        problems = " ".join(budget["problems"])
        return (
            f"Over {department.name}'s {year} headcount budget{counting(budget)}: {problems}"
            + only_you
        )
    return f"{department.name}'s budget could not be checked for these seats."


async def propose(
    session: AsyncSession, user: User, inputs: dict[str, Any], ctx: Any
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Offer a requisition with its department, seats, CTC ceiling, budget check and approvers."""
    from app.services.companion.actions import _HOME_PATH, _as_portal

    request = getattr(ctx, "request", None)
    # A sub-admin's hiring rights, as POST /hiring/requisitions would read them; the
    # rest of the turn goes on with the rights of a message to Ask Miraa.
    await _as_portal(session, user, request, _PATH)
    try:
        return await _propose(session, user, inputs, ctx)
    finally:
        await _as_portal(session, user, request, _HOME_PATH)


async def _propose(
    session: AsyncSession, user: User, inputs: dict[str, Any], ctx: Any
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from app.services.companion.actions import _offer, card_for

    if not await available_to(session, user):
        return [
            _notice(
                "Only HR or a manager can raise a requisition, and hiring has to be switched on "
                "for this workspace.",
                "warning",
            )
        ], {}

    title = " ".join(str(inputs.get("role_title") or "").split())[:255]
    wanted = str(inputs.get("department") or "").strip()
    try:
        openings = int(str(inputs.get("openings") or "1").strip() or 1)
    except ValueError:
        openings = 0
    ctc = rupees_in(inputs.get("ctc_max"))
    justification = " ".join(str(inputs.get("justification") or "").split())[:4000]
    kind = str(inputs.get("employment_type") or "full_time")
    if kind not in EMPLOYMENT_TYPES:
        kind = "full_time"
    grade = " ".join(str(inputs.get("grade") or "").split())[:32] or None
    joining = None
    raw_joining = str(inputs.get("joining_by") or "").strip()
    if raw_joining:
        try:
            joining = date.fromisoformat(raw_joining)
        except ValueError:
            return [_notice("When should they join? Give a date like 2026-12-01.")], {}
        if joining < date.today():
            return [_notice("The joining date has passed. When should they join?")], {}
    used = {"role_title": title, "department": wanted, "openings": openings}

    departments = await _departments(session, user)
    department: Department | None = None
    if wanted:
        found = _match(departments, wanted)
        if len(found) > 1:
            names = ", ".join(d.name for d in found[:10])
            return [_notice(f"Which department: {names}?")], used
        if not found:
            names = ", ".join(d.name for d in departments[:15]) or "none set up yet"
            text = f"There is no department called “{wanted}”. These exist: {names}."
            return [_notice(text)], used
        department = found[0]
    elif not permissions.is_hr(user):
        own = await _own_department(session, user)
        department = next((d for d in departments if d.id == own), None)
    if department is None:
        return [_notice("Which department is the role in?")], used
    if not permissions.is_hr(user):
        own = await _own_department(session, user)
        if department.id != own:
            return [
                _notice(
                    "A manager raises requisitions for their own team. Ask HR to raise one for "
                    f"{department.name}.",
                    "warning",
                )
            ], used

    missing = []
    if len(title) < 2:
        missing.append("the role's title")
    if ctc is None:
        missing.append("the yearly CTC ceiling per hire")
    if not justification:
        missing.append("why the role is needed (the approvers read it)")
    if missing:
        return [_notice("Before I prepare it, tell me " + ", ".join(missing) + ".")], used
    if not 1 <= openings <= MAX_OPENINGS:
        return [_notice(f"How many seats? Between 1 and {MAX_OPENINGS} here.")], used
    assert ctc is not None  # checked above

    approvers, problem = await _approval_path(session, user, ctc, grade)
    if approvers is None:
        return [
            _notice(
                "Requisitions can't be sent for approval yet: "
                f"{problem} HR sets the approval rules under Hiring.",
                "warning",
            )
        ], used
    budget = await _budget_note(session, user, department, openings, ctc)

    requisition = {
        "title": title,
        "department_id": str(department.id),
        "openings": openings,
        "employment_type": kind,
        "grade": grade,
        "budget_ctc_max": str(ctc),
        "justification": justification,
        "target_joining_date": joining.isoformat() if joining else None,
    }
    figures = [
        {"label": "Role", "value": title},
        {"label": "Department", "value": department.name},
        {"label": "Seats", "value": str(openings)},
        {"label": "CTC ceiling", "value": f"{_inr(ctc)} a year each"},
        {"label": "Type", "value": _TYPE_LABEL[kind]},
    ]
    if grade:
        figures.append({"label": "Grade", "value": grade})
    if joining:
        figures.append({"label": "Joining by", "value": f"{joining:%d %b %Y}"})
    # What the approvers will be told when it is sent, said before the yes, so
    # "it fits the budget" and their flag never surprise anyone.
    from app.services.positions import service as positions

    flags = await positions.off_budget_flags(
        session, user, position_id=None, department_id=department.id, requested_ctc_max=ctc
    )
    told = [f["message"] for f in flags if f.get("severity") == "blocking_attention"]
    notes = [f"Why: {justification[:300]}"]
    if budget:
        notes.append(budget)
    if told:
        notes.append("The approvers will also be told: " + " ".join(told))
    notes.append("Approval path: " + " → ".join(approvers) + ".")
    action = await _offer(
        session,
        user,
        ctx,
        kind="requisition",
        payload={"requisition": requisition},
        summary={
            "title": "Requisition",
            "figures": figures,
            "notes": notes,
            "confirm_label": "Raise and send for approval",
            "question": "Raise this requisition and send it for approval?",
        },
        # The same role for the same department, asked for again with a change.
        replaces=f"{' '.join(title.lower().split())}|{department.id}",
    )
    return [card_for(action)], used


async def carry_out(
    session: AsyncSession, user: User, action: CompanionAction, request: Request | None
) -> dict[str, Any]:
    """Raise it and send it for approval, through the hiring module's own services."""
    from app.schemas.hiring import RequisitionCreate
    from app.services.companion.actions import _as_portal
    from app.services.hiring import requisitions as reqs

    # Rights first, as the portal reads them for this change; then who may raise it.
    await _as_portal(session, user, request, _PATH)
    if not await available_to(session, user):
        raise HTTPException(
            status_code=403, detail="You can no longer raise requisitions in this workspace."
        )
    payload = RequisitionCreate.model_validate(action.payload["requisition"])
    if not permissions.is_hr(user) and payload.department_id != await _own_department(
        session, user
    ):
        raise HTTPException(
            status_code=403,
            detail="A manager raises requisitions for their own team. Ask HR to raise this one.",
        )
    requisition = await reqs.create_requisition(session, user, payload, request=request)
    sent = await reqs.submit_requisition(
        session, user, requisition_id=requisition.id, request=request
    )
    chain = [step["role"] for step in sent["chain"]]
    flags = [f for f in sent["flags"] if f.get("severity") == "blocking_attention"]
    text = f"Requisition {requisition.code} is raised and waiting for approval"
    text += f" ({len(chain)} step{'s' if len(chain) != 1 else ''})." if chain else "."
    if flags:
        text += " The approvers see: " + " ".join(f["message"] for f in flags)
    return {"text": text, "open": "/hiring", "reference": requisition.code}
