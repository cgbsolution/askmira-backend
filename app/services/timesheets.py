"""Projects, weekly timesheets, approval, hours reports and hourly pay.

- A person fills a week (Monday to Sunday) with entries — a date, a project
  (or none), hours, a note — and submits it. A submitted week is not edited;
  a rejected one goes back to draft for changes.
- The line manager, or HR, approves or rejects it. Nobody decides their own.
- On approval, if the person has an hourly rate, the week's pay is priced day
  by day at the rate in force that day and becomes a one-time payroll
  adjustment for the month the week ends in, pending an admin's approval.
- Reports total approved hours by project and person for a date range.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.payroll_inputs import (
    AdjustmentCategory,
    AdjustmentKind,
    AdjustmentRecurrence,
    AdjustmentStatus,
    PayrollAdjustment,
)
from app.models.timesheets import (
    HourlyRate,
    Project,
    Timesheet,
    TimesheetEntry,
    TimesheetStatus,
)
from app.models.user import User
from app.services import audit

__all__ = [
    "EntryIn",
    "ProjectIn",
    "RateIn",
    "add_rate",
    "create_project",
    "decide",
    "hours_report",
    "list_projects",
    "list_rates",
    "my_week",
    "pending_for_approver",
    "save_week",
    "submit_week",
    "update_project",
]


class ProjectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_\-]+$")
    name: str = Field(min_length=2, max_length=160)
    client: str | None = Field(default=None, max_length=160)
    billable: bool = True


class EntryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    work_date: date
    project_id: uuid.UUID | None = None
    hours: Decimal = Field(gt=0, le=24, max_digits=5, decimal_places=2)
    description: str | None = Field(default=None, max_length=500)


class RateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    employee_id: uuid.UUID
    rate: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    effective_from: date
    reason: str | None = Field(default=None, max_length=500)


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


# ── Projects ────────────────────────────────────────────────────────────────


def _project_read(p: Project) -> dict[str, Any]:
    return {
        "id": str(p.id),
        "code": p.code,
        "name": p.name,
        "client": p.client,
        "billable": p.billable,
        "active": p.active,
    }


async def list_projects(
    session: AsyncSession, user: User, *, include_inactive: bool = False
) -> list[dict[str, Any]]:
    stmt = select(Project).order_by(Project.code)
    if not (include_inactive and permissions.is_hr(user)):
        stmt = stmt.where(Project.active.is_(True))
    return [
        _project_read(p) for p in (await session.scalars(tenant_scoped(stmt, Project, user))).all()
    ]


async def create_project(
    session: AsyncSession, user: User, payload: ProjectIn, *, request: Request | None
) -> dict[str, Any]:
    taken = await session.scalar(
        tenant_scoped(select(Project.id).where(Project.code == payload.code), Project, user)
    )
    if taken is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="That code is in use.")
    row = Project(tenant_id=user.tenant_id, **payload.model_dump())
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="project",
        entity_id=row.id,
        actor=user,
        changes=_project_read(row),
        summary=f"Added the project {row.code}",
        request=request,
    )
    await session.commit()
    return _project_read(row)


async def update_project(
    session: AsyncSession,
    user: User,
    project_id: uuid.UUID,
    *,
    name: str | None,
    client: str | None,
    billable: bool | None,
    active: bool | None,
    request: Request | None,
) -> dict[str, Any]:
    row = await session.scalar(
        tenant_scoped(select(Project).where(Project.id == project_id), Project, user)
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    before = _project_read(row)
    for key, value in (
        ("name", name),
        ("client", client),
        ("billable", billable),
        ("active", active),
    ):
        if value is not None:
            setattr(row, key, value)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="project",
        entity_id=row.id,
        actor=user,
        changes={"before": before, "after": _project_read(row)},
        summary=f"Changed the project {row.code}",
        request=request,
    )
    await session.commit()
    return _project_read(row)


# ── Weeks ───────────────────────────────────────────────────────────────────


async def _sheet(
    session: AsyncSession, user: User, employee_id: uuid.UUID, week_start: date
) -> Timesheet | None:
    return await session.scalar(
        tenant_scoped(
            select(Timesheet).where(
                Timesheet.employee_id == employee_id, Timesheet.week_start == week_start
            ),
            Timesheet,
            user,
        )
    )


async def _entries(session: AsyncSession, user: User, sheet: Timesheet) -> list[TimesheetEntry]:
    return list(
        (
            await session.scalars(
                tenant_scoped(
                    select(TimesheetEntry)
                    .where(TimesheetEntry.timesheet_id == sheet.id)
                    .order_by(TimesheetEntry.work_date, TimesheetEntry.created_at),
                    TimesheetEntry,
                    user,
                )
            )
        ).all()
    )


async def _read(
    session: AsyncSession,
    user: User,
    sheet: Timesheet | None,
    *,
    week_start: date,
    employee_id: uuid.UUID,
) -> dict[str, Any]:
    entries = await _entries(session, user, sheet) if sheet else []
    projects = (
        {
            p.id: p
            for p in (
                await session.scalars(
                    tenant_scoped(
                        select(Project).where(
                            Project.id.in_({e.project_id for e in entries if e.project_id})
                        ),
                        Project,
                        user,
                    )
                )
            ).all()
        }
        if entries
        else {}
    )
    total = sum((Decimal(str(e.hours)) for e in entries), start=Decimal(0))
    return {
        "id": str(sheet.id) if sheet else None,
        "employee_id": str(employee_id),
        "week_start": week_start.isoformat(),
        "status": sheet.status.value if sheet else "draft",
        "decision_note": sheet.decision_note if sheet else None,
        "total_hours": str(total),
        "entries": [
            {
                "id": str(e.id),
                "work_date": e.work_date.isoformat(),
                "project_id": str(e.project_id) if e.project_id else None,
                "project": projects[e.project_id].name if e.project_id in projects else None,
                "hours": str(e.hours),
                "description": e.description,
            }
            for e in entries
        ],
    }


async def my_week(session: AsyncSession, user: User, week_of: date) -> dict[str, Any]:
    permissions.own_employee_id(user)
    start = _monday(week_of)
    sheet = await _sheet(session, user, user.employee_id, start)
    return await _read(session, user, sheet, week_start=start, employee_id=user.employee_id)


async def save_week(
    session: AsyncSession, user: User, week_of: date, entries: list[EntryIn]
) -> dict[str, Any]:
    """Replace the week's entries. Only a draft or rejected week changes."""
    permissions.own_employee_id(user)
    start = _monday(week_of)
    end = start + timedelta(days=6)
    for entry in entries:
        if not start <= entry.work_date <= end:
            raise _unprocessable(f"{entry.work_date} is not in the week of {start}.")
    per_day: dict[date, Decimal] = {}
    for entry in entries:
        per_day[entry.work_date] = per_day.get(entry.work_date, Decimal(0)) + entry.hours
    over = [d for d, h in per_day.items() if h > 24]
    if over:
        raise _unprocessable(f"More than 24 hours on {over[0]}.")
    project_ids = {e.project_id for e in entries if e.project_id}
    if project_ids:
        known = set(
            (
                await session.scalars(
                    tenant_scoped(
                        select(Project.id).where(
                            Project.id.in_(project_ids), Project.active.is_(True)
                        ),
                        Project,
                        user,
                    )
                )
            ).all()
        )
        if project_ids - known:
            raise _unprocessable("One of those projects is not open for time.")

    sheet = await _sheet(session, user, user.employee_id, start)
    if sheet is None:
        sheet = Timesheet(tenant_id=user.tenant_id, employee_id=user.employee_id, week_start=start)
        session.add(sheet)
        await session.flush()
    elif sheet.status not in (TimesheetStatus.DRAFT, TimesheetStatus.REJECTED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This week is {sheet.status.value}; it can no longer be changed.",
        )
    for old in await _entries(session, user, sheet):
        await session.delete(old)
    for entry in entries:
        session.add(
            TimesheetEntry(
                tenant_id=user.tenant_id,
                timesheet_id=sheet.id,
                work_date=entry.work_date,
                project_id=entry.project_id,
                hours=entry.hours,
                description=(entry.description or "").strip() or None,
            )
        )
    sheet.status = TimesheetStatus.DRAFT
    await session.commit()
    return await _read(session, user, sheet, week_start=start, employee_id=user.employee_id)


