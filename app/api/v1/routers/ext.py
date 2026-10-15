"""The customer API, authenticated by an API key, not a person's login.

`Authorization: Bearer amk_...`. Each route names the scope it needs; a key
without it is refused. Everything returned is scoped to the key's workspace.

Reads cover the directory, org, holidays, leave and balances, attendance and
open roles. Writes are deliberately few: adding and updating employees (through
the same validation and audit as the screens, recorded as the key) and pushing
attendance punches from a system the workspace runs.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.deps import DbSession, tenant_scoped
from app.models.attendance import AttendanceDay, AttendancePunch, PunchDirection, PunchSource
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.hiring import Requisition, RequisitionStatus
from app.models.leave import Holiday, LeaveBalance, LeaveRequest, LeaveType
from app.models.org import Department, Location
from app.models.user import User
from app.schemas.employee import EmployeeCreate, EmployeeUpdate
from app.services import audit, integrations
from app.services import employee as employee_service

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


def _employee_out(e: Employee) -> dict[str, Any]:
    return {
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


# ── Reads ───────────────────────────────────────────────────────────────────


@router.get("/holidays")
async def holidays(
    session: DbSession,
    actor: Annotated[User, Depends(_key("org:read"))],
    year: int = Query(ge=2000, le=2100),
) -> list[dict[str, Any]]:
    """The year's holidays: national, state and location-specific, optional ones marked."""
    rows = (
        await session.scalars(
            tenant_scoped(
                select(Holiday).where(Holiday.year == year).order_by(Holiday.holiday_date),
                Holiday,
                actor,
            )
        )
    ).all()
    return [
        {
            "date": h.holiday_date.isoformat(),
            "name": h.name,
            "state_code": h.state_code,
            "location_id": str(h.location_id) if h.location_id else None,
            "optional": h.is_optional,
        }
        for h in rows
    ]


@router.get("/leave-balances")
async def leave_balances(
    session: DbSession,
    actor: Annotated[User, Depends(_key("leave:read"))],
    year: int = Query(ge=2000, le=2100),
) -> list[dict[str, Any]]:
    """Each person's balance per leave type for the year, with its parts."""
    rows = (
        await session.execute(
            tenant_scoped(
                select(LeaveBalance, LeaveType.code, Employee.employee_code)
                .join(LeaveType, LeaveType.id == LeaveBalance.leave_type_id)
                .join(Employee, Employee.id == LeaveBalance.employee_id)
                .where(LeaveBalance.year == year)
                .order_by(Employee.employee_code, LeaveType.code),
                LeaveBalance,
                actor,
            )
        )
    ).all()
    out = []
    for b, type_code, code in rows:
        parts = {
            "opening": b.opening_balance, "accrued": b.accrued, "used": b.used,
            "encashed": b.encashed, "carried_forward": b.carried_forward,
            "adjusted": b.adjusted,
        }
        available = (
            parts["opening"] + parts["accrued"] + parts["carried_forward"] + parts["adjusted"]
            - parts["used"] - parts["encashed"]
        )
        out.append(
            {
                "employee_code": code,
                "leave_type": type_code,
                **{k: str(v) for k, v in parts.items()},
                "available": str(available),
            }
        )
    return out


@router.get("/job-openings")
async def job_openings(
    session: DbSession, actor: Annotated[User, Depends(_key("hiring:read"))]
) -> list[dict[str, Any]]:
    """Open requisitions: what is being hired for, where, and how many."""
    rows = (
        await session.scalars(
            tenant_scoped(
                select(Requisition)
                .where(Requisition.status == RequisitionStatus.OPEN)
                .order_by(Requisition.code),
                Requisition,
                actor,
            )
        )
    ).all()
    return [
        {
            "id": str(r.id),
            "code": r.code,
            "title": r.title,
            "department_id": str(r.department_id) if r.department_id else None,
            "location_id": str(r.location_id) if r.location_id else None,
            "openings": r.openings,
            "employment_type": r.employment_type,
        }
        for r in rows
    ]


