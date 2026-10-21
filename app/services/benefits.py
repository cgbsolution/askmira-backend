"""Group benefits: plans, enrolment, life events, insurer files, payroll deductions.

See `app.models.benefits` for the shape. The rules that matter:

- **Enrolment** happens in the plan's window (or any time, when the plan has
  none). The members chosen must be relations the plan covers, within its
  child limit. Enrolling again in the window replaces the earlier choice.
- **A life event** outside the window is a request: within the plan's days of
  the event, approved by HR, and only then does cover change.
- **Every change to who is covered** is written to `benefit_member_changes`,
  insert-only; the endorsement file for the insurer is read from there.
- **A top-up premium** becomes a monthly payroll adjustment for an admin to
  approve. Changing or dropping the top-up cancels the old one; what payroll
  already recovered stays recovered.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.benefits import (
    BenefitEnrolment,
    BenefitLifeEvent,
    BenefitMemberChange,
    BenefitPlan,
    EnrolmentStatus,
    LifeEventStatus,
)
from app.models.employee import Employee
from app.models.payroll_inputs import (
    AdjustmentCategory,
    AdjustmentKind,
    AdjustmentRecurrence,
    AdjustmentStatus,
    PayrollAdjustment,
)
from app.models.user import User
from app.schemas.benefits import EnrolIn, LifeEventIn, Member, PlanCreate, PlanUpdate
from app.services import audit

__all__ = [
    "census_csv",
    "create_plan",
    "decide_life_event",
    "endorsements_csv",
    "enrol",
    "list_life_events",
    "list_plans",
    "my_benefits",
    "raise_life_event",
    "update_plan",
]


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


def _money(value: Any) -> str:
    return str(Decimal(str(value or 0)).quantize(Decimal("0.01")))


def _plan_read(plan: BenefitPlan) -> dict[str, Any]:
    return {
        "id": str(plan.id),
        "name": plan.name,
        "kind": plan.kind.value,
        "insurer": plan.insurer,
        "tpa": plan.tpa,
        "policy_number": plan.policy_number,
        "policy_start": plan.policy_start.isoformat(),
        "policy_end": plan.policy_end.isoformat(),
        "sum_insured": _money(plan.sum_insured),
        "covers": list(plan.covers or []),
        "max_children": plan.max_children,
        "employer_premium_per_member": _money(plan.employer_premium_per_member),
        "top_ups": list(plan.top_ups or []),
        "enrolment_opens": plan.enrolment_opens.isoformat() if plan.enrolment_opens else None,
        "enrolment_closes": plan.enrolment_closes.isoformat() if plan.enrolment_closes else None,
        "life_event_window_days": plan.life_event_window_days,
        "active": plan.active,
    }


def _enrolment_read(row: BenefitEnrolment | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": str(row.id),
        "status": row.status.value,
        "members": list(row.members or []),
        "top_up_code": row.top_up_code,
        "employee_premium_annual": _money(row.employee_premium_annual),
        "effective_from": row.effective_from.isoformat(),
        "payroll_adjustment_id": str(row.payroll_adjustment_id)
        if row.payroll_adjustment_id
        else None,
    }


def _window_open(plan: BenefitPlan, today: date) -> bool:
    if plan.enrolment_opens is None:
        return True
    return plan.enrolment_opens <= today <= (plan.enrolment_closes or plan.enrolment_opens)


def _member_json(member: Member) -> dict[str, Any]:
    return {
        "name": member.name.strip(),
        "relationship": member.relationship,
        "date_of_birth": member.date_of_birth.isoformat() if member.date_of_birth else None,
        "gender": member.gender,
    }


def _check_members(plan: BenefitPlan, members: list[dict[str, Any]]) -> None:
    covered = set(plan.covers or [])
    for m in members:
        if m["relationship"] not in covered:
            raise _unprocessable(
                f"{plan.name} does not cover a {m['relationship'].replace('_', ' ')}."
            )
    spouses = sum(1 for m in members if m["relationship"] == "spouse")
    if spouses > 1:
        raise _unprocessable("Only one spouse can be covered.")
    children = sum(1 for m in members if m["relationship"] == "child")
    if plan.max_children is not None and children > plan.max_children:
        raise _unprocessable(f"{plan.name} covers at most {plan.max_children} children.")
    names = [m["name"].lower() for m in members]
    if len(set(names)) != len(names):
        raise _unprocessable("Each covered person is listed once.")


async def _plan(session: AsyncSession, user: User, plan_id: uuid.UUID) -> BenefitPlan:
    plan = await session.scalar(
        tenant_scoped(select(BenefitPlan).where(BenefitPlan.id == plan_id), BenefitPlan, user)
    )
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")
    return plan


# ── Plans ───────────────────────────────────────────────────────────────────


async def create_plan(
    session: AsyncSession, user: User, payload: PlanCreate, *, request: Request | None
) -> dict[str, Any]:
    values = payload.model_dump()
    values["top_ups"] = [
        {
            **t,
            "sum_insured": str(t["sum_insured"]),
            "employee_premium_annual": str(t["employee_premium_annual"]),
        }
        for t in values["top_ups"]
    ]
    plan = BenefitPlan(tenant_id=user.tenant_id, **values)
    session.add(plan)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="benefit_plan",
        entity_id=plan.id,
        actor=user,
        changes=_plan_read(plan),
        summary=f"Added the benefit plan {plan.name} ({plan.insurer})",
        request=request,
    )
    await session.commit()
    return _plan_read(plan)


async def update_plan(
    session: AsyncSession,
    user: User,
    plan_id: uuid.UUID,
    payload: PlanUpdate,
    *,
    request: Request | None,
) -> dict[str, Any]:
    plan = await _plan(session, user, plan_id)
    before = _plan_read(plan)
    for key, value in payload.model_dump(exclude_unset=True).items():
        if value is None and key in ("employer_premium_per_member", "active"):
            continue
        setattr(plan, key, value)
    if (plan.enrolment_opens is None) != (plan.enrolment_closes is None):
        raise _unprocessable("Give both enrolment dates, or neither.")
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="benefit_plan",
        entity_id=plan.id,
        actor=user,
        changes={"before": before, "after": _plan_read(plan)},
        summary=f"Changed the benefit plan {plan.name}",
        request=request,
    )
    await session.commit()
    return _plan_read(plan)


async def list_plans(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    stmt = select(BenefitPlan).order_by(BenefitPlan.policy_start.desc())
    if not permissions.is_hr(user):
        stmt = stmt.where(BenefitPlan.active.is_(True))
    plans = (await session.scalars(tenant_scoped(stmt, BenefitPlan, user))).all()
    counts: dict[uuid.UUID, int] = {}
    if permissions.is_hr(user):
        for row in (
            await session.scalars(
                tenant_scoped(
                    select(BenefitEnrolment).where(
                        BenefitEnrolment.status == EnrolmentStatus.ENROLLED
                    ),
                    BenefitEnrolment,
                    user,
                )
            )
        ).all():
            counts[row.plan_id] = counts.get(row.plan_id, 0) + 1
    return [{**_plan_read(p), "enrolled": counts.get(p.id) if counts else None} for p in plans]


# ── Enrolment ───────────────────────────────────────────────────────────────


async def my_benefits(
    session: AsyncSession, user: User, *, today: date | None = None
) -> list[dict[str, Any]]:
    permissions.own_employee_id(user)
    day = today or date.today()
    plans = (
        await session.scalars(
            tenant_scoped(
                select(BenefitPlan).where(
                    BenefitPlan.active.is_(True), BenefitPlan.policy_end >= day
                ),
                BenefitPlan,
                user,
            )
        )
    ).all()
    mine = {
        e.plan_id: e
        for e in (
            await session.scalars(
                tenant_scoped(
                    select(BenefitEnrolment).where(
                        BenefitEnrolment.employee_id == user.employee_id
                    ),
                    BenefitEnrolment,
                    user,
                )
            )
        ).all()
    }
    return [
        {
            "plan": _plan_read(p),
            "enrolment": _enrolment_read(mine.get(p.id)),
            "window_open": _window_open(p, day),
        }
        for p in plans
    ]


async def _sync_premium(
    session: AsyncSession,
    user: User,
    plan: BenefitPlan,
    enrolment: BenefitEnrolment,
    *,
    starting: date,
) -> None:
    """The top-up premium as a monthly deduction, pending an admin's approval."""
    if enrolment.payroll_adjustment_id is not None:
        old = await session.get(PayrollAdjustment, enrolment.payroll_adjustment_id)
        if old is not None and old.status in (AdjustmentStatus.PENDING, AdjustmentStatus.APPROVED):
            old.status = AdjustmentStatus.CANCELLED
            old.cancelled_at = datetime.now(UTC)
            old.decision_note = "The benefit top-up changed."
        enrolment.payroll_adjustment_id = None
    premium = Decimal(str(enrolment.employee_premium_annual or 0))
    if premium <= 0 or enrolment.status is not EnrolmentStatus.ENROLLED:
        return
    monthly = (premium / 12).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    adjustment = PayrollAdjustment(
        tenant_id=user.tenant_id,
        employee_id=enrolment.employee_id,
        kind=AdjustmentKind.DEDUCTION,
        category=AdjustmentCategory.OTHER_DEDUCTION,
        name=f"{plan.name} top-up premium"[:128],
        amount=monthly,
        taxable=False,
        counts_as_wages=False,
        within_net_pay=True,
        recurrence=AdjustmentRecurrence.MONTHLY,
        start_year=starting.year,
        start_month=starting.month,
        end_year=plan.policy_end.year,
        end_month=plan.policy_end.month,
        total_cap=premium,
        reference=f"benefit:{enrolment.id}"[:128],
        reason=(
            f"Voluntary top-up {enrolment.top_up_code} chosen at enrolment on "
            f"{date.today():%d %b %Y}."
        ),
        status=AdjustmentStatus.PENDING,
        created_by_user_id=None,
    )
    session.add(adjustment)
    await session.flush()
    enrolment.payroll_adjustment_id = adjustment.id


