"""Back-dated salary revision and arrears (slice 12)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.core.deps import CurrentUser, DbSession, require_roles, tenant_scoped
from app.models.arrears import (
    ArrearBatch,
    ArrearLine,
    ArrearStatus,
    RevisionType,
    SalaryRevision,
)
from app.models.payroll import EmployeeSalary
from app.models.user import UserRole
from app.services.arrears import service as arrears_service

router = APIRouter(tags=["arrears"])

PAYROLL_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN, UserRole.HR_OPS)
ADMIN_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN)


# ── Contracts ───────────────────────────────────────────────────────────────


class RevisionItem(BaseModel):
    employee_id: uuid.UUID
    new_salary_id: uuid.UUID
    previous_salary_id: uuid.UUID | None = None


class RevisionRequest(BaseModel):
    """One request revises one employee or three hundred.

    An increment cycle is a single request with a common effective date, and it
    produces one batch — a payroll lead asked to approve three hundred separate
    batches will approve them without reading any.
    """

    legal_entity_id: uuid.UUID
    effective_from: date
    revision_type: RevisionType
    revisions: list[RevisionItem] = Field(min_length=1)
    reason: str | None = None
    additional_approval_threshold: Decimal | None = Field(default=None, ge=0)


class PreviewRequest(BaseModel):
    legal_entity_id: uuid.UUID
    effective_from: date
    revisions: list[RevisionItem] = Field(min_length=1)


class ArrearLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    period: str
    component_code: str
    component_name: str
    component_type: str
    paid_amount: Decimal
    should_have_been_amount: Decimal
    difference: Decimal
    rule_set_id: uuid.UUID | None


class ArrearBatchRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    legal_entity_id: uuid.UUID
    from_period: str
    to_period: str
    status: ArrearStatus
    employee_count: int
    total_earnings_arrear: Decimal
    total_employee_deduction_arrear: Decimal
    total_employer_contribution_arrear: Decimal
    total_net_payable: Decimal
    affected_filings: list[str]
    requires_additional_approval: bool
    computed_at: datetime | None
    approved_at: datetime | None


class RevisionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    effective_from: date
    revision_type: RevisionType
    reason: str | None
    processed_at: datetime | None
    arrear_batch_id: uuid.UUID | None


class ApproveRequest(BaseModel):
    acknowledge_filings: bool = False


# ── Preview ─────────────────────────────────────────────────────────────────


@router.post(
    "/arrears/preview",
    dependencies=[Depends(require_roles(*PAYROLL_ROLES))],
)
async def preview(
    payload: PreviewRequest,
    user: CurrentUser,
    session: DbSession,
) -> dict[str, Any]:
    """Total liability before anything is written.

    Nothing is persisted. A payroll lead approving a six-figure arrear needs to
    see it before it exists, not after.
    """
    salaries: dict[uuid.UUID, EmployeeSalary] = {}
    for item in payload.revisions:
        salary = await session.scalar(
            tenant_scoped(
                select(EmployeeSalary).where(EmployeeSalary.id == item.new_salary_id),
                EmployeeSalary,
                user,
            )
        )
        if salary is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Salary {item.new_salary_id} not found",
            )
        salaries[item.employee_id] = salary

    per_employee, summary = await arrears_service.preview_arrears(
        session,
        user,
        legal_entity_id=payload.legal_entity_id,
        employee_salaries=salaries,
        effective_from=payload.effective_from,
    )

    return {
        "summary": summary.as_dict(),
        "per_employee": {
            employee_id: [
                {
                    "period": arrear.period,
                    "earnings_arrear": str(arrear.earnings_arrear),
                    "employee_deduction_arrear": str(arrear.employee_deduction_arrear),
                    "employer_contribution_arrear": str(
                        arrear.employer_contribution_arrear
                    ),
                    "net_payable": str(arrear.net_payable),
                    "rule_set_ids": arrear.rule_set_ids,
                    "components": [d.as_dict() for d in arrear.diffs if d.difference],
                    "notes": arrear.notes,
                }
                for arrear in arrears
                if arrear.has_movement
            ]
            for employee_id, arrears in per_employee.items()
        },
    }


# ── Commit ──────────────────────────────────────────────────────────────────


@router.post(
    "/arrears/revisions",
    response_model=ArrearBatchRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def revise(
    payload: RevisionRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> ArrearBatchRead:
    batch = await arrears_service.revise_salary(
        session,
        user,
        legal_entity_id=payload.legal_entity_id,
        revisions=[item.model_dump() for item in payload.revisions],
        effective_from=payload.effective_from,
        revision_type=payload.revision_type,
        reason=payload.reason,
        threshold=payload.additional_approval_threshold,
        request=request,
    )
    await session.commit()
    await session.refresh(batch)
    return ArrearBatchRead.model_validate(batch)


@router.get(
    "/arrears/batches",
    response_model=list[ArrearBatchRead],
    dependencies=[Depends(require_roles(*PAYROLL_ROLES))],
)
async def list_batches(
    user: CurrentUser,
    session: DbSession,
    batch_status: ArrearStatus | None = None,
) -> list[ArrearBatchRead]:
    stmt = select(ArrearBatch).order_by(ArrearBatch.created_at.desc())
    if batch_status is not None:
        stmt = stmt.where(ArrearBatch.status == batch_status)
    rows = (await session.scalars(tenant_scoped(stmt, ArrearBatch, user))).all()
    return [ArrearBatchRead.model_validate(row) for row in rows]


@router.get(
    "/arrears/batches/{batch_id}/lines",
    response_model=list[ArrearLineRead],
    dependencies=[Depends(require_roles(*PAYROLL_ROLES))],
)
async def list_lines(
    batch_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    employee_id: uuid.UUID | None = None,
) -> list[ArrearLineRead]:
    """One line per component per period.

    Never a lump sum: basic and a travel allowance are taxed differently, count
    differently towards PF, and appear on different lines of a return.
    """
    stmt = (
        select(ArrearLine)
        .where(ArrearLine.batch_id == batch_id)
        .order_by(ArrearLine.period, ArrearLine.component_code)
    )
    if employee_id is not None:
        stmt = stmt.where(ArrearLine.employee_id == employee_id)
    rows = (await session.scalars(tenant_scoped(stmt, ArrearLine, user))).all()
    return [ArrearLineRead.model_validate(row) for row in rows]


@router.post(
    "/arrears/batches/{batch_id}/approve",
    response_model=ArrearBatchRead,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def approve(
    batch_id: uuid.UUID,
    payload: ApproveRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> ArrearBatchRead:
    """Sign off for payment in a future run.

    Affected filings must be acknowledged. A revised ECR for a closed month is
    a decision with consequences, and approving past it silently means nobody
    made that decision.
    """
    batch = await arrears_service.approve_batch(
        session,
        user,
        batch_id=batch_id,
        acknowledge_filings=payload.acknowledge_filings,
        request=request,
    )
    await session.commit()
    await session.refresh(batch)
    return ArrearBatchRead.model_validate(batch)


@router.get(
    "/arrears/revisions",
    response_model=list[RevisionRead],
    dependencies=[Depends(require_roles(*PAYROLL_ROLES))],
)
async def list_revisions(
    user: CurrentUser,
    session: DbSession,
    employee_id: uuid.UUID | None = None,
) -> list[RevisionRead]:
    stmt = select(SalaryRevision).order_by(SalaryRevision.effective_from.desc())
    if employee_id is not None:
        stmt = stmt.where(SalaryRevision.employee_id == employee_id)
    rows = (await session.scalars(tenant_scoped(stmt, SalaryRevision, user))).all()
    return [RevisionRead.model_validate(row) for row in rows]
