"""Back-dated salary revision and arrears (slice 12)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.core.deps import CurrentUser, DbSession, require_roles, tenant_scoped
from app.models.arrears import (
    ArrearBatch,
    ArrearStatus,
    RevisionType,
    SalaryRevision,
)
from app.models.payroll import EmployeeSalary
from app.models.user import UserRole
from app.schemas.payroll_ops import IssuedDocumentRead
from app.services.arrears import relief_service
from app.services.arrears import service as arrears_service
from app.services.filings import support

router = APIRouter(tags=["arrears"])

PAYROLL_ROLES = (UserRole.ADMIN, UserRole.SUB_ADMIN)
ADMIN_ROLES = (UserRole.ADMIN,)


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
    employee_code: str | None = None
    employee_name: str | None = None


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
    computed_by_user_id: uuid.UUID | None
    approved_at: datetime | None
    approved_by_user_id: uuid.UUID | None
    additional_approved_at: datetime | None
    additional_approved_by_user_id: uuid.UUID | None
    paid_in_run_id: uuid.UUID | None
    awaiting_additional_approval: bool = False

    @classmethod
    def from_model(cls, batch: ArrearBatch) -> ArrearBatchRead:
        read = cls.model_validate(batch)
        read.awaiting_additional_approval = (
            batch.status is ArrearStatus.PENDING_APPROVAL
            and batch.requires_additional_approval
            and batch.approved_by_user_id is not None
            and batch.additional_approved_by_user_id is None
        )
        return read


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


class CancelRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class PriorYearIncome(BaseModel):
    financial_year: str = Field(pattern=r"^\d{4}-\d{2,4}$")
    regime: str | None = Field(default=None, pattern="^(old|new)$")
    original_taxable_income: Decimal = Field(ge=0, max_digits=14, decimal_places=2)


class ReliefRequest(BaseModel):
    employee_id: uuid.UUID
    receipt_financial_year: str | None = None
    regime: str = Field(pattern="^(old|new)$")
    receipt_year_taxable_income: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    prior_years: list[PriorYearIncome] = Field(default_factory=list, max_length=20)


class ReliefRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    employee_code: str | None = None
    employee_name: str | None = None
    arrear_batch_id: uuid.UUID
    financial_year: str
    arrear_amount: Decimal
    tax_without_relief: Decimal
    tax_with_spreading: Decimal
    relief_amount: Decimal
    is_beneficial: bool
    working: dict
    form_10e_letter_id: uuid.UUID | None


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

    guardrails = await arrears_service.preview_guardrails(
        session,
        user,
        employee_ids=[item.employee_id for item in payload.revisions],
        effective_from=payload.effective_from,
        legal_entity_id=payload.legal_entity_id,
    )
    labels = await arrears_service.employee_labels(
        session, user, {item.employee_id for item in payload.revisions}
    )
    # Months paid on the previous payroll (imported as paid): never recomputed.
    not_recomputed = await arrears_service.imported_periods(
        session, user, legal_entity_id=payload.legal_entity_id, from_date=payload.effective_from
    )

    return {
        "guardrails": guardrails,
        "not_recomputed": not_recomputed,
        "employees": {
            str(employee_id): {"employee_code": code, "employee_name": name}
            for employee_id, (code, name) in labels.items()
        },
        "summary": summary.as_dict(),
        "per_employee": {
            employee_id: [
                {
                    "period": arrear.period,
                    "earnings_arrear": str(arrear.earnings_arrear),
                    "employee_deduction_arrear": str(arrear.employee_deduction_arrear),
                    "employer_contribution_arrear": str(arrear.employer_contribution_arrear),
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
    return ArrearBatchRead.from_model(batch)


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
    return [ArrearBatchRead.from_model(row) for row in rows]


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
    rows = await arrears_service.batch_lines(session, user, batch_id, employee_id=employee_id)
    out = []
    for line, label in rows:
        read = ArrearLineRead.model_validate(line)
        if label:
            read.employee_code, read.employee_name = label
        out.append(read)
    return out


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
    return ArrearBatchRead.from_model(batch)


@router.post(
    "/arrears/batches/{batch_id}/cancel",
    response_model=ArrearBatchRead,
    dependencies=[Depends(require_roles(*PAYROLL_ROLES))],
)
async def cancel(
    batch_id: uuid.UUID,
    payload: CancelRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> ArrearBatchRead:
    """Discard a batch that is wrong, before a run pays it."""
    batch = await arrears_service.cancel_batch(
        session, user, batch_id=batch_id, reason=payload.reason, request=request
    )
    await session.commit()
    await session.refresh(batch)
    return ArrearBatchRead.from_model(batch)


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


# ── Section 89(1) relief and Form 10E ───────────────────────────────────────


async def _relief_read(session, user, record) -> ReliefRead:
    read = ReliefRead.model_validate(record)
    labels = await arrears_service.employee_labels(session, user, {record.employee_id})
    if record.employee_id in labels:
        read.employee_code, read.employee_name = labels[record.employee_id]
    return read


@router.get(
    "/arrears/batches/{batch_id}/relief-years",
    dependencies=[Depends(require_roles(*PAYROLL_ROLES))],
)
async def relief_years(
    batch_id: uuid.UUID, employee_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    """Which earlier years an employee's arrear relates to, and how much of it each."""
    return await relief_service.relief_years(session, user, batch_id, employee_id)


