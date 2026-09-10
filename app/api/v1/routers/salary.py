"""Salary structure and CTC builder endpoints (slice 01).

Everything here is behind the tenant guard and a payroll-capable role. Assigning
or revising a salary writes exactly one audit entry, in the same transaction as
the assignment.
"""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.user import UserRole
from app.schemas.payroll import (
    EmployeeSalaryCreate,
    EmployeeSalaryRead,
    ResolvedComponentRead,
    SalaryBreakdownRead,
    SalaryComponentCreate,
    SalaryComponentRead,
    SalaryStructureCreate,
    SalaryStructureRead,
)
from app.services.payroll import service as salary_service

router = APIRouter(
    tags=["payroll"],
    dependencies=[
        Depends(require_roles(UserRole.TENANT_ADMIN, UserRole.HR_ADMIN, UserRole.HR_OPS))
    ],
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
            )
            for c in breakdown.components
        ],
    )


# ── Employee salary ─────────────────────────────────────────────────────────


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
    return EmployeeSalaryRead.model_validate(salary)


@router.get("/employees/{employee_id}/salary/history", response_model=list[EmployeeSalaryRead])
async def salary_history(
    employee_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[EmployeeSalaryRead]:
    rows = await salary_service.salary_history(session, user, employee_id)
    return [EmployeeSalaryRead.model_validate(row) for row in rows]
