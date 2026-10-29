"""Projects, tasks, weekly timesheets, approval, reminders, reports and hourly pay.

- A person fills a week (Monday to Sunday) with entries — a date, a project
  (or none) and, where the project has tasks, a task, hours, a note — and
  submits it. A submitted week is not edited; a rejected one goes back to draft.
- Submitting tells the approvers (the line manager, else HR); deciding tells
  the person. A daily job reminds people of weeks they saved but never
  submitted, and approvers of weeks waiting on them more than two days.
- The line manager, or HR, approves or rejects it. Nobody decides their own.
- On approval, if the person has an hourly rate, the week's pay is priced
  entry by entry at the rate in force that day — the person's rate for that
  project where one is set, else their general rate — and becomes a one-time
  payroll adjustment for the month the week ends in, pending an admin's
  approval.
- Reports total approved hours by project, task and person for a date range.
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
from app.models.employee import Employee, EmploymentStatus
from app.models.org import LegalEntity
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
    ProjectTask,
    Timesheet,
    TimesheetEntry,
    TimesheetStatus,
)
from app.models.user import User
from app.services import audit
from app.services.approvals.email_actions import ApprovalRef
from app.services.notifications.dispatch import notify, register_event

__all__ = [
    "EntryIn",
    "ProjectIn",
    "RateIn",
    "TaskIn",
    "add_rate",
    "create_project",
    "create_task",
    "decide",
    "hours_report",
    "list_projects",
    "list_rates",
    "my_week",
    "pending_for_approver",
    "save_week",
    "send_timesheet_reminders",
    "submit_week",
    "update_project",
    "update_task",
]

#: A submitted week waiting longer than this is worth reminding its approver of.
PENDING_AFTER = timedelta(hours=48)

register_event(
    "timesheets.submitted",
    "{employee_name} submitted a timesheet",
    "{employee_name} submitted {hours} hours for the week of {week}. Decide it under "
    "Time, Timesheet approvals.",
)
register_event(
    "timesheets.decided",
    "Your timesheet for the week of {week} was {outcome}",
    "Your timesheet for the week of {week} was {outcome}.{note_line}",
)
register_event(
    "timesheets.reminder.unsubmitted",
    "Your timesheet for the week of {week} is not submitted",
    "You saved {hours} hours for the week of {week} but have not submitted them. Nobody "
    "can approve the week until you do.",
)
register_event(
    "timesheets.reminder.pending",
    "{count} timesheet(s) waiting on you",
    "{count} timesheet(s) from your team have waited more than two days, the oldest for the "
    "week of {oldest}. Decide them under Time, Timesheet approvals.",
)


class ProjectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_\-]+$")
    name: str = Field(min_length=2, max_length=160)
    client: str | None = Field(default=None, max_length=160)
    billable: bool = True
    #: The company of the group that staffs it.
    legal_entity_id: uuid.UUID | None = None


class TaskIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=160)


class EntryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    work_date: date
    project_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    hours: Decimal = Field(gt=0, le=24, max_digits=5, decimal_places=2)
    description: str | None = Field(default=None, max_length=500)


class RateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    employee_id: uuid.UUID
    #: Null for the person's general rate; set for their rate on one project.
    project_id: uuid.UUID | None = None
    rate: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    effective_from: date
    reason: str | None = Field(default=None, max_length=500)


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


# ── Projects ────────────────────────────────────────────────────────────────


def _task_read(t: ProjectTask) -> dict[str, Any]:
    return {"id": str(t.id), "project_id": str(t.project_id), "name": t.name, "active": t.active}


def _project_read(
    p: Project, tasks: list[ProjectTask] | None = None, headcount: int | None = None
) -> dict[str, Any]:
    return {
        "id": str(p.id),
        "code": p.code,
        "name": p.name,
        "client": p.client,
        "billable": p.billable,
        "active": p.active,
        "legal_entity_id": str(p.legal_entity_id) if p.legal_entity_id else None,
        "tasks": [_task_read(t) for t in tasks or []],
        # People on it who have not left; HR's list only.
        "headcount": headcount,
    }


async def project_headcounts(session: AsyncSession, user: User) -> dict[uuid.UUID, int]:
    """People on each project who have not left."""
    rows = await session.execute(
        tenant_scoped(
            select(Employee.project_id, func.count(Employee.id))
            .where(
                Employee.project_id.is_not(None),
                Employee.deleted_at.is_(None),
                Employee.status != EmploymentStatus.EXITED,
            )
            .group_by(Employee.project_id),
            Employee,
            user,
        )
    )
    return {project_id: count for project_id, count in rows.all()}


async def _check_entity(session: AsyncSession, user: User, entity_id: uuid.UUID | None) -> None:
    if entity_id is None:
        return
    found = await session.scalar(
        tenant_scoped(select(LegalEntity.id).where(LegalEntity.id == entity_id), LegalEntity, user)
    )
    if found is None:
        raise _unprocessable("That company is not part of this workspace.")


async def _tasks_by_project(
    session: AsyncSession, user: User, project_ids: set[uuid.UUID], *, include_inactive: bool
) -> dict[uuid.UUID, list[ProjectTask]]:
    if not project_ids:
        return {}
    stmt = select(ProjectTask).where(ProjectTask.project_id.in_(project_ids))
    if not include_inactive:
        stmt = stmt.where(ProjectTask.active.is_(True))
    out: dict[uuid.UUID, list[ProjectTask]] = {}
    for task in (
        await session.scalars(tenant_scoped(stmt.order_by(ProjectTask.name), ProjectTask, user))
    ).all():
        out.setdefault(task.project_id, []).append(task)
    return out


async def list_projects(
    session: AsyncSession, user: User, *, include_inactive: bool = False
) -> list[dict[str, Any]]:
    everything = include_inactive and permissions.is_hr(user)
    stmt = select(Project).order_by(Project.code)
    if not everything:
        stmt = stmt.where(Project.active.is_(True))
    projects = (await session.scalars(tenant_scoped(stmt, Project, user))).all()
    tasks = await _tasks_by_project(
        session, user, {p.id for p in projects}, include_inactive=everything
    )
    counts = await project_headcounts(session, user) if permissions.is_hr(user) else None
    return [
        _project_read(p, tasks.get(p.id), counts.get(p.id, 0) if counts is not None else None)
        for p in projects
    ]


async def _project(session: AsyncSession, user: User, project_id: uuid.UUID) -> Project:
    project = await session.scalar(
        tenant_scoped(select(Project).where(Project.id == project_id), Project, user)
    )
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


async def create_task(
    session: AsyncSession,
    user: User,
    project_id: uuid.UUID,
    payload: TaskIn,
    *,
    request: Request | None,
) -> dict[str, Any]:
    project = await _project(session, user, project_id)
    name = payload.name.strip()
    taken = await session.scalar(
        tenant_scoped(
            select(ProjectTask.id).where(
                ProjectTask.project_id == project.id, func.lower(ProjectTask.name) == name.lower()
            ),
            ProjectTask,
            user,
        )
    )
    if taken is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"{project.code} already has '{name}'."
        )
    task = ProjectTask(tenant_id=user.tenant_id, project_id=project.id, name=name)
    session.add(task)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="project_task",
        entity_id=task.id,
        actor=user,
        changes={"project": project.code, "name": name},
        summary=f"Added the task '{name}' to {project.code}",
        request=request,
    )
    await session.commit()
    return _task_read(task)


async def update_task(
    session: AsyncSession,
    user: User,
    task_id: uuid.UUID,
    *,
    name: str | None,
    active: bool | None,
    request: Request | None,
) -> dict[str, Any]:
    task = await session.scalar(
        tenant_scoped(select(ProjectTask).where(ProjectTask.id == task_id), ProjectTask, user)
    )
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    changes: dict[str, Any] = {}
    if name is not None and name.strip() != task.name:
        changes["name"] = [task.name, name.strip()]
        task.name = name.strip()
    if active is not None and active != task.active:
        changes["active"] = [task.active, active]
        task.active = active
    if changes:
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.UPDATE,
            entity_type="project_task",
            entity_id=task.id,
            actor=user,
            changes=changes,
            summary=f"Updated the task '{task.name}'",
            request=request,
        )
        await session.commit()
    return _task_read(task)


async def create_project(
    session: AsyncSession, user: User, payload: ProjectIn, *, request: Request | None
) -> dict[str, Any]:
    taken = await session.scalar(
        tenant_scoped(select(Project.id).where(Project.code == payload.code), Project, user)
    )
    if taken is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="That code is in use.")
    await _check_entity(session, user, payload.legal_entity_id)
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
        changes={**_project_read(row), "tasks": []},
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
    legal_entity_id: uuid.UUID | None = None,
    clear_legal_entity: bool = False,
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
    if legal_entity_id is not None:
        await _check_entity(session, user, legal_entity_id)
        row.legal_entity_id = legal_entity_id
    elif clear_legal_entity:
        row.legal_entity_id = None
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


def _project_references() -> list[tuple[type, Any]]:
    """Every (model, column) beyond employees and the project's own tasks naming a project."""
    from app.models.base import Base

    found: list[tuple[type, Any]] = []
    for mapper in Base.registry.mappers:
        table = mapper.local_table
        if table.name in ("employees", "project_tasks", "projects"):
            continue
        for column in table.columns:
            if any(
                fk.column.table.name == "projects" and fk.column.name == "id"
                for fk in column.foreign_keys
            ):
                found.append((mapper.class_, column))
    return found