async def submit_week(
    session: AsyncSession, user: User, week_of: date, *, request: Request | None = None
) -> dict[str, Any]:
    permissions.own_employee_id(user)
    start = _monday(week_of)
    sheet = await _sheet(session, user, user.employee_id, start)
    if sheet is None or not await _entries(session, user, sheet):
        raise _unprocessable("Add some hours before submitting the week.")
    if sheet.status not in (TimesheetStatus.DRAFT, TimesheetStatus.REJECTED):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already submitted.")
    sheet.status = TimesheetStatus.SUBMITTED
    sheet.submitted_at = datetime.now(UTC)
    sheet.decision_note = None
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="timesheet",
        entity_id=sheet.id,
        actor=user,
        changes={"status": "submitted"},
        summary=f"Submitted the timesheet for the week of {start}",
        request=request,
    )
    await session.commit()
    return await _read(session, user, sheet, week_start=start, employee_id=user.employee_id)


async def pending_for_approver(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    stmt = (
        select(Timesheet, Employee)
        .join(Employee, Employee.id == Timesheet.employee_id)
        .where(Timesheet.status == TimesheetStatus.SUBMITTED)
    )
    if not permissions.is_hr(user):
        line = await permissions.reporting_line(session, user)
        if not line:
            return []
        stmt = stmt.where(Timesheet.employee_id.in_(line))
    if user.employee_id is not None:
        stmt = stmt.where(Timesheet.employee_id != user.employee_id)
    rows = (
        await session.execute(tenant_scoped(stmt.order_by(Timesheet.week_start), Timesheet, user))
    ).all()
    out = []
    for sheet, employee in rows:
        read = await _read(
            session, user, sheet, week_start=sheet.week_start, employee_id=employee.id
        )
        out.append(
            {
                **read,
                "employee_name": employee.full_name,
                "employee_code": employee.employee_code,
                "submitted_at": sheet.submitted_at.isoformat() if sheet.submitted_at else None,
            }
        )
    return out


async def _rate_on(
    session: AsyncSession, user: User, employee_id: uuid.UUID, day: date
) -> Decimal | None:
    rate = await session.scalar(
        tenant_scoped(
            select(HourlyRate.rate)
            .where(HourlyRate.employee_id == employee_id, HourlyRate.effective_from <= day)
            .order_by(HourlyRate.effective_from.desc())
            .limit(1),
            HourlyRate,
            user,
        )
    )
    return Decimal(str(rate)) if rate is not None else None


async def decide(
    session: AsyncSession,
    user: User,
    timesheet_id: uuid.UUID,
    *,
    approve: bool,
    note: str | None,
    request: Request | None = None,
) -> dict[str, Any]:
    sheet = await session.scalar(
        tenant_scoped(select(Timesheet).where(Timesheet.id == timesheet_id), Timesheet, user)
    )
    if sheet is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Timesheet not found")
    if sheet.employee_id == user.employee_id:
        raise permissions.forbidden("Somebody else approves your timesheet.")
    if not (
        permissions.is_hr(user)
        or await permissions.is_line_manager_of(session, user, sheet.employee_id)
    ):
        raise permissions.forbidden("Only their manager or HR decides this timesheet.")
    if sheet.status is not TimesheetStatus.SUBMITTED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Not waiting for a decision."
        )
    clean = (note or "").strip() or None
    if not approve and clean is None:
        raise _unprocessable("Say what needs changing.")
    sheet.status = TimesheetStatus.APPROVED if approve else TimesheetStatus.REJECTED
    sheet.decided_by_user_id = user.id
    sheet.decided_at = datetime.now(UTC)
    sheet.decision_note = clean

    pay_note = None
    if approve:
        entries = await _entries(session, user, sheet)
        total = Decimal(0)
        priced = True
        for entry in entries:
            rate = await _rate_on(session, user, sheet.employee_id, entry.work_date)
            if rate is None:
                priced = False
                break
            total += Decimal(str(entry.hours)) * rate
        if priced and total > 0:
            amount = total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            week_end = sheet.week_start + timedelta(days=6)
            adjustment = PayrollAdjustment(
                tenant_id=user.tenant_id,
                employee_id=sheet.employee_id,
                kind=AdjustmentKind.EARNING,
                category=AdjustmentCategory.OTHER_EARNING,
                name=f"Hours, week of {sheet.week_start:%d %b %Y}",
                amount=amount,
                taxable=True,
                counts_as_wages=True,
                within_net_pay=False,
                recurrence=AdjustmentRecurrence.ONE_TIME,
                start_year=week_end.year,
                start_month=week_end.month,
                reference=f"timesheet:{sheet.id}",
                status=AdjustmentStatus.PENDING,
                reason=f"Approved timesheet, {sum(Decimal(str(e.hours)) for e in entries)} hours.",
                created_by_user_id=user.id,
            )
            session.add(adjustment)
            await session.flush()
            sheet.payroll_adjustment_id = adjustment.id
            pay_note = str(amount)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.APPROVE if approve else AuditAction.REJECT,
        entity_type="timesheet",
        entity_id=sheet.id,
        actor=user,
        changes={"status": sheet.status.value, "pay": pay_note, "note": clean},
        summary=(
            f"{'Approved' if approve else 'Sent back'} the timesheet for the week of "
            f"{sheet.week_start}"
        ),
        request=request,
    )
    await session.commit()
    read = await _read(
        session, user, sheet, week_start=sheet.week_start, employee_id=sheet.employee_id
    )
    return {**read, "pay": pay_note}