async def enrol(
    session: AsyncSession,
    user: User,
    plan_id: uuid.UUID,
    payload: EnrolIn,
    *,
    today: date | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """Enrol (or waive) in a plan's window. Re-enrolling replaces the choice."""
    permissions.own_employee_id(user)
    day = today or date.today()
    plan = await _plan(session, user, plan_id)
    if not plan.active or plan.policy_end < day:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This plan has ended.")
    if not _window_open(plan, day):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Enrolment is closed. A marriage, birth or other life event can still "
            "change cover; raise it as a life event.",
        )
    members = [] if payload.waive else [_member_json(m) for m in payload.members]
    _check_members(plan, members)
    top_up = None
    if payload.top_up_code and not payload.waive:
        top_up = next((t for t in plan.top_ups or [] if t["code"] == payload.top_up_code), None)
        if top_up is None:
            raise _unprocessable("That top-up is not offered on this plan.")

    row = await session.scalar(
        tenant_scoped(
            select(BenefitEnrolment).where(
                BenefitEnrolment.plan_id == plan.id,
                BenefitEnrolment.employee_id == user.employee_id,
            ),
            BenefitEnrolment,
            user,
        )
    )
    effective = max(day, plan.policy_start)
    previous_members = list(row.members or []) if row is not None else []
    if row is None:
        row = BenefitEnrolment(
            tenant_id=user.tenant_id,
            plan_id=plan.id,
            employee_id=user.employee_id,
            status=EnrolmentStatus.WAIVED,
            members=[],
            effective_from=effective,
        )
        session.add(row)
        await session.flush()
    row.status = EnrolmentStatus.WAIVED if payload.waive else EnrolmentStatus.ENROLLED
    row.members = members
    row.top_up_code = top_up["code"] if top_up else None
    row.employee_premium_annual = (
        Decimal(str(top_up["employee_premium_annual"])) if top_up else Decimal("0")
    )
    row.effective_from = effective
    _record_member_changes(
        session,
        user,
        row,
        previous_members,
        members,
        effective,
        "waiver" if payload.waive else "enrolment",
    )
    await _sync_premium(session, user, plan, row, starting=effective)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="benefit_enrolment",
        entity_id=row.id,
        actor=user,
        changes={"status": row.status.value, "members": len(members), "top_up": row.top_up_code},
        summary=(
            f"Waived {plan.name}"
            if payload.waive
            else f"Enrolled in {plan.name} with {len(members)} dependant(s)"
        ),
        request=request,
    )
    await session.commit()
    return _enrolment_read(row) or {}