_REFERENCE_LABELS = {
    "payslips": "payslips",
    "timesheet_entries": "timesheet entries",
    "hourly_rates": "hourly rates",
}


async def delete_project(
    session: AsyncSession, user: User, project_id: uuid.UUID, *, request: Request | None
) -> None:
    """Delete a project nobody has been on and nothing has been booked or paid against.

    Anything that would lose its project — a person (current or left), a
    payslip, a timesheet row, an hourly rate — refuses it with what uses it;
    closing the project is the way to retire one with a history.
    """
    row = await _project(session, user, project_id)
    reasons: list[str] = []
    current, left = 0, 0
    for status_value, count in (
        await session.execute(
            tenant_scoped(
                select(Employee.status, func.count(Employee.id))
                .where(Employee.project_id == row.id, Employee.deleted_at.is_(None))
                .group_by(Employee.status),
                Employee,
                user,
            )
        )
    ).all():
        if status_value is EmploymentStatus.EXITED:
            left += count
        else:
            current += count
    if current:
        reasons.append(
            f"{current} {'person works' if current == 1 else 'people work'} on it — move "
            "them to another project first"
        )
    if left:
        who = "person who has left was" if left == 1 else "people who have left were"
        reasons.append(f"{left} {who} on it")
    for model, column in _project_references():
        stmt = select(func.count()).select_from(model).where(column == row.id)
        count = await session.scalar(
            tenant_scoped(stmt, model, user) if hasattr(model, "tenant_id") else stmt
        )
        if count:
            name = model.__tablename__
            label = _REFERENCE_LABELS.get(name, name.replace("_", " "))
            reasons.append(f"{count} {label} name it")
    if reasons:
        tail = (
            ""
            if current and len(reasons) == 1
            else " Close it instead: it stays on those records but is no longer offered."
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{row.name} cannot be deleted: " + "; ".join(reasons) + "." + tail,
        )
    snapshot = _project_read(row)
    await session.delete(row)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.DELETE,
        entity_type="project",
        entity_id=project_id,
        actor=user,
        changes=snapshot,
        summary=f"Deleted the project {snapshot['code']}",
        request=request,
    )
    await session.commit()


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
    task_names = (
        {
            t.id: t.name
            for t in (
                await session.scalars(
                    tenant_scoped(
                        select(ProjectTask).where(
                            ProjectTask.id.in_({e.task_id for e in entries if e.task_id})
                        ),
                        ProjectTask,
                        user,
                    )
                )
            ).all()
        }
        if any(e.task_id for e in entries)
        else {}
    )
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
        "approval_chain": list(sheet.approval_chain or []) if sheet else [],
        "total_hours": str(total),
        "entries": [
            {
                "id": str(e.id),
                "work_date": e.work_date.isoformat(),
                "project_id": str(e.project_id) if e.project_id else None,
                "project": projects[e.project_id].name if e.project_id in projects else None,
                "task_id": str(e.task_id) if e.task_id else None,
                "task": task_names.get(e.task_id) if e.task_id else None,
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
    tasks = await _tasks_by_project(session, user, project_ids, include_inactive=False)
    for entry in entries:
        offered = tasks.get(entry.project_id, []) if entry.project_id else []
        if entry.task_id is not None:
            if entry.project_id is None or entry.task_id not in {t.id for t in offered}:
                raise _unprocessable(
                    f"The task on {entry.work_date} is not an open task of that entry's project."
                )
        elif offered:
            raise _unprocessable(
                f"Choose a task for the hours on {entry.work_date}: that project is split "
                "into tasks."
            )

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
                task_id=entry.task_id,
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
    from app.models.approval_rules import ApprovalProcess
    from app.services.approvals import rules

    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == user.employee_id), Employee, user)
    )
    sheet.status = TimesheetStatus.SUBMITTED
    sheet.submitted_at = datetime.now(UTC)
    sheet.decision_note = None
    if employee is not None:
        sheet.approval_chain = await rules.attach(
            session,
            user,
            employee,
            rules.Facts(
                process=ApprovalProcess.TIMESHEET,
                department_id=employee.department_id,
                location_id=employee.location_id,
            ),
        )
    approvers = [
        uid for uid in await _approvers(session, user, user.employee_id, sheet) if uid != user.id
    ]
    if approvers:
        hours = sum(
            (Decimal(str(e.hours)) for e in await _entries(session, user, sheet)), Decimal(0)
        )
        await notify(
            session,
            tenant_id=user.tenant_id,
            event_key="timesheets.submitted",
            recipient_user_ids=approvers,
            payload={
                "employee_name": employee.full_name if employee else "A colleague",
                "hours": str(hours),
                "week": f"{start:%d %b %Y}",
                "timesheet_id": str(sheet.id),
            },
            commit=False,
            approval=ApprovalRef("timesheet", sheet.id),
        )
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
    """Weeks waiting on the caller: the chain's current step is theirs (or someone
    they stand in for); older weeks from their reporting line; everything for HR."""
    from app.services.approvals import rules

    stmt = (
        select(Timesheet, Employee)
        .join(Employee, Employee.id == Timesheet.employee_id)
        .where(Timesheet.status == TimesheetStatus.SUBMITTED)
    )
    if user.employee_id is not None:
        stmt = stmt.where(Timesheet.employee_id != user.employee_id)
    rows = (
        await session.execute(tenant_scoped(stmt.order_by(Timesheet.week_start), Timesheet, user))
    ).all()
    if not permissions.is_hr(user):
        line = set(await permissions.reporting_line(session, user))
        acting = {str(a) for a in await rules.acting_for(session, user)}
        rows = [
            (sheet, employee)
            for sheet, employee in rows
            if (sheet.approval_chain and rules.waiting_on(sheet.approval_chain, acting))
            or (not sheet.approval_chain and sheet.employee_id in line)
        ]
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