# ── Writes ──────────────────────────────────────────────────────────────────


@router.post("/employees", status_code=status.HTTP_201_CREATED)
async def create_employee(
    payload: EmployeeCreate,
    session: DbSession,
    request: Request,
    actor: Annotated[User, Depends(_key("employees:write"))],
) -> dict[str, Any]:
    """Add an employee, validated exactly as the screen validates one; audited as the key."""
    employee = await employee_service.create_employee(session, actor, payload, request=request)
    return _employee_out(employee)


@router.patch("/employees/{employee_id}")
async def update_employee(
    employee_id: uuid.UUID,
    payload: EmployeeUpdate,
    session: DbSession,
    request: Request,
    actor: Annotated[User, Depends(_key("employees:write"))],
) -> dict[str, Any]:
    """Change the fields named; a future-dated job change is refused here as on screen."""
    employee = await employee_service.update_employee(
        session, actor, employee_id, payload, request=request
    )
    return _employee_out(employee)


class PunchIn(BaseModel):
    employee_code: str = Field(min_length=1, max_length=32)
    #: With its offset, e.g. 2026-10-15T09:31:00+05:30.
    punched_at: datetime
    direction: Literal["in", "out"]
    device_id: str | None = Field(default=None, max_length=64)


class PunchBatchIn(BaseModel):
    punches: list[PunchIn] = Field(min_length=1, max_length=1000)


@router.post("/attendance-punches")
async def push_punches(
    payload: PunchBatchIn,
    session: DbSession,
    request: Request,
    actor: Annotated[User, Depends(_key("attendance:write"))],
) -> dict[str, Any]:
    """Punches from your own system. Unknown codes and repeats are reported, not stored.

    Days are judged from punches by the nightly recompute, as for any device.
    """
    if any(p.punched_at.tzinfo is None for p in payload.punches):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Every punched_at needs its UTC offset, e.g. +05:30.",
        )
    codes = {p.employee_code for p in payload.punches}
    people = {
        e.employee_code: e.id
        for e in (
            await session.scalars(
                tenant_scoped(
                    select(Employee).where(
                        Employee.employee_code.in_(codes), Employee.deleted_at.is_(None)
                    ),
                    Employee,
                    actor,
                )
            )
        ).all()
    }
    earliest = min(p.punched_at for p in payload.punches)
    latest = max(p.punched_at for p in payload.punches)
    existing = {
        (row.employee_id, row.punched_at)
        for row in (
            await session.scalars(
                tenant_scoped(
                    select(AttendancePunch).where(
                        AttendancePunch.employee_id.in_(list(people.values()) or [uuid.uuid4()]),
                        AttendancePunch.punched_at >= earliest,
                        AttendancePunch.punched_at <= latest,
                    ),
                    AttendancePunch,
                    actor,
                )
            )
        ).all()
    }
    stored = duplicates = 0
    unknown: set[str] = set()
    for punch in payload.punches:
        employee_id = people.get(punch.employee_code)
        if employee_id is None:
            unknown.add(punch.employee_code)
            continue
        key = (employee_id, punch.punched_at)
        if key in existing:
            duplicates += 1
            continue
        existing.add(key)
        session.add(
            AttendancePunch(
                tenant_id=actor.tenant_id,
                employee_id=employee_id,
                punched_at=punch.punched_at,
                direction=PunchDirection(punch.direction),
                source=PunchSource.IMPORT,
                device_id=punch.device_id,
                raw_payload={"via": "customer_api"},
            )
        )
        stored += 1
    await audit.record(
        session,
        tenant_id=actor.tenant_id,
        action=AuditAction.CREATE,
        entity_type="attendance_punch_batch",
        actor=actor,
        changes={"stored": stored, "duplicates": duplicates, "unknown_codes": sorted(unknown)},
        summary=f"{stored} punch(es) received through the customer API",
        request=request,
    )
    await session.commit()
    return {"stored": stored, "duplicates": duplicates, "unknown_codes": sorted(unknown)}
