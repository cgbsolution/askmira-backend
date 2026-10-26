"""Salary structure and CTC builder endpoints (slice 01).

Everything here is behind the tenant guard and a payroll-capable role. Assigning
or revising a salary writes exactly one audit entry, in the same transaction as
the assignment.
"""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)

from app.core.config import settings
from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.payroll import SalaryStructure
from app.models.user import UserRole
from app.schemas.common import Page
from app.schemas.pay_grades import PayGradeCreate, PayGradeRead, PayGradeUpdate
from app.schemas.payroll import (
    EmployeeSalaryCreate,
    EmployeeSalaryRead,
    EmployeeSalaryUpdate,
    ResolvedComponentRead,
    SalaryBreakdownRead,
    SalaryComponentCreate,
    SalaryComponentRead,
    SalaryDeleteRead,
    SalaryExportInput,
    SalaryImportRead,
    SalaryPreviewInput,
    SalaryRevisionRead,
    SalaryStructureCreate,
    SalaryStructureRead,
    SalaryTrailEntryRead,
    StandardSetupRead,
    StandardStatusRead,
)
from app.schemas.payroll_run import SalaryOverviewRead, SalaryStructureDetailRead
from app.services.payroll import grades as grade_service
from app.services.payroll import salary_import, salary_master, standard_setup
from app.services.payroll import service as salary_service
from app.services.payroll.salary_structure import SalaryBreakdown

router = APIRouter(
    tags=["payroll"],
    dependencies=[Depends(require_roles(UserRole.ADMIN, UserRole.SUB_ADMIN))],
)


# ── Components ──────────────────────────────────────────────────────────────


@router.get("/salary-components", response_model=list[SalaryComponentRead])
async def list_components(user: CurrentUser, session: DbSession) -> list[SalaryComponentRead]:
    rows = await salary_service.list_components(session, user)
    return [SalaryComponentRead.model_validate(row) for row in rows]