async def _approvers(
    session: AsyncSession, user: User, employee_id: uuid.UUID, sheet: Timesheet | None = None
) -> list[uuid.UUID]:
    """Who decides this week now: its chain's current approver (or HR for an HR
    step); for a week from before chains, the line manager's login, else HR."""
    from app.services.approvals import rules
    from app.services.attendance.service import _approver_user_ids

    if sheet is not None and sheet.approval_chain:
        return await rules.next_recipients(session, user, sheet.approval_chain)
    return list(await _approver_user_ids(session, user, employee_id))


async def _rate_on(
    session: AsyncSession,
    user: User,
    employee_id: uuid.UUID,
    day: date,
    project_id: uuid.UUID | None = None,
) -> Decimal | None:
    """The rate in force that day: the project's own rate for the person, else general."""

    async def latest(project: uuid.UUID | None) -> Decimal | None:
        stmt = select(HourlyRate.rate).where(
            HourlyRate.employee_id == employee_id,
            HourlyRate.effective_from <= day,
            HourlyRate.project_id.is_(None)
            if project is None
            else HourlyRate.project_id == project,
        )
        rate = await session.scalar(
            tenant_scoped(
                stmt.order_by(HourlyRate.effective_from.desc()).limit(1), HourlyRate, user
            )
        )
        return Decimal(str(rate)) if rate is not None else None

    if project_id is not None:
        own = await latest(project_id)
        if own is not None:
            return own
    return await latest(None)


