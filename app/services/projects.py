"""Turning departments that stood in for projects into projects.

A staffing company's people are employed by its legal entity and work on
clients' projects (or "Internal"). Workspaces that grouped them by client under
departments ("Internal", "External") convert those departments here: each
becomes a project of the same name, everybody in it goes onto that project, and
the department is removed — departments are for functions like HR, IT and
Finance.

All of it is one transaction, admin-only, with an audit row per change. A
current employee's move is filed in their history as a project change dated
today. People who have left move too (the department was where the project was
recorded), but only in the audit log: their history ends when they left. The
department comes off their records; a department something else still names
(a position, a budget, an approval rule) is closed instead of deleted, and the
answer says why.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

from fastapi import HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee, EmploymentStatus
from app.models.org import Department, LegalEntity
from app.models.timesheets import Project
from app.models.user import User
from app.services import audit
from app.services.employee_import.fields import norm

__all__ = ["FromDepartmentsIn", "from_departments"]


class FromDepartmentsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    department_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)


def _key(value: Any) -> str:
    return norm(value) if value is not None else ""


def _unique_code(name: str, taken: set[str]) -> str:
    base = re.sub(r"[^A-Z0-9]+", "-", name.upper()).strip("-")[:28] or "PROJECT"
    code, n = base, 2
    while code in taken:
        code = f"{base[: 28 - len(str(n))]}-{n}"
        n += 1
    taken.add(code)
    return code


async def _only_entity(session: AsyncSession, user: User) -> uuid.UUID | None:
    ids = (
        await session.scalars(tenant_scoped(select(LegalEntity.id).limit(2), LegalEntity, user))
    ).all()
    return ids[0] if len(ids) == 1 else None


async def _new_project(
    session: AsyncSession,
    user: User,
    *,
    department: Department,
    code: str,
    legal_entity_id: uuid.UUID | None,
    request: Request | None,
) -> Project:
    project = Project(
        tenant_id=user.tenant_id,
        name=department.name[:160],
        code=code,
        # The company's own staff are not a client's billable work.
        billable=_key(department.name) != "internal",
        legal_entity_id=legal_entity_id,
    )
    session.add(project)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="project",
        entity_id=project.id,
        actor=user,
        changes={
            "name": project.name,
            "code": project.code,
            "source": f"department {department.code}",
        },
        summary=f"Created project {project.code} from department {department.code}",
        request=request,
    )
    return project


@dataclass
class _Mover:
    session: AsyncSession
    user: User
    today: date
    last_days: dict[uuid.UUID, date]
    request: Request | None
    moved: int = 0
    leavers: int = 0
    unchanged: int = 0

    def has_left(self, employee: Employee) -> bool:
        left_on = employee.date_of_exit or self.last_days.get(employee.id)
        return (
            employee.deleted_at is not None
            or employee.status is EmploymentStatus.EXITED
            or (left_on is not None and left_on < self.today)
        )

    async def move(self, employee: Employee, project: Project) -> None:
        from app.services.lifecycle import service as lifecycle

        changes: dict[str, Any] = {"department_id": None}
        if employee.project_id != project.id:
            changes["project_id"] = project.id
        left = self.has_left(employee)
        before = {key: str(getattr(employee, key)) for key in changes}
        if not left:
            lifecycle.record_direct_edit(self.session, self.user, employee, changes, on=self.today)
        for key, value in changes.items():
            setattr(employee, key, value)
        await audit.record(
            self.session,
            tenant_id=self.user.tenant_id,
            action=AuditAction.UPDATE,
            entity_type="employee",
            entity_id=employee.id,
            actor=self.user,
            changes={
                key: {"from": before[key], "to": str(value)} for key, value in changes.items()
            },
            summary=f"Moved {employee.employee_code} to project {project.code} "
            "(department became a project)",
            request=self.request,
        )
        if "project_id" not in changes:
            self.unchanged += 1
        elif left:
            self.leavers += 1
        else:
            self.moved += 1


async def from_departments(
    session: AsyncSession, user: User, payload: FromDepartmentsIn, *, request: Request | None
) -> dict[str, Any]:
    """Each chosen department becomes a project with its people on it; the department goes."""
    from app.services import org
    from app.services.attendance.service import local_today
    from app.services.exit_workflow.last_days import agreed_last_days

    departments = (
        await session.scalars(
            tenant_scoped(
                select(Department).where(Department.id.in_(payload.department_ids)),
                Department,
                user,
            )
        )
    ).all()
    if len(departments) != len(set(payload.department_ids)):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="One of those departments is not part of this workspace.",
        )
    existing = list((await session.scalars(tenant_scoped(select(Project), Project, user))).all())
    by_key = {_key(p.code): p for p in existing} | {_key(p.name): p for p in existing}
    taken = {p.code.upper() for p in existing}
    entity = await _only_entity(session, user)

    people = list(
        (
            await session.scalars(
                tenant_scoped(
                    select(Employee)
                    .where(Employee.department_id.in_([d.id for d in departments]))
                    .order_by(Employee.employee_code),
                    Employee,
                    user,
                )
            )
        ).all()
    )
    mover = _Mover(
        session=session,
        user=user,
        today=await local_today(session, user),
        last_days=await agreed_last_days(session, user, {person.id for person in people}),
        request=request,
    )
    created: list[str] = []
    reused: list[str] = []
    for department in sorted(departments, key=lambda d: d.name.lower()):
        project = by_key.get(_key(department.code)) or by_key.get(_key(department.name))
        if project is None:
            code = department.code.upper()
            code = _unique_code(department.name, taken) if code in taken else code
            taken.add(code)
            project = await _new_project(
                session, user, department=department, code=code, legal_entity_id=entity,
                request=request,
            )
            by_key[_key(project.code)] = project
            created.append(project.name)
        else:
            reused.append(project.name)
        for person in people:
            if person.department_id == department.id:
                await mover.move(person, project)
    await session.flush()

    deleted: list[str] = []
    closed: list[dict[str, Any]] = []
    for department in sorted(departments, key=lambda d: d.name.lower()):
        reasons = await org.department_blockers(session, user, department)
        if reasons:
            if department.is_active:
                department.is_active = False
                await audit.record(
                    session,
                    tenant_id=user.tenant_id,
                    action=AuditAction.UPDATE,
                    entity_type="department",
                    entity_id=department.id,
                    actor=user,
                    changes={"is_active": {"from": True, "to": False}, "kept_for": reasons},
                    summary=f"Closed department {department.code}: it became a project",
                    request=request,
                )
            closed.append({"name": department.name, "reasons": reasons})
        else:
            await org.remove_department(session, user, department, request=request)
            deleted.append(department.name)

    await session.commit()
    return {
        "projects_created": created,
        "projects_reused": reused,
        "moved": mover.moved,
        "leavers_moved": mover.leavers,
        "unchanged": mover.unchanged,
        "departments_deleted": deleted,
        "departments_closed": closed,
    }
