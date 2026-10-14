"""Grades, pay bands, and where each person's pay sits in their band.

`positioning` reads every current employee's CTC in force today against their
grade's band: below the minimum, inside, or above the maximum, with the
compa-ratio (CTC ÷ midpoint). People with no grade, a grade not in the master,
or no salary are listed as such rather than left out — a gap in the data is
something to fix, not to hide.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee, EmploymentStatus
from app.models.pay_grades import PayGrade
from app.models.payroll import EmployeeSalary
from app.models.user import User
from app.schemas.pay_grades import PayGradeCreate, PayGradeUpdate
from app.services import audit

__all__ = [
    "check_grade_code",
    "create_grade",
    "list_grades",
    "positioning",
    "update_grade",
]


def _plain(row: PayGrade) -> dict[str, Any]:
    return {
        "code": row.code,
        "name": row.name,
        "level": row.level,
        "band_min": str(row.band_min),
        "band_mid": str(row.band_mid),
        "band_max": str(row.band_max),
        "active": row.active,
    }


async def list_grades(
    session: AsyncSession, user: User, *, include_inactive: bool = False
) -> list[tuple[PayGrade, int]]:
    stmt = select(PayGrade)
    if not include_inactive:
        stmt = stmt.where(PayGrade.active.is_(True))
    grades = list(
        (
            await session.scalars(
                tenant_scoped(stmt.order_by(PayGrade.level, PayGrade.code), PayGrade, user)
            )
        ).all()
    )
    counts = dict(
        (
            await session.execute(
                tenant_scoped(
                    select(Employee.grade, func.count())
                    .where(
                        Employee.deleted_at.is_(None),
                        Employee.status != EmploymentStatus.EXITED,
                        Employee.grade.is_not(None),
                    )
                    .group_by(Employee.grade),
                    Employee,
                    user,
                )
            )
        ).all()
    )
    return [(g, int(counts.get(g.code, 0))) for g in grades]


async def _grade(session: AsyncSession, user: User, grade_id: uuid.UUID) -> PayGrade:
    row = await session.scalar(
        tenant_scoped(select(PayGrade).where(PayGrade.id == grade_id), PayGrade, user)
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Grade not found")
    return row


async def create_grade(
    session: AsyncSession, user: User, payload: PayGradeCreate, *, request: Request | None
) -> PayGrade:
    taken = await session.scalar(
        tenant_scoped(select(PayGrade.id).where(PayGrade.code == payload.code), PayGrade, user)
    )
    if taken is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"Grade {payload.code} already exists."
        )
    row = PayGrade(tenant_id=user.tenant_id, **payload.model_dump())
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="pay_grade",
        entity_id=row.id,
        actor=user,
        changes={"after": _plain(row)},
        summary=f"Added grade {row.code} ({row.band_min} to {row.band_max})",
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def update_grade(
    session: AsyncSession,
    user: User,
    grade_id: uuid.UUID,
    payload: PayGradeUpdate,
    *,
    request: Request | None,
) -> PayGrade:
    row = await _grade(session, user, grade_id)
    before = _plain(row)
    changes = payload.model_dump(exclude_unset=True)
    for key in ("name", "level", "band_min", "band_mid", "band_max", "active"):
        if key in changes and changes[key] is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"'{key}' cannot be cleared.",
            )
    for key, value in changes.items():
        setattr(row, key, value)
    if not (row.band_min <= row.band_mid <= row.band_max):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The band runs minimum ≤ midpoint ≤ maximum.",
        )
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="pay_grade",
        entity_id=row.id,
        actor=user,
        changes={"before": before, "after": _plain(row)},
        summary=f"Changed grade {row.code}",
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def check_grade_code(session: AsyncSession, user: User, code: str | None) -> None:
    """Once a workspace has grades, a record may only carry an active one."""
    if code is None:
        return
    defined = await session.scalar(
        tenant_scoped(select(func.count()).select_from(PayGrade), PayGrade, user)
    )
    if not defined:
        return
    found = await session.scalar(
        tenant_scoped(
            select(PayGrade.id).where(PayGrade.code == code, PayGrade.active.is_(True)),
            PayGrade,
            user,
        )
    )
    if found is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Grade {code} is not one of this workspace's grades.",
        )


async def positioning(
    session: AsyncSession, user: User, *, on: date | None = None
) -> dict[str, Any]:
    """Every current person's CTC against their grade's band. HR only."""
    if not permissions.is_hr(user):
        raise permissions.forbidden("Pay positioning is for HR.")
    today = on or date.today()
    grades = {g.code: g for g, _ in await list_grades(session, user, include_inactive=True)}
    rows = (
        await session.execute(
            tenant_scoped(
                select(Employee, EmployeeSalary)
                .outerjoin(
                    EmployeeSalary,
                    (EmployeeSalary.employee_id == Employee.id)
                    & (EmployeeSalary.effective_from <= today)
                    & or_(
                        EmployeeSalary.effective_to.is_(None),
                        EmployeeSalary.effective_to >= today,
                    ),
                )
                .where(
                    Employee.deleted_at.is_(None),
                    Employee.status != EmploymentStatus.EXITED,
                )
                .order_by(Employee.employee_code, EmployeeSalary.effective_from.desc()),
                Employee,
                user,
            )
        )
    ).all()

    seen: set[uuid.UUID] = set()
    people: list[dict[str, Any]] = []
    summary = {"below": 0, "within": 0, "above": 0, "no_grade": 0, "no_salary": 0}
    for employee, salary in rows:
        if employee.id in seen:
            continue
        seen.add(employee.id)
        grade = grades.get(employee.grade or "")
        ctc = Decimal(str(salary.ctc_annual)) if salary else None
        if grade is None:
            position = "no_grade"
        elif ctc is None:
            position = "no_salary"
        elif ctc < grade.band_min:
            position = "below"
        elif ctc > grade.band_max:
            position = "above"
        else:
            position = "within"
        summary[position] += 1
        compa = (
            (ctc / grade.band_mid).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if grade is not None and ctc is not None and grade.band_mid > 0
            else None
        )
        people.append(
            {
                "employee_id": str(employee.id),
                "employee_code": employee.employee_code,
                "name": employee.full_name,
                "designation": employee.designation,
                "grade": employee.grade,
                "ctc_annual": str(ctc) if ctc is not None else None,
                "band_min": str(grade.band_min) if grade else None,
                "band_mid": str(grade.band_mid) if grade else None,
                "band_max": str(grade.band_max) if grade else None,
                "compa_ratio": str(compa) if compa is not None else None,
                "position": position,
            }
        )
    return {"as_of": today.isoformat(), "summary": summary, "people": people}