@router.post(
    "/salary-components",
    response_model=SalaryComponentRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_component(
    payload: SalaryComponentCreate, user: CurrentUser, session: DbSession, request: Request
) -> SalaryComponentRead:
    component = await salary_service.create_component(session, user, payload, request)
    return SalaryComponentRead.model_validate(component)


# ── Structures ──────────────────────────────────────────────────────────────


@router.get("/salary-structures", response_model=list[SalaryStructureRead])
async def list_structures(user: CurrentUser, session: DbSession) -> list[SalaryStructureRead]:
    rows = await salary_service.list_structures(session, user)
    return [SalaryStructureRead.model_validate(row) for row in rows]


@router.post(
    "/salary-structures",
    response_model=SalaryStructureRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_structure(
    payload: SalaryStructureCreate, user: CurrentUser, session: DbSession, request: Request
) -> SalaryStructureRead:
    """Create a structure.

    Rejects — with the offending component named — a structure that has no
    BALANCE component, more than one, a circular reference, a dangling
    reference, or a malformed formula.
    """
    structure = await salary_service.create_structure(session, user, payload, request)
    return SalaryStructureRead.model_validate(structure)


@router.get("/salary-structures/{structure_id}", response_model=SalaryStructureDetailRead)
async def get_structure(
    structure_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> SalaryStructureDetailRead:
    """A structure with its components and per-structure overrides."""
    return await salary_service.structure_detail(session, user, structure_id)


@router.get("/salary-structures/{structure_id}/preview", response_model=SalaryBreakdownRead)
async def preview_structure(
    structure_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    ctc: str = Query(description="Annual CTC, e.g. 1200000"),
) -> SalaryBreakdownRead:
    """Resolve a CTC against a structure without persisting anything.

    `ctc` is a string rather than a float: parsing "1200000.50" through a float
    would introduce the exact representation error this module exists to avoid.
    """
    try:
        amount = Decimal(ctc)
    except InvalidOperation as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"'{ctc}' is not a valid CTC amount",
        ) from exc

    structure, breakdown = await salary_service.preview_structure(
        session, user, structure_id, amount
    )
    groups = await salary_service.wage_group_map(session, user)
    return _breakdown_read(structure, breakdown, groups)


@router.post("/salary-structures/{structure_id}/preview", response_model=SalaryBreakdownRead)
async def preview_salary(
    structure_id: uuid.UUID, payload: SalaryPreviewInput, user: CurrentUser, session: DbSession
) -> SalaryBreakdownRead:
    """Resolve a CTC with amounts typed for some wage types. Writes nothing.

    The Salary master editor calls this as HR types, so every figure on that
    screen (annual amounts, sub-totals) comes from here, never from the browser.
    """
    structure, resolved = await salary_service.preview_salary(session, user, structure_id, payload)
    groups = await salary_service.wage_group_map(session, user)
    read = _breakdown_read(structure, resolved.breakdown, groups)
    gross_monthly, gross_annual = resolved.gross_figures()
    read.gross_monthly = gross_monthly
    read.gross_annual = gross_annual
    read.earnings_monthly = resolved.earnings_monthly
    read.warnings = resolved.warnings
    return read


def _breakdown_read(
    structure: SalaryStructure, breakdown: SalaryBreakdown, groups: dict
) -> SalaryBreakdownRead:
    return SalaryBreakdownRead(
        structure_id=structure.id,
        structure_name=structure.name,
        ctc_annual=breakdown.ctc_annual,
        annual_total_in_ctc=breakdown.annual_total_in_ctc,
        monthly_total_in_ctc=breakdown.monthly_total_in_ctc,
        components=[
            ResolvedComponentRead(
                code=c.code,
                name=c.name,
                component_type=c.component_type,
                annual_amount=c.annual_amount,
                monthly_amount=c.monthly_amount,
                taxable=c.taxable,
                part_of_ctc=c.part_of_ctc,
                pro_rate_on_lop=c.pro_rate_on_lop,
                counts_toward_pf=c.counts_toward_pf,
                counts_toward_esi=c.counts_toward_esi,
                display_order=c.display_order,
                is_manual=c.is_manual,
                wage_group=salary_service.group_of(groups, c.code, c.component_type),
            )
            for c in breakdown.components
        ],
        totals=salary_service.type_totals(
            [(c.component_type, c.monthly_amount, c.annual_amount) for c in breakdown.components]
        ),
        group_totals=salary_service.group_totals(
            [
                (
                    salary_service.group_of(groups, c.code, c.component_type),
                    c.monthly_amount,
                    c.annual_amount,
                )
                for c in breakdown.components
            ]
        ),
    )


@router.get("/salary-components/standard", response_model=StandardStatusRead)
async def standard_setup_status(user: CurrentUser, session: DbSession) -> StandardStatusRead:
    """Whether the workspace has the Salary Master template's wage types and names."""
    return await standard_setup.standard_status(session, user)


@router.post("/salary-components/standard", response_model=StandardSetupRead)
async def install_standard_setup(
    user: CurrentUser, session: DbSession, request: Request
) -> StandardSetupRead:
    """Add India's usual wage types the workspace lacks, and a standard structure if it has none.

    Nothing already set up is changed. See `standard_setup` for the rules.
    """
    return await standard_setup.install_standard(session, user, request)


# ── Bulk upload ─────────────────────────────────────────────────────────────


@router.get("/salaries/import/template")
async def salary_import_template(user: CurrentUser, session: DbSession) -> Response:
    """An Excel sheet to fill: current people, their structure, a column per wage type."""
    data = await salary_import.template(session, user)
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="salary-upload-template.xlsx"'},
    )


@router.post("/salaries/export")
async def salary_export(
    payload: SalaryExportInput, user: CurrentUser, session: DbSession, request: Request
) -> Response:
    """The chosen people's present salary as an Excel sheet, in the upload layout."""
    data, _ = await salary_import.export_present(session, user, payload.employee_ids, request)
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="EmployeeSalaryMaster.xlsx"'},
    )


@router.post("/salaries/import", response_model=SalaryImportRead)
async def salary_import_upload(
    file: Annotated[UploadFile, File()],
    user: CurrentUser,
    session: DbSession,
    request: Request,
    apply: Annotated[bool, Form()] = False,
) -> SalaryImportRead:
    """Check a filled sheet row by row; with `apply`, save every row in one go.

    Nothing is saved while any row has an error.
    """
    data = await file.read(salary_import.MAX_BYTES + 1)
    return await salary_import.upload(
        session, user, data, file.filename or "salaries.xlsx", apply=apply, request=request
    )


# ── Employee salary ─────────────────────────────────────────────────────────


@router.get("/salaries", response_model=Page[SalaryOverviewRead])
async def salary_overview(
    user: CurrentUser,
    session: DbSession,
    search: str | None = Query(default=None, max_length=128),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
) -> Page[SalaryOverviewRead]:
    """Employees with the salary each is on today."""
    rows, total = await salary_service.salary_overview(
        session, user, search=search, page=page, page_size=page_size
    )
    return Page[SalaryOverviewRead](items=rows, total=total, page=page, page_size=page_size)