def _record_member_changes(
    session: AsyncSession,
    user: User,
    enrolment: BenefitEnrolment,
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    effective: date,
    source: str,
) -> None:
    def key(m: dict[str, Any]) -> tuple[str, str]:
        return (m["name"].lower(), m["relationship"])

    old = {key(m): m for m in before}
    new = {key(m): m for m in after}
    for k, m in new.items():
        if k not in old:
            session.add(
                BenefitMemberChange(
                    tenant_id=user.tenant_id,
                    enrolment_id=enrolment.id,
                    plan_id=enrolment.plan_id,
                    change="add",
                    member=m,
                    effective_from=effective,
                    source=source,
                )
            )
    for k, m in old.items():
        if k not in new:
            session.add(
                BenefitMemberChange(
                    tenant_id=user.tenant_id,
                    enrolment_id=enrolment.id,
                    plan_id=enrolment.plan_id,
                    change="remove",
                    member=m,
                    effective_from=effective,
                    source=source,
                )
            )


# ── Life events ─────────────────────────────────────────────────────────────


async def raise_life_event(
    session: AsyncSession,
    user: User,
    payload: LifeEventIn,
    *,
    today: date | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    permissions.own_employee_id(user)
    day = today or date.today()
    plan = await _plan(session, user, payload.plan_id)
    if payload.event_date > day:
        raise _unprocessable("A life event is raised after it happens.")
    if day - payload.event_date > timedelta(days=plan.life_event_window_days):
        raise _unprocessable(
            f"{plan.name} accepts changes within {plan.life_event_window_days} days of the "
            "event. Ask HR about the next enrolment window."
        )
    enrolment = await session.scalar(
        tenant_scoped(
            select(BenefitEnrolment).where(
                BenefitEnrolment.plan_id == plan.id,
                BenefitEnrolment.employee_id == user.employee_id,
                BenefitEnrolment.status == EnrolmentStatus.ENROLLED,
            ),
            BenefitEnrolment,
            user,
        )
    )
    if enrolment is None:
        raise _unprocessable("You are not enrolled in this plan.")
    adds = [_member_json(m) for m in payload.add_members]
    names = {m["name"].lower() for m in enrolment.members or []}
    missing = [n for n in payload.remove_members if n.strip().lower() not in names]
    if missing:
        raise _unprocessable(f"Not covered now: {', '.join(missing)}.")
    after = [
        m
        for m in enrolment.members or []
        if m["name"].lower() not in {n.strip().lower() for n in payload.remove_members}
    ]
    _check_members(plan, after + adds)
    event = BenefitLifeEvent(
        tenant_id=user.tenant_id,
        enrolment_id=enrolment.id,
        employee_id=user.employee_id,
        kind=payload.kind,
        event_date=payload.event_date,
        add_members=adds,
        remove_members=[n.strip() for n in payload.remove_members],
        note=(payload.note or "").strip() or None,
    )
    session.add(event)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="benefit_life_event",
        entity_id=event.id,
        actor=user,
        changes={"kind": event.kind.value, "event_date": event.event_date.isoformat()},
        summary=f"Raised a {event.kind.value} change to {plan.name}",
        request=request,
    )
    await session.commit()
    return _life_event_read(event, plan)


