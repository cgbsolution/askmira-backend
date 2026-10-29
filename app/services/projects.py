"""Putting people on projects in bulk: from departments, or from a sheet.

A staffing company's people are employed by its legal entity and work on
clients' projects (or "Internal"). Two ways to set that up for everybody at
once, both admin-only and each one transaction with an audit row per change:

- **From departments.** Workspaces that grouped people by client under
  departments ("Internal", "External") turn each chosen department into a
  project of the same name and put its people on it, optionally taking the
  department off their records.
- **From a sheet.** The old HRMS's salary register has an Emp Code and a
  PROJECT column; uploaded, every listed person goes onto that project, and a
  project the sheet names that is not here yet is created. A preview says what
  would happen before anything does.

A move is filed in the person's history as a project change dated today. Their
company, and so their payroll, never changes here. Their own record and people
who have left are not moved.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
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
from app.services.employee_import.sheet import SheetError, read_table

__all__ = [
    "FromDepartmentsIn",
    "assign_from_sheet",
    "from_departments",
]


class FromDepartmentsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    department_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)
    #: Take the department off their records once they are on the project.
    clear_department: bool = False


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
        await session.scalars(
            tenant_scoped(select(LegalEntity.id).limit(2), LegalEntity, user)
        )
    ).all()
    return ids[0] if len(ids) == 1 else None


async def _projects(session: AsyncSession, user: User) -> list[Project]:
    return list((await session.scalars(tenant_scoped(select(Project), Project, user))).all())


async def _new_project(
    session: AsyncSession,
    user: User,
    *,
    name: str,
    code: str,
    legal_entity_id: uuid.UUID | None,
    source: str,
    request: Request | None,
) -> Project:
    project = Project(
        tenant_id=user.tenant_id,
        name=name[:160],
        code=code,
        # The company's own staff are not a client's billable work.
        billable=_key(name) != "internal",
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
        changes={"name": project.name, "code": project.code, "source": source},
        summary=f"Created project {project.code} ({source})",
        request=request,
    )
    return project


@dataclass
class _Mover:
    """Moves people onto projects, the way the directory's bulk move does."""

    session: AsyncSession
    user: User
    today: date
    last_days: dict[uuid.UUID, date]
    request: Request | None
    moved: int = 0
    unchanged: int = 0
    skipped: list[dict[str, Any]] = field(default_factory=list)

    def blocked(self, employee: Employee) -> str | None:
        left_on = employee.date_of_exit or self.last_days.get(employee.id)
        if employee.status is EmploymentStatus.EXITED or (
            left_on is not None and left_on < self.today
        ):
            return "Has left: the project they left on stays on their record."
        if self.user.employee_id is not None and employee.id == self.user.employee_id:
            return "Your own record is changed by somebody else in HR."
        return None

    async def move(
        self, employee: Employee, project: Project, *, clear_department: bool = False
    ) -> None:
        from app.services.lifecycle import service as lifecycle

        reason = self.blocked(employee)
        if reason:
            self.skipped.append(
                {"employee_code": employee.employee_code, "name": employee.full_name,
                 "reason": reason}
            )
            return
        changes: dict[str, Any] = {}
        if employee.project_id != project.id:
            changes["project_id"] = project.id
        if clear_department and employee.department_id is not None:
            changes["department_id"] = None
        if not changes:
            self.unchanged += 1
            return
        before = {key: str(getattr(employee, key)) for key in changes}
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
            summary=f"Moved {employee.employee_code} to project {project.code}",
            request=self.request,
        )
        self.moved += 1


async def _mover(
    session: AsyncSession, user: User, people: list[Employee], request: Request | None
) -> _Mover:
    from app.services.attendance.service import local_today
    from app.services.exit_workflow.last_days import agreed_last_days

    return _Mover(
        session=session,
        user=user,
        today=await local_today(session, user),
        last_days=await agreed_last_days(session, user, {person.id for person in people}),
        request=request,
    )


# ── From departments ────────────────────────────────────────────────────────


