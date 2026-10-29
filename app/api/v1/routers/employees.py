"""Employee master endpoints (FRD 5.1), the extended profile and profile change requests."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile, status

from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.employee import EmploymentStatus, ProfileChangeStatus
from app.models.user import UserRole
from app.schemas.common import Page
from app.schemas.employee import (
    EmployeeCreate,
    EmployeeRead,
    EmployeeSelfUpdate,
    EmployeeSummary,
    EmployeeUpdate,
    EntityMoveRequest,
    EntityMoveResult,
    ProjectMoveRequest,
    ProjectMoveResult,
)
from app.schemas.employee_import import ImportPreview, ImportResult
from app.schemas.employee_profile import (
    CelebrationsRead,
    ChangeRequestCreate,
    ChangeRequestDecision,
    ChangeRequestRead,
    ChangeRequestRejection,
    EmployeeProfileRead,
    ProfileUpdate,
)
from app.services import custom_tables, employee_timeline
from app.services import employee as employee_service
from app.services import employee_profile as profile_service
from app.services.dpdp.gates import strip_hidden_photos
from app.services.employee_import import service as import_service
from app.services.employee_import.sheet import MAX_BYTES as IMPORT_MAX_BYTES
from app.services.employee_profile import PHOTO_MAX_BYTES

router = APIRouter(prefix="/employees", tags=["core-hr"])

HR_WRITE_ROLES = (UserRole.ADMIN, UserRole.SUB_ADMIN)
HR_ONLY = [Depends(require_roles(*HR_WRITE_ROLES))]


@router.get("", response_model=Page[EmployeeSummary])
async def list_employees(
    user: CurrentUser,
    session: DbSession,
    search: str | None = Query(default=None, max_length=128),
    department_id: uuid.UUID | None = None,
    location_id: uuid.UUID | None = None,
    manager_id: uuid.UUID | None = None,
    legal_entity_id: uuid.UUID | None = None,
    without_legal_entity: bool = Query(
        default=False, description="Only people with no company: no payroll run pays them."
    ),
    project_id: uuid.UUID | None = None,
    without_project: bool = Query(default=False, description="Only people on no project."),
    employment_status: EmploymentStatus | None = Query(default=None, alias="status"),
    current: bool = Query(
        default=False, description="Everybody who has not left: every status but exited."
    ),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
) -> Page[EmployeeSummary]:
    rows, total = await employee_service.list_employees(
        session,
        user,
        search=search,
        department_id=department_id,
        location_id=location_id,
        manager_id=manager_id,
        legal_entity_id=legal_entity_id,
        without_legal_entity=without_legal_entity,
        project_id=project_id,
        without_project=without_project,
        status_filter=employment_status.value if employment_status else None,
        current_only=current,
        offset=(page - 1) * page_size,
        limit=page_size,
    )
    items = [EmployeeSummary.model_validate(row) for row in rows]
    await strip_hidden_photos(session, user, items)
    return Page[EmployeeSummary](
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/export", dependencies=HR_ONLY)
async def export_directory(
    user: CurrentUser,
    session: DbSession,
    request: Request,
    search: str | None = Query(default=None, max_length=128),
    department_id: uuid.UUID | None = None,
    location_id: uuid.UUID | None = None,
    manager_id: uuid.UUID | None = None,
    legal_entity_id: uuid.UUID | None = None,
    without_legal_entity: bool = False,
    project_id: uuid.UUID | None = None,
    without_project: bool = False,
    employment_status: EmploymentStatus | None = Query(default=None, alias="status"),
    current: bool = False,
) -> Response:
    """The directory as CSV, filtered like the screen, with each manager named."""
    content = await employee_service.directory_csv(
        session,
        user,
        search=search,
        department_id=department_id,
        location_id=location_id,
        manager_id=manager_id,
        legal_entity_id=legal_entity_id,
        without_legal_entity=without_legal_entity,
        project_id=project_id,
        without_project=without_project,
        status_filter=employment_status.value if employment_status else None,
        current_only=current,
        request=request,
    )
    await session.commit()
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="directory.csv"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post(
    "",
    response_model=EmployeeRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=HR_ONLY,
)
async def create_employee(
    payload: EmployeeCreate, user: CurrentUser, session: DbSession, request: Request
) -> EmployeeRead:
    employee = await employee_service.create_employee(session, user, payload, request)
    return EmployeeRead.model_validate(employee)


@router.patch("/bulk/legal-entity", response_model=EntityMoveResult, dependencies=HR_ONLY)
async def move_to_legal_entity(
    payload: EntityMoveRequest, user: CurrentUser, session: DbSession, request: Request
) -> EntityMoveResult:
    """Put the chosen people in one company (legal entity), all in one go.

    A PATCH, so a sub-admin needs the Edit right on People (it changes existing
    records). Their payroll from this month, their filings and the logo they
    see follow the company. Each move is in their history as a transfer; people
    who have left are not moved.
    """
    return await employee_service.move_to_legal_entity(session, user, payload, request=request)


@router.patch("/bulk/project", response_model=ProjectMoveResult, dependencies=HR_ONLY)
async def move_to_project(
    payload: ProjectMoveRequest, user: CurrentUser, session: DbSession, request: Request
) -> ProjectMoveResult:
    """Put the chosen people on one project (a client's or Internal), or on none.

    Their company and payroll do not change. Each move is in their history as
    a project change; people who have left are not moved.
    """
    return await employee_service.move_to_project(session, user, payload, request=request)


# ── Import from a spreadsheet ───────────────────────────────────────────────

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.get("/import/template", dependencies=HR_ONLY)
async def import_template(user: CurrentUser, session: DbSession) -> Response:
    """The import template: factoHR's column names, plus this workspace's custom fields."""
    content = await import_service.template(session, user)
    return Response(
        content=content,
        media_type=XLSX,
        headers={
            "Content-Disposition": 'attachment; filename="askmira-employee-import.xlsx"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/import/preview", response_model=ImportPreview, dependencies=HR_ONLY)
async def import_preview(
    user: CurrentUser, session: DbSession, file: UploadFile = File(...)
) -> ImportPreview:
    """What importing the file would do, row by row. Nothing is saved."""
    data = await file.read(IMPORT_MAX_BYTES + 1)
    return await import_service.preview(session, user, data, file.filename or "employees.xlsx")


@router.post("/import/rejected", dependencies=HR_ONLY)
async def import_rejected(
    user: CurrentUser, session: DbSession, file: UploadFile = File(...)
) -> Response:
    """The file's rows that are not added, with their own cells and why. Nothing is saved."""
    data = await file.read(IMPORT_MAX_BYTES + 1)
    content = await import_service.rejected(session, user, data, file.filename or "employees.xlsx")
    return Response(
        content=content,
        media_type=XLSX,
        headers={
            "Content-Disposition": 'attachment; filename="employees-not-added.xlsx"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post(
    "/import",
    response_model=ImportResult,
    status_code=status.HTTP_201_CREATED,
    dependencies=HR_ONLY,
)
async def import_employees(
    user: CurrentUser,
    session: DbSession,
    request: Request,
    file: UploadFile = File(...),
    include_leavers: bool = Form(default=True),
) -> ImportResult:
    """Import the file's ready rows in one transaction; codes already here are skipped."""
    data = await file.read(IMPORT_MAX_BYTES + 1)
    result = await import_service.run(
        session,
        user,
        data,
        file.filename or "employees.xlsx",
        include_leavers=include_leavers,
        request=request,
    )
    await session.commit()
    return result


# Literal paths (`/me`, `/celebrations`, `/change-requests`) are declared before
# `/{employee_id}` so they are not parsed as an id.


@router.get("/celebrations", response_model=CelebrationsRead)
async def celebrations(
    user: CurrentUser, session: DbSession, days: int = Query(default=30, ge=1, le=90)
) -> CelebrationsRead:
    """Upcoming birthdays and work anniversaries, for people who chose to share them."""
    return await employee_service.celebrations(session, user, days=days)


@router.get("/me", response_model=EmployeeRead)
async def get_my_record(user: CurrentUser, session: DbSession) -> EmployeeRead:
    """The caller's own full record, found through the login's own link."""
    employee = await employee_service.get_own_employee(session, user)
    return await employee_service.employee_view(session, user, employee)


@router.patch("/me", response_model=EmployeeRead)
async def update_my_contact(
    payload: EmployeeSelfUpdate, user: CurrentUser, session: DbSession, request: Request
) -> EmployeeRead:
    """Self-service contact details. Employment fields are not in the contract."""
    employee = await employee_service.update_own_contact(session, user, payload, request)
    return await employee_service.employee_view(session, user, employee)


@router.get("/me/profile", response_model=EmployeeProfileRead)
async def get_my_profile(user: CurrentUser, session: DbSession) -> EmployeeProfileRead:
    employee = await employee_service.get_own_employee(session, user)
    return await profile_service.read_profile(session, user, employee.id)


@router.put("/me/profile", response_model=EmployeeProfileRead)
async def update_my_profile(
    payload: ProfileUpdate, user: CurrentUser, session: DbSession, request: Request
) -> EmployeeProfileRead:
    """Save one of your own non-sensitive sections. Sensitive ones are a change request."""
    employee = await employee_service.get_own_employee(session, user)
    return await profile_service.write_section(
        session, user, employee.id, payload.change, request=request
    )


@router.get("/me/change-requests", response_model=list[ChangeRequestRead])
async def my_change_requests(user: CurrentUser, session: DbSession) -> list[ChangeRequestRead]:
    employee = await employee_service.get_own_employee(session, user)
    return await profile_service.list_change_requests(session, user, employee_id=employee.id)


@router.post(
    "/me/change-requests",
    response_model=ChangeRequestRead,
    status_code=status.HTTP_201_CREATED,
)
async def raise_change_request(
    payload: ChangeRequestCreate, user: CurrentUser, session: DbSession, request: Request
) -> ChangeRequestRead:
    """Ask HR to change your legal name, date of birth, identifiers, bank, address or nominees."""
    _ = user.employee_id  # the caller's own record; the service refuses anything else
    return await profile_service.create_change_request(
        session, user, payload.change, payload.reason, request=request
    )


@router.put("/me/photo", response_model=EmployeeRead)
async def upload_my_photo(
    user: CurrentUser,
    session: DbSession,
    request: Request,
    file: Annotated[UploadFile, File()],
) -> EmployeeRead:
    employee = await employee_service.get_own_employee(session, user)
    data = await file.read(PHOTO_MAX_BYTES + 1)
    saved = await profile_service.set_photo(session, user, employee.id, data, request=request)
    return await employee_service.employee_view(session, user, saved)


@router.delete("/me/photo", response_model=EmployeeRead)
async def remove_my_photo(user: CurrentUser, session: DbSession, request: Request) -> EmployeeRead:
    employee = await employee_service.get_own_employee(session, user)
    saved = await profile_service.remove_photo(session, user, employee.id, request=request)
    return await employee_service.employee_view(session, user, saved)


@router.get("/change-requests", response_model=list[ChangeRequestRead], dependencies=HR_ONLY)
async def list_change_requests(
    user: CurrentUser,
    session: DbSession,
    request_status: ProfileChangeStatus | None = Query(default=None, alias="status"),
    employee_id: uuid.UUID | None = None,
) -> list[ChangeRequestRead]:
    """HR's queue of profile change requests."""
    return await profile_service.list_change_requests(
        session, user, employee_id=employee_id, status_filter=request_status
    )


@router.post(
    "/change-requests/{request_id}/approve",
    response_model=ChangeRequestRead,
    dependencies=HR_ONLY,
)
async def approve_change_request(
    request_id: uuid.UUID,
    payload: ChangeRequestDecision,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> ChangeRequestRead:
    """Approve, and apply the change in the same transaction."""
    return await profile_service.decide_change_request(
        session, user, request_id, approve=True, note=payload.note, request=request
    )


@router.post(
    "/change-requests/{request_id}/reject",
    response_model=ChangeRequestRead,
    dependencies=HR_ONLY,
)
async def reject_change_request(
    request_id: uuid.UUID,
    payload: ChangeRequestRejection,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> ChangeRequestRead:
    return await profile_service.decide_change_request(
        session, user, request_id, approve=False, note=payload.note, request=request
    )


@router.post("/change-requests/{request_id}/withdraw", response_model=ChangeRequestRead)
async def withdraw_change_request(
    request_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> ChangeRequestRead:
    """Take back your own pending request."""
    _ = user.employee_id  # only the requester; anybody else's request is a 404
    return await profile_service.withdraw_change_request(session, user, request_id, request=request)


@router.get("/{employee_id}", response_model=EmployeeRead)
async def get_employee(
    employee_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> EmployeeRead:
    employee = await employee_service.get_employee(session, user, employee_id)
    return await employee_service.employee_view(session, user, employee)


@router.get("/{employee_id}/custom-tables")
async def get_custom_tables(
    employee_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[dict]:
    """Each custom table with this person's rows: HR, or the person for non-sensitive ones."""
    return await custom_tables.tables_for(session, user, employee_id)


@router.post("/{employee_id}/custom-tables/{table_id}/rows", status_code=status.HTTP_201_CREATED)
async def add_custom_table_row(
    employee_id: uuid.UUID,
    table_id: uuid.UUID,
    values: dict[str, Any],
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict:
    return await custom_tables.add_row(
        session, user, employee_id, table_id, values, request=request
    )


@router.patch("/custom-table-rows/{row_id}")
async def update_custom_table_row(
    row_id: uuid.UUID,
    values: dict[str, Any],
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict:
    return await custom_tables.update_row(session, user, row_id, values, request=request)


@router.delete(
    "/custom-table-rows/{row_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None
)
async def delete_custom_table_row(
    row_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> None:
    await custom_tables.delete_row(session, user, row_id, request=request)


@router.get("/{employee_id}/timeline")
async def get_timeline(
    employee_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    module: Annotated[list[str] | None, Query(max_length=20)] = None,
) -> dict:
    """Everything that has happened to this person, across modules, newest first."""
    return await employee_timeline.timeline(
        session, user, employee_id, modules=set(module) if module else None
    )


@router.get("/{employee_id}/profile", response_model=EmployeeProfileRead)
async def get_profile(
    employee_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> EmployeeProfileRead:
    """The extended profile: the person themselves and HR only."""
    return await profile_service.read_profile(session, user, employee_id)


@router.put("/{employee_id}/profile", response_model=EmployeeProfileRead, dependencies=HR_ONLY)
async def update_profile(
    employee_id: uuid.UUID,
    payload: ProfileUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> EmployeeProfileRead:
    """HR writes any section of somebody else's profile directly."""
    return await profile_service.write_section(
        session, user, employee_id, payload.change, request=request
    )


@router.get("/{employee_id}/change-requests", response_model=list[ChangeRequestRead])
async def employee_change_requests(
    employee_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[ChangeRequestRead]:
    """One person's requests: theirs to see, and HR's."""
    return await profile_service.list_change_requests(session, user, employee_id=employee_id)


@router.get("/{employee_id}/photo", response_class=Response)
async def get_photo(employee_id: uuid.UUID, user: CurrentUser, session: DbSession) -> Response:
    """A colleague's photo, as the directory shows it beside their name."""
    mime, data = await profile_service.photo(session, user, employee_id)
    return Response(
        content=data,
        media_type=mime,
        headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"},
    )


@router.put("/{employee_id}/photo", response_model=EmployeeRead, dependencies=HR_ONLY)
async def upload_photo(
    employee_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    request: Request,
    file: Annotated[UploadFile, File()],
) -> EmployeeRead:
    data = await file.read(PHOTO_MAX_BYTES + 1)
    saved = await profile_service.set_photo(session, user, employee_id, data, request=request)
    return await employee_service.employee_view(session, user, saved)


@router.patch(
    "/{employee_id}",
    response_model=EmployeeRead,
    dependencies=HR_ONLY,
)
async def update_employee(
    employee_id: uuid.UUID,
    payload: EmployeeUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> EmployeeRead:
    employee = await employee_service.update_employee(session, user, employee_id, payload, request)
    return EmployeeRead.model_validate(employee)


@router.delete(
    "/{employee_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    dependencies=[Depends(require_roles(UserRole.ADMIN))],
)
async def deactivate_employee(
    employee_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> None:
    """Soft-delete. The record stays queryable for payroll and audit history."""
    await employee_service.deactivate_employee(session, user, employee_id, request)