def _life_event_read(event: BenefitLifeEvent, plan: BenefitPlan | None) -> dict[str, Any]:
    return {
        "id": str(event.id),
        "plan_name": plan.name if plan else None,
        "employee_id": str(event.employee_id),
        "kind": event.kind.value,
        "event_date": event.event_date.isoformat(),
        "add_members": list(event.add_members or []),
        "remove_members": list(event.remove_members or []),
        "note": event.note,
        "status": event.status.value,
        "decision_note": event.decision_note,
    }


async def list_life_events(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """HR: every pending change. Anyone else: their own."""
    stmt = (
        select(BenefitLifeEvent, BenefitPlan, Employee)
        .join(BenefitEnrolment, BenefitEnrolment.id == BenefitLifeEvent.enrolment_id)
        .join(BenefitPlan, BenefitPlan.id == BenefitEnrolment.plan_id)
        .join(Employee, Employee.id == BenefitLifeEvent.employee_id)
    )
    if permissions.is_hr(user):
        stmt = stmt.where(BenefitLifeEvent.status == LifeEventStatus.PENDING)
    else:
        permissions.own_employee_id(user)
        stmt = stmt.where(BenefitLifeEvent.employee_id == user.employee_id)
    rows = (
        await session.execute(
            tenant_scoped(stmt.order_by(BenefitLifeEvent.created_at.desc()), BenefitLifeEvent, user)
        )
    ).all()
    return [
        {
            **_life_event_read(event, plan),
            "employee_name": employee.full_name,
            "employee_code": employee.employee_code,
        }
        for event, plan, employee in rows
    ]


async def decide_life_event(
    session: AsyncSession,
    user: User,
    event_id: uuid.UUID,
    *,
    approve: bool,
    note: str | None,
    request: Request | None = None,
) -> dict[str, Any]:
    permissions.require_hr(user, detail="HR decides benefit changes.")
    event = await session.scalar(
        tenant_scoped(
            select(BenefitLifeEvent).where(BenefitLifeEvent.id == event_id),
            BenefitLifeEvent,
            user,
        )
    )
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Change not found")
    if event.employee_id == user.employee_id:
        raise permissions.forbidden("Somebody else in HR decides your own benefit change.")
    if event.status is not LifeEventStatus.PENDING:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already decided.")
    enrolment = await session.get(BenefitEnrolment, event.enrolment_id)
    plan = await session.get(BenefitPlan, enrolment.plan_id) if enrolment else None
    if enrolment is None or plan is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="The enrolment is gone.")
    if approve:
        removed = {n.lower() for n in event.remove_members or []}
        before = list(enrolment.members or [])
        after = [m for m in before if m["name"].lower() not in removed] + list(
            event.add_members or []
        )
        _check_members(plan, after)
        enrolment.members = after
        _record_member_changes(
            session, user, enrolment, before, after, event.event_date, "life_event"
        )
    event.status = LifeEventStatus.APPROVED if approve else LifeEventStatus.REJECTED
    event.decided_by_user_id = user.id
    event.decided_at = datetime.now(UTC)
    event.decision_note = (note or "").strip() or None
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.APPROVE if approve else AuditAction.REJECT,
        entity_type="benefit_life_event",
        entity_id=event.id,
        actor=user,
        changes={"status": event.status.value},
        summary=(
            f"{'Approved' if approve else 'Rejected'} a {event.kind.value} change to {plan.name}"
        ),
        request=request,
    )
    await session.commit()
    return _life_event_read(event, plan)