async def from_departments(
    session: AsyncSession, user: User, payload: FromDepartmentsIn, *, request: Request | None
) -> dict[str, Any]:
    """Each chosen department becomes a project of the same name, with its people on it."""
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
    existing = await _projects(session, user)
    by_key = {_key(p.code): p for p in existing} | {_key(p.name): p for p in existing}
    taken = {p.code.upper() for p in existing}
    entity = await _only_entity(session, user)

    people = list(
        (
            await session.scalars(
                tenant_scoped(
                    select(Employee)
                    .where(
                        Employee.department_id.in_([d.id for d in departments]),
                        Employee.deleted_at.is_(None),
                    )
                    .order_by(Employee.employee_code),
                    Employee,
                    user,
                )
            )
        ).all()
    )
    mover = await _mover(session, user, people, request)
    created: list[str] = []
    reused: list[str] = []
    for department in sorted(departments, key=lambda d: d.name.lower()):
        project = by_key.get(_key(department.code)) or by_key.get(_key(department.name))
        if project is None:
            code = department.code.upper() if department.code.upper() not in taken else (
                _unique_code(department.name, taken)
            )
            taken.add(code)
            project = await _new_project(
                session,
                user,
                name=department.name,
                code=code,
                legal_entity_id=entity,
                source=f"from department {department.code}",
                request=request,
            )
            by_key[_key(project.code)] = project
            created.append(project.name)
        else:
            reused.append(project.name)
        for person in people:
            if person.department_id == department.id:
                await mover.move(person, project, clear_department=payload.clear_department)

    await session.commit()
    return {
        "projects_created": created,
        "projects_reused": reused,
        "moved": mover.moved,
        "unchanged": mover.unchanged,
        "skipped": mover.skipped,
    }


# ── From a sheet ────────────────────────────────────────────────────────────


async def assign_from_sheet(
    session: AsyncSession,
    user: User,
    data: bytes,
    filename: str,
    *,
    apply: bool,
    request: Request | None,
) -> dict[str, Any]:
    """Put everybody the sheet lists on its PROJECT; with `apply` false, only say what would."""
    try:
        table = read_table(data, filename)
    except SheetError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    code_col = table.mapping.get("employee_code")
    project_col = table.mapping.get("project")
    if code_col is None or project_col is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "The sheet needs an 'Emp Code' column and a 'Project' column (the old salary "
                "register's PROJECT works). Neither was found under the same heading row."
            ),
        )

    people = {
        person.employee_code.upper(): person
        for person in (
            await session.scalars(
                tenant_scoped(
                    select(Employee).where(Employee.deleted_at.is_(None)), Employee, user
                )
            )
        ).all()
    }
    existing = await _projects(session, user)
    by_key = {_key(p.code): p for p in existing} | {_key(p.name): p for p in existing}
    taken = {p.code.upper() for p in existing}
    entity = await _only_entity(session, user)
    mover = await _mover(session, user, list(people.values()), request)

    rows: list[dict[str, Any]] = []
    new_projects: dict[str, str] = {}
    seen: set[str] = set()
    for line, cells in table.rows:
        raw_code = cells[code_col] if code_col < len(cells) else None
        code = str(raw_code).strip() if raw_code not in (None, "") else ""
        raw_project = cells[project_col] if project_col < len(cells) else None
        name = (
            re.sub(r"\s+", " ", str(raw_project)).strip()
            if raw_project not in (None, "")
            else ""
        )
        if not code or code.lower().startswith(("grand total", "total")):
            continue
        out: dict[str, Any] = {"line": line, "employee_code": code, "name": None,
                               "project": name or None}
        person = people.get(code.upper())
        if code.upper() in seen:
            rows.append(
                {**out, "outcome": "duplicate", "note": "Listed twice: the first row is used."}
            )
            continue
        seen.add(code.upper())
        if person is None:
            rows.append({**out, "outcome": "unknown", "note": "No employee with this code here."})
            continue
        out["name"] = person.full_name
        if not name:
            rows.append({**out, "outcome": "blank", "note": "No project given: left as it is."})
            continue
        blocked = mover.blocked(person)
        if blocked:
            rows.append({**out, "outcome": "skipped", "note": blocked})
            continue
        project = by_key.get(_key(name))
        if project is None:
            new_projects.setdefault(_key(name), name)
            rows.append({**out, "outcome": "assign", "note": "New project: created on apply."})
        elif person.project_id == project.id:
            rows.append({**out, "outcome": "unchanged", "note": None})
        else:
            rows.append({**out, "outcome": "assign", "note": None})

    created: list[str] = []
    if apply:
        for key, name in sorted(new_projects.items()):
            project = await _new_project(
                session,
                user,
                name=name,
                code=_unique_code(name, taken),
                legal_entity_id=entity,
                source=f"from {filename}",
                request=request,
            )
            by_key[key] = project
            created.append(project.name)
        for row in rows:
            if row["outcome"] != "assign":
                continue
            await mover.move(people[row["employee_code"].upper()], by_key[_key(row["project"])])
        await session.commit()

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
    return {
        "filename": filename,
        "applied": apply,
        "header_row": table.header_row,
        "rows": rows,
        "counts": counts,
        "new_projects": sorted(new_projects.values(), key=str.lower),
        "projects_created": created,
        "moved": mover.moved if apply else 0,
    }