async def decide(
    session: AsyncSession,
    user: User,
    timesheet_id: uuid.UUID,
    *,
    approve: bool,
    note: str | None,
    request: Request | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    sheet = await session.scalar(
        tenant_scoped(select(Timesheet).where(Timesheet.id == timesheet_id), Timesheet, user)
    )
    if sheet is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Timesheet not found")
    from app.services.approvals import rules

    if sheet.employee_id == user.employee_id:
        raise permissions.forbidden("Somebody else approves your timesheet.")
    chain = list(sheet.approval_chain or [])
    if not chain and not (
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
    if chain:
        chain, outcome = await rules.decide(session, user, chain, approve=approve, note=clean)
        sheet.approval_chain = chain
        if outcome == "advanced":
            following = [
                uid for uid in await rules.next_recipients(session, user, chain) if uid != user.id
            ]
            employee = await session.scalar(
                tenant_scoped(
                    select(Employee).where(Employee.id == sheet.employee_id), Employee, user
                )
            )
            if following:
                logged = await _entries(session, user, sheet)
                await notify(
                    session,
                    tenant_id=user.tenant_id,
                    event_key="timesheets.submitted",
                    recipient_user_ids=following,
                    payload={
                        "employee_name": employee.full_name if employee else "A colleague",
                        "hours": str(sum((Decimal(str(e.hours)) for e in logged), Decimal(0))),
                        "week": f"{sheet.week_start:%d %b %Y}",
                        "timesheet_id": str(sheet.id),
                    },
                    commit=False,
                    approval=ApprovalRef("timesheet", sheet.id),
                )
            await audit.record(
                session,
                tenant_id=user.tenant_id,
                action=AuditAction.APPROVE,
                entity_type="timesheet",
                entity_id=sheet.id,
                actor=user,
                changes={"step": "approved", "note": clean},
                summary=(
                    f"Approved a step of the timesheet for the week of {sheet.week_start}; "
                    "passed to the next approver"
                ),
                request=request,
            )
            await (session.commit() if commit else session.flush())
            read = await _read(
                session, user, sheet, week_start=sheet.week_start, employee_id=sheet.employee_id
            )
            return {**read, "pay": None}
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
            rate = await _rate_on(
                session, user, sheet.employee_id, entry.work_date, entry.project_id
            )
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
    recipients = list(
        (
            await session.scalars(
                tenant_scoped(
                    select(User.id).where(
                        User.employee_id == sheet.employee_id, User.is_active.is_(True)
                    ),
                    User,
                    user,
                )
            )
        ).all()
    )
    if recipients:
        await notify(
            session,
            tenant_id=user.tenant_id,
            event_key="timesheets.decided",
            recipient_user_ids=recipients,
            payload={
                "week": f"{sheet.week_start:%d %b %Y}",
                "outcome": "approved" if approve else "sent back",
                "note_line": f" Note: {clean}" if clean else "",
                "timesheet_id": str(sheet.id),
            },
            commit=False,
        )
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
    await (session.commit() if commit else session.flush())
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
    project = await _project(session, user, payload.project_id) if payload.project_id else None
    taken = await session.scalar(
        tenant_scoped(
            select(HourlyRate.id).where(
                HourlyRate.employee_id == employee.id,
                HourlyRate.effective_from == payload.effective_from,
                HourlyRate.project_id.is_(None)
                if project is None
                else HourlyRate.project_id == project.id,
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
            "project": project.code if project else None,
            "rate": str(row.rate),
            "from": row.effective_from.isoformat(),
        },
        summary=(
            f"Hourly rate {row.rate} for {employee.employee_code}"
            + (f" on {project.code}" if project else "")
            + f" from {row.effective_from}"
        ),
        request=request,
    )
    await session.commit()
    return {
        "id": str(row.id),
        "employee_id": str(employee.id),
        "project_id": str(project.id) if project else None,
        "project": project.name if project else None,
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
    names = (
        {
            p.id: p.name
            for p in (
                await session.scalars(
                    tenant_scoped(
                        select(Project).where(Project.id.in_({r.project_id for r in rows})),
                        Project,
                        user,
                    )
                )
            ).all()
        }
        if any(r.project_id for r in rows)
        else {}
    )
    return [
        {
            "id": str(r.id),
            "project_id": str(r.project_id) if r.project_id else None,
            "project": names.get(r.project_id) if r.project_id else None,
            "rate": str(r.rate),
            "effective_from": r.effective_from.isoformat(),
            "reason": r.reason,
        }
        for r in rows
    ]


# ── Reminders ───────────────────────────────────────────────────────────────


async def send_timesheet_reminders(
    session: AsyncSession, actor: User, *, today: date, now: datetime | None = None
) -> dict[str, int]:
    """Scheduler-callable: weeks saved but never submitted, and weeks waiting on approvers.

    A week is "unsubmitted" once it has ended and still sits in draft (or was
    sent back) with hours on it. A submitted week is "waiting" after two days.
    """
    moment = now or datetime.now(UTC)
    this_monday = _monday(today)
    drafts = (
        await session.execute(
            tenant_scoped(
                select(Timesheet, func.coalesce(func.sum(TimesheetEntry.hours), 0))
                .join(TimesheetEntry, TimesheetEntry.timesheet_id == Timesheet.id)
                .where(
                    Timesheet.status.in_([TimesheetStatus.DRAFT, TimesheetStatus.REJECTED]),
                    Timesheet.week_start < this_monday,
                )
                .group_by(Timesheet.id),
                Timesheet,
                actor,
            )
        )
    ).all()
    people_reminded = 0
    for sheet, hours in drafts:
        recipients = list(
            (
                await session.scalars(
                    tenant_scoped(
                        select(User.id).where(
                            User.employee_id == sheet.employee_id, User.is_active.is_(True)
                        ),
                        User,
                        actor,
                    )
                )
            ).all()
        )
        if not recipients:
            continue
        await notify(
            session,
            tenant_id=actor.tenant_id,
            event_key="timesheets.reminder.unsubmitted",
            recipient_user_ids=recipients,
            payload={"week": f"{sheet.week_start:%d %b %Y}", "hours": str(hours)},
            commit=False,
        )
        people_reminded += 1

    waiting = (
        await session.scalars(
            tenant_scoped(
                select(Timesheet).where(
                    Timesheet.status == TimesheetStatus.SUBMITTED,
                    Timesheet.submitted_at <= moment - PENDING_AFTER,
                ),
                Timesheet,
                actor,
            )
        )
    ).all()
    per_approver: dict[uuid.UUID, list[Timesheet]] = {}
    for sheet in waiting:
        for approver in await _approvers(session, actor, sheet.employee_id, sheet):
            per_approver.setdefault(approver, []).append(sheet)
    for approver, sheets in per_approver.items():
        oldest = min(s.week_start for s in sheets)
        await notify(
            session,
            tenant_id=actor.tenant_id,
            event_key="timesheets.reminder.pending",
            recipient_user_ids=[approver],
            payload={"count": len(sheets), "oldest": f"{oldest:%d %b %Y}"},
            commit=False,
        )
    await session.commit()
    return {"people_reminded": people_reminded, "approvers_reminded": len(per_approver)}


# ── Reports ─────────────────────────────────────────────────────────────────


async def hours_report(
    session: AsyncSession, user: User, *, start: date, end: date
) -> dict[str, Any]:
    """Approved hours by project, task and person. HR sees all; a manager their line."""
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
            ProjectTask.name,
            func.sum(TimesheetEntry.hours),
        )
        .select_from(TimesheetEntry)
        .join(Timesheet, Timesheet.id == TimesheetEntry.timesheet_id)
        .join(Employee, Employee.id == Timesheet.employee_id)
        .outerjoin(Project, Project.id == TimesheetEntry.project_id)
        .outerjoin(ProjectTask, ProjectTask.id == TimesheetEntry.task_id)
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
            ProjectTask.name,
        )
    )
    if not permissions.is_hr(user):
        line = await permissions.reporting_line(session, user)
        if not line:
            raise permissions.forbidden("Hours reports are for HR and managers.")
        stmt = stmt.where(Timesheet.employee_id.in_(line))
    rows = (await session.execute(tenant_scoped(stmt, TimesheetEntry, user))).all()
    by_project: dict[str, dict[str, Any]] = {}
    for code, name, billable, emp_code, first, last, task, hours in rows:
        key = code or "—"
        project = by_project.setdefault(
            key,
            {
                "code": code,
                "name": name or "No project",
                "billable": bool(billable),
                "hours": Decimal(0),
                "people": [],
                "tasks": {},
            },
        )
        project["hours"] += Decimal(str(hours))
        task_key = task or "No task"
        project["tasks"][task_key] = project["tasks"].get(task_key, Decimal(0)) + Decimal(
            str(hours)
        )
        project["people"].append(
            {
                "employee_code": emp_code,
                "name": " ".join(p for p in (first, last) if p),
                "task": task,
                "hours": str(hours),
            }
        )
    projects = sorted(by_project.values(), key=lambda p: p["hours"], reverse=True)
    for p in projects:
        p["hours"] = str(p["hours"])
        p["tasks"] = [
            {"name": name, "hours": str(value)}
            for name, value in sorted(p["tasks"].items(), key=lambda kv: kv[1], reverse=True)
        ]
    return {"from": start.isoformat(), "to": end.isoformat(), "projects": projects}