# ── Insurer files ───────────────────────────────────────────────────────────

_CENSUS_HEADER = [
    "Policy number",
    "Employee code",
    "Employee name",
    "Member name",
    "Relationship",
    "Date of birth",
    "Gender",
    "Sum insured",
    "Top-up",
    "Cover from",
]


def _cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r")) else text


async def census_csv(
    session: AsyncSession, user: User, plan_id: uuid.UUID, *, request: Request | None = None
) -> str:
    """Every covered person on a plan today: the file an insurer or TPA asks for."""
    permissions.require_hr(user, detail="The insurer census is for HR.")
    plan = await _plan(session, user, plan_id)
    rows = (
        await session.execute(
            tenant_scoped(
                select(BenefitEnrolment, Employee)
                .join(Employee, Employee.id == BenefitEnrolment.employee_id)
                .where(
                    BenefitEnrolment.plan_id == plan.id,
                    BenefitEnrolment.status == EnrolmentStatus.ENROLLED,
                )
                .order_by(Employee.employee_code),
                BenefitEnrolment,
                user,
            )
        )
    ).all()
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(_CENSUS_HEADER)
    lives = 0
    for enrolment, employee in rows:
        people = [
            {
                "name": employee.full_name,
                "relationship": "self",
                "date_of_birth": employee.date_of_birth.isoformat()
                if employee.date_of_birth
                else None,
                "gender": employee.gender.value if employee.gender else None,
            },
            *list(enrolment.members or []),
        ]
        for member in people:
            lives += 1
            writer.writerow(
                [
                    _cell(v)
                    for v in (
                        plan.policy_number,
                        employee.employee_code,
                        employee.full_name,
                        member["name"],
                        member["relationship"],
                        member.get("date_of_birth"),
                        member.get("gender"),
                        _money(plan.sum_insured),
                        enrolment.top_up_code,
                        enrolment.effective_from.isoformat(),
                    )
                ]
            )
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.EXPORT,
        entity_type="benefit_plan",
        entity_id=plan.id,
        actor=user,
        changes={"lives": lives},
        summary=f"Exported the {plan.name} census ({lives} lives)",
        request=request,
    )
    await session.commit()
    return buffer.getvalue()