@router.post(
    "/employees/{employee_id}/salary",
    response_model=EmployeeSalaryRead,
    status_code=status.HTTP_201_CREATED,
)
async def assign_salary(
    employee_id: uuid.UUID,
    payload: EmployeeSalaryCreate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> EmployeeSalaryRead:
    """Assign or revise a salary, effective-dated.

    Materialises the resolved amounts so a later structure edit cannot change
    what this employee was actually paid.
    """
    salary = await salary_service.assign_salary(session, user, employee_id, payload, request)
    read = EmployeeSalaryRead.model_validate(salary)
    read.paid_months_affected = await salary_service.paid_months_since(
        session, user, employee_id, payload.effective_from
    )
    return read


@router.get("/employees/{employee_id}/salary/history", response_model=list[EmployeeSalaryRead])
async def salary_history(
    employee_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[EmployeeSalaryRead]:
    rows = await salary_service.salary_history(session, user, employee_id)
    return [EmployeeSalaryRead.model_validate(row) for row in rows]


# ── Salary master ───────────────────────────────────────────────────────────


@router.get("/employees/{employee_id}/salary/revisions", response_model=list[SalaryRevisionRead])
async def salary_revisions(
    employee_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[SalaryRevisionRead]:
    """One person's revisions, newest first, each saying whether payroll has used it."""
    return await salary_master.list_revisions(session, user, employee_id)


@router.get("/employees/{employee_id}/salary/trail", response_model=list[SalaryTrailEntryRead])
async def salary_trail(
    employee_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[SalaryTrailEntryRead]:
    """Every recorded change to this person's salary, newest first, with who made it."""
    return await salary_master.salary_trail(session, user, employee_id)


@router.put("/employees/{employee_id}/salary/{salary_id}", response_model=EmployeeSalaryRead)
async def update_salary(
    employee_id: uuid.UUID,
    salary_id: uuid.UUID,
    payload: EmployeeSalaryUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> EmployeeSalaryRead:
    """Correct a revision payroll has not used yet (409 once it has)."""
    change = await salary_master.update_salary(
        session, user, employee_id, salary_id, payload, request
    )
    read = EmployeeSalaryRead.model_validate(change.salary)
    read.paid_months_affected = change.paid_months_affected
    read.runs_to_recompute = change.runs_to_recompute
    read.carried_forward_to = change.carried_forward_to
    return read


@router.delete("/employees/{employee_id}/salary/{salary_id}", response_model=SalaryDeleteRead)
async def delete_salary(
    employee_id: uuid.UUID,
    salary_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> SalaryDeleteRead:
    """Delete a revision payroll has not used yet (409 once it has)."""
    runs = await salary_master.delete_salary(session, user, employee_id, salary_id, request)
    return SalaryDeleteRead(runs_to_recompute=runs)


@router.post(
    "/employees/{employee_id}/salary/{salary_id}/attachment", response_model=SalaryRevisionRead
)
async def attach_salary_file(
    employee_id: uuid.UUID,
    salary_id: uuid.UUID,
    file: Annotated[UploadFile, File()],
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> SalaryRevisionRead:
    """Attach the letter or approval behind a revision: a PDF, PNG or JPEG."""
    data = await file.read(settings.MAX_UPLOAD_BYTES + 1)
    salary = await salary_master.attach_file(
        session, user, employee_id, salary_id, filename=file.filename, data=data, request=request
    )
    locks = await salary_master.revision_locks(session, user, [salary.id])
    groups = await salary_service.wage_group_map(session, user)
    return salary_master.revision_read(salary, locks.get(salary.id), groups)


@router.get("/employees/{employee_id}/salary/{salary_id}/attachment")
async def download_salary_file(
    employee_id: uuid.UUID,
    salary_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> Response:
    filename, mime, data = await salary_master.read_attachment(
        session, user, employee_id, salary_id, request
    )
    return Response(
        content=data,
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete(
    "/employees/{employee_id}/salary/{salary_id}/attachment",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def remove_salary_file(
    employee_id: uuid.UUID,
    salary_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> None:
    await salary_master.remove_attachment(session, user, employee_id, salary_id, request)


# ── Grades and pay bands ────────────────────────────────────────────────────

ADMIN_ONLY = [Depends(require_roles(UserRole.ADMIN))]


@router.get("/pay-grades", response_model=list[PayGradeRead])
async def list_pay_grades(
    user: CurrentUser, session: DbSession, include_inactive: bool = False
) -> list[PayGradeRead]:
    rows = await grade_service.list_grades(session, user, include_inactive=include_inactive)
    return [
        PayGradeRead.model_validate(grade).model_copy(update={"headcount": count})
        for grade, count in rows
    ]


@router.post(
    "/pay-grades",
    response_model=PayGradeRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=ADMIN_ONLY,
)
async def create_pay_grade(
    payload: PayGradeCreate, user: CurrentUser, session: DbSession, request: Request
) -> PayGradeRead:
    row = await grade_service.create_grade(session, user, payload, request=request)
    return PayGradeRead.model_validate(row)


@router.patch("/pay-grades/{grade_id}", response_model=PayGradeRead, dependencies=ADMIN_ONLY)
async def update_pay_grade(
    grade_id: uuid.UUID,
    payload: PayGradeUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> PayGradeRead:
    row = await grade_service.update_grade(session, user, grade_id, payload, request=request)
    return PayGradeRead.model_validate(row)


@router.get("/pay-grades/positioning")
async def pay_positioning(user: CurrentUser, session: DbSession) -> dict:
    """Each current person's CTC against their grade's band, with the compa-ratio."""
    return await grade_service.positioning(session, user)