# ── Rates ───────────────────────────────────────────────────────────────────


async def add_rate(
    session: AsyncSession, user: User, payload: RateIn, *, request: Request | None
) -> dict[str, Any]:
    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == payload.employee_id), Employee, user)
    )
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
    taken = await session.scalar(
        tenant_scoped(
            select(HourlyRate.id).where(
                HourlyRate.employee_id == employee.id,
                HourlyRate.effective_from == payload.effective_from,
            ),
            HourlyRate,
            user,
        )
    )
    if taken is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A rate already starts that day. Rates are never edited; start the new one "
            "another day.",
        )
    row = HourlyRate(tenant_id=user.tenant_id, **payload.model_dump())
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="hourly_rate",
        entity_id=row.id,
        actor=user,
        changes={
            "employee": employee.employee_code,
            "rate": str(row.rate),
            "from": row.effective_from.isoformat(),
        },
        summary=f"Hourly rate {row.rate} for {employee.employee_code} from {row.effective_from}",
        request=request,
    )
    await session.commit()
    return {
        "id": str(row.id),
        "employee_id": str(employee.id),
        "rate": str(row.rate),
        "effective_from": row.effective_from.isoformat(),
        "reason": row.reason,
    }


async def list_rates(
    session: AsyncSession, user: User, employee_id: uuid.UUID
) -> list[dict[str, Any]]:
    rows = (
        await session.scalars(
            tenant_scoped(
                select(HourlyRate)
                .where(HourlyRate.employee_id == employee_id)
                .order_by(HourlyRate.effective_from.desc()),
                HourlyRate,
                user,
            )
        )
    ).all()
    return [
        {
            "id": str(r.id),
            "rate": str(r.rate),
            "effective_from": r.effective_from.isoformat(),
            "reason": r.reason,
        }
        for r in rows
    ]


