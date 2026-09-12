"""Employee master endpoints (FRD 5.1)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request, status

from app.core import permissions
from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.user import UserRole
from app.schemas.common import Page
from app.schemas.employee import (
    EmployeeCreate,
    EmployeeRead,
    EmployeeSummary,
    EmployeeUpdate,
)
from app.services import employee as employee_service

router = APIRouter(prefix="/employees", tags=["core-hr"])

HR_WRITE_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN, UserRole.HR_OPS)


@router.get("", response_model=Page[EmployeeSummary])
async def list_employees(
    user: CurrentUser,
    session: DbSession,
    search: str | None = Query(default=None, max_length=128),
    department_id: uuid.UUID | None = None,
    location_id: uuid.UUID | None = None,
    employment_status: str | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
) -> Page[EmployeeSummary]:
    rows, total = await employee_service.list_employees(
        session,
        user,
        search=search,
        department_id=department_id,
        location_id=location_id,
        status_filter=employment_status,
        offset=(page - 1) * page_size,
        limit=page_size,
    )
    return Page[EmployeeSummary](
        items=[EmployeeSummary.model_validate(row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post(
    "",
    response_model=EmployeeRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*HR_WRITE_ROLES))],
)
async def create_employee(
    payload: EmployeeCreate, user: CurrentUser, session: DbSession, request: Request
) -> EmployeeRead:
    employee = await employee_service.create_employee(session, user, payload, request)
    return EmployeeRead.model_validate(employee)


@router.get("/{employee_id}", response_model=EmployeeRead)
async def get_employee(
    employee_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> EmployeeRead:
    employee = await employee_service.get_employee(session, user, employee_id)
    record = EmployeeRead.model_validate(employee)
    if employee.id == user.employee_id or permissions.is_hr(user):
        return record
    # A colleague's profile is the directory view. Nobody outside HR sees
    # another person's date of birth, phone, personal email, gender or custom
    # fields; a line manager also keeps the employment fields for their own
    # reporting line.
    hidden: dict[str, object] = {
        "personal_email": None,
        "phone": None,
        "date_of_birth": None,
        "gender": None,
        "custom_fields": {},
    }
    if not await permissions.is_line_manager_of(session, user, employee.id):
        hidden |= {
            "grade": None,
            "probation_end_date": None,
            "notice_period_days": None,
            "date_of_exit": None,
        }
    return record.model_copy(update=hidden)


@router.patch(
    "/{employee_id}",
    response_model=EmployeeRead,
    dependencies=[Depends(require_roles(*HR_WRITE_ROLES))],
)
async def update_employee(
    employee_id: uuid.UUID,
    payload: EmployeeUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> EmployeeRead:
    employee = await employee_service.update_employee(
        session, user, employee_id, payload, request
    )
    return EmployeeRead.model_validate(employee)


@router.delete(
    "/{employee_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    dependencies=[Depends(require_roles(UserRole.TENANT_ADMIN, UserRole.HR_ADMIN))],
)
async def deactivate_employee(
    employee_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> None:
    """Soft-delete. The record stays queryable for payroll and audit history."""
    await employee_service.deactivate_employee(session, user, employee_id, request)
