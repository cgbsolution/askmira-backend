"""The customer API: read-only, authenticated by an API key, not a person's login.

`Authorization: Bearer amk_...`. Each route names the scope it needs; a key
without it is refused. Everything returned is scoped to the key's workspace.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

from app.core.deps import DbSession, tenant_scoped
from app.models.attendance import AttendanceDay
from app.models.employee import Employee
from app.models.leave import LeaveRequest, LeaveType
from app.models.org import Department, Location
from app.models.user import User
from app.services import integrations

router = APIRouter(prefix="/ext", tags=["customer-api"])

_bearer = HTTPBearer(auto_error=False)
MAX_RANGE_DAYS = 93


def _key(scope: str):
    async def dependency(
        session: DbSession,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    ) -> User:
        if credentials is None or not credentials.credentials.startswith("amk_"):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Send an API key as a Bearer token.",
            )
        return await integrations.authenticate_key(session, credentials.credentials, scope)

    return dependency


def _range(start: date, end: date) -> None:
    if end < start or (end - start).days > MAX_RANGE_DAYS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Ask for at most {MAX_RANGE_DAYS} days at a time.",
        )


@router.get("/employees")
async def employees(
    session: DbSession,
    actor: Annotated[User, Depends(_key("employees:read"))],
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=200, ge=1, le=500),
) -> dict[str, Any]:
    """The directory: codes, names, work email, title, department, manager, status."""
    rows = (
        await session.scalars(
            tenant_scoped(
                select(Employee)
                .where(Employee.deleted_at.is_(None))
                .order_by(Employee.employee_code)
                .offset((page - 1) * page_size)
                .limit(page_size),
                Employee,
                actor,
            )
        )
    ).all()
    return {
        "page": page,
        "items": [
            {
                "id": str(e.id),
                "employee_code": e.employee_code,
                "first_name": e.first_name,
                "last_name": e.last_name,
                "work_email": e.work_email,
                "designation": e.designation,
                "department_id": str(e.department_id) if e.department_id else None,
                "location_id": str(e.location_id) if e.location_id else None,
                "manager_id": str(e.manager_id) if e.manager_id else None,
                "status": e.status.value,
                "date_of_joining": e.date_of_joining.isoformat() if e.date_of_joining else None,
            }
            for e in rows
        ],
    }


@router.get("/departments")
async def departments(
    session: DbSession, actor: Annotated[User, Depends(_key("org:read"))]
) -> list[dict[str, Any]]:
    rows = (
        await session.scalars(
            tenant_scoped(select(Department).order_by(Department.name), Department, actor)
        )
    ).all()
    return [{"id": str(d.id), "name": d.name, "code": d.code} for d in rows]


@router.get("/locations")
async def locations(
    session: DbSession, actor: Annotated[User, Depends(_key("org:read"))]
) -> list[dict[str, Any]]:
    rows = (
        await session.scalars(
            tenant_scoped(select(Location).order_by(Location.name), Location, actor)
        )
    ).all()
    return [
        {"id": str(lo.id), "name": lo.name, "code": lo.code, "state_code": lo.state_code}
        for lo in rows
    ]


@router.get("/leave-requests")
async def leave_requests(
    session: DbSession,
    actor: Annotated[User, Depends(_key("leave:read"))],
    start: date = Query(alias="from"),
    end: date = Query(alias="to"),
) -> list[dict[str, Any]]:
    """Leave overlapping the range: who, which type, which days, and its status."""
    _range(start, end)
    rows = (
        await session.execute(
            tenant_scoped(
                select(LeaveRequest, LeaveType, Employee)
                .join(LeaveType, LeaveType.id == LeaveRequest.leave_type_id)
                .join(Employee, Employee.id == LeaveRequest.employee_id)
                .where(LeaveRequest.start_date <= end, LeaveRequest.end_date >= start)
                .order_by(LeaveRequest.start_date),
                LeaveRequest,
                actor,
            )
        )
    ).all()
    return [
        {
            "id": str(r.id),
            "employee_code": e.employee_code,
            "leave_type": t.code,
            "start_date": r.start_date.isoformat(),
            "end_date": r.end_date.isoformat(),
            "days": str(r.total_days),
            "half_day": r.is_half_day,
            "status": r.status.value,
        }
        for r, t, e in rows
    ]


@router.get("/attendance-days")
async def attendance_days(
    session: DbSession,
    actor: Annotated[User, Depends(_key("attendance:read"))],
    start: date = Query(alias="from"),
    end: date = Query(alias="to"),
) -> list[dict[str, Any]]:
    """Each judged attendance day in the range."""
    _range(start, end)
    rows = (
        await session.execute(
            tenant_scoped(
                select(AttendanceDay, Employee.employee_code)
                .join(Employee, Employee.id == AttendanceDay.employee_id)
                .where(
                    AttendanceDay.attendance_date >= start,
                    AttendanceDay.attendance_date <= end,
                )
                .order_by(AttendanceDay.attendance_date, Employee.employee_code),
                AttendanceDay,
                actor,
            )
        )
    ).all()
    return [
        {
            "employee_code": code,
            "date": d.attendance_date.isoformat(),
            "status": d.status.value,
            "payable_fraction": str(d.payable_fraction),
            "worked_minutes": d.worked_minutes,
        }
        for d, code in rows
    ]