async def endorsements_csv(
    session: AsyncSession,
    user: User,
    plan_id: uuid.UUID,
    *,
    since: date,
    request: Request | None = None,
) -> str:
    """Additions and deletions since a date: the endorsement the insurer needs."""
    permissions.require_hr(user, detail="Endorsements are for HR.")
    plan = await _plan(session, user, plan_id)
    rows = (
        await session.execute(
            tenant_scoped(
                select(BenefitMemberChange, Employee)
                .join(BenefitEnrolment, BenefitEnrolment.id == BenefitMemberChange.enrolment_id)
                .join(Employee, Employee.id == BenefitEnrolment.employee_id)
                .where(
                    BenefitMemberChange.plan_id == plan.id,
                    BenefitMemberChange.created_at
                    >= datetime.combine(since, datetime.min.time(), UTC),
                )
                .order_by(BenefitMemberChange.created_at),
                BenefitMemberChange,
                user,
            )
        )
    ).all()
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(
        [
            "Policy number",
            "Change",
            "Employee code",
            "Employee name",
            "Member name",
            "Relationship",
            "Date of birth",
            "Gender",
            "Effective from",
            "Reason",
        ]
    )
    for change, employee in rows:
        m = change.member or {}
        writer.writerow(
            [
                _cell(v)
                for v in (
                    plan.policy_number,
                    "Addition" if change.change == "add" else "Deletion",
                    employee.employee_code,
                    employee.full_name,
                    m.get("name"),
                    m.get("relationship"),
                    m.get("date_of_birth"),
                    m.get("gender"),
                    change.effective_from.isoformat(),
                    change.source.replace("_", " "),
                )
            ]
        )
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.EXPORT,
        entity_type="benefit_plan",
        entity_id=plan.id,
        actor=user,
        changes={"since": since.isoformat(), "rows": len(rows)},
        summary=f"Exported {plan.name} endorsements since {since}",
        request=request,
    )
    await session.commit()
    return buffer.getvalue()