@router.post(
    "/arrears/batches/{batch_id}/relief",
    response_model=ReliefRead,
    dependencies=[Depends(require_roles(*PAYROLL_ROLES))],
)
async def compute_relief(
    batch_id: uuid.UUID,
    payload: ReliefRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> ReliefRead:
    """Section 89(1) relief for one employee, each year priced by its own rule set.

    A relief of zero is a correct answer when spreading back is not beneficial.
    The result needs review by a tax advisor before the employee files Form 10E.
    """
    record = await relief_service.compute_employee_relief(
        session,
        user,
        batch_id=batch_id,
        employee_id=payload.employee_id,
        regime=payload.regime,
        receipt_year_taxable_income=payload.receipt_year_taxable_income,
        prior_years=[item.model_dump() for item in payload.prior_years],
        request=request,
    )
    await session.commit()
    await session.refresh(record)
    return await _relief_read(session, user, record)


@router.get(
    "/arrears/reliefs",
    response_model=list[ReliefRead],
    dependencies=[Depends(require_roles(*PAYROLL_ROLES))],
)
async def list_reliefs(
    user: CurrentUser,
    session: DbSession,
    batch_id: uuid.UUID | None = None,
    employee_id: uuid.UUID | None = None,
) -> list[ReliefRead]:
    rows = await relief_service.list_reliefs(
        session, user, batch_id=batch_id, employee_id=employee_id
    )
    return [await _relief_read(session, user, row) for row in rows]


@router.post(
    "/arrears/reliefs/{relief_id}/form-10e",
    response_model=IssuedDocumentRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*PAYROLL_ROLES))],
)
async def issue_form_10e(
    relief_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> IssuedDocumentRead:
    letter = await relief_service.issue_form_10e(session, user, relief_id, request=request)
    await session.commit()
    await session.refresh(letter)
    return IssuedDocumentRead.from_model(letter)


@router.get(
    "/arrears/reliefs/{relief_id}/form-10e/pdf",
    dependencies=[Depends(require_roles(*PAYROLL_ROLES))],
)
async def form_10e_pdf(relief_id: uuid.UUID, user: CurrentUser, session: DbSession) -> Response:
    data, name = await relief_service.form_10e_pdf(session, user, relief_id)
    return Response(
        content=data, media_type="application/pdf", headers=support.attachment_headers(name)
    )


# ── For the payroll run ─────────────────────────────────────────────────────


@router.get(
    "/arrears/payable",
    dependencies=[Depends(require_roles(*PAYROLL_ROLES))],
)
async def payable(
    user: CurrentUser, session: DbSession, legal_entity_id: uuid.UUID | None = None
) -> list[dict[str, Any]]:
    """Approved arrears not yet paid, per employee and component.

    What the next payroll run pays. The run itself is another module's; this is
    the list it reads.
    """
    return await arrears_service.payable_arrears(session, user, legal_entity_id=legal_entity_id)