# ── Reports ─────────────────────────────────────────────────────────────────


async def hours_report(
    session: AsyncSession, user: User, *, start: date, end: date
) -> dict[str, Any]:
    """Approved hours by project and person. HR sees all; a manager their line."""
    if end < start:
        raise _unprocessable("The end is before the start.")
    stmt = (
        select(
            Project.code,
            Project.name,
            Project.billable,
            Employee.employee_code,
            Employee.first_name,
            Employee.last_name,
            func.sum(TimesheetEntry.hours),
        )
        .select_from(TimesheetEntry)
        .join(Timesheet, Timesheet.id == TimesheetEntry.timesheet_id)
        .join(Employee, Employee.id == Timesheet.employee_id)
        .outerjoin(Project, Project.id == TimesheetEntry.project_id)
        .where(
            Timesheet.status == TimesheetStatus.APPROVED,
            TimesheetEntry.work_date >= start,
            TimesheetEntry.work_date <= end,
        )
        .group_by(
            Project.code,
            Project.name,
            Project.billable,
            Employee.employee_code,
            Employee.first_name,
            Employee.last_name,
        )
    )
    if not permissions.is_hr(user):
        line = await permissions.reporting_line(session, user)
        if not line:
            raise permissions.forbidden("Hours reports are for HR and managers.")
        stmt = stmt.where(Timesheet.employee_id.in_(line))
    rows = (await session.execute(tenant_scoped(stmt, TimesheetEntry, user))).all()
    by_project: dict[str, dict[str, Any]] = {}
    for code, name, billable, emp_code, first, last, hours in rows:
        key = code or "—"
        project = by_project.setdefault(
            key,
            {
                "code": code,
                "name": name or "No project",
                "billable": bool(billable),
                "hours": Decimal(0),
                "people": [],
            },
        )
        project["hours"] += Decimal(str(hours))
        project["people"].append(
            {
                "employee_code": emp_code,
                "name": " ".join(p for p in (first, last) if p),
                "hours": str(hours),
            }
        )
    projects = sorted(by_project.values(), key=lambda p: p["hours"], reverse=True)
    for p in projects:
        p["hours"] = str(p["hours"])
    return {"from": start.isoformat(), "to": end.isoformat(), "projects": projects}
