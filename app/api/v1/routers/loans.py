"""Loans, advances and reimbursement components (slice 14).

Thin: parse, gate, delegate to `app.services.loans.service`, serialise. The
employee's own routes (`/loans/mine`, claims, positions) resolve the person from
the token through `app.core.permissions`; payroll routes are role-gated.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field

from app.core import permissions
from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.loans import (
    ClaimStatus,
    InterestType,
    LoanAccount,
    LoanStatus,
    LoanType,
    ReimbursementClaim,
)
from app.models.user import UserRole
from app.services.filings import support
from app.services.loans import service as loan_service

router = APIRouter(tags=["loans"])

PAYROLL_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN, UserRole.HR_OPS)
ADMIN_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN)


# ── Contracts ───────────────────────────────────────────────────────────────


class SchemeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    loan_type: LoanType
    interest_type: InterestType
    interest_rate: Decimal = Field(default=Decimal(0), ge=0, le=100)
    max_tenure_months: int = Field(gt=0, le=360)
    max_amount: Decimal | None = Field(default=None, gt=0)
    max_amount_as_multiple_of_salary: Decimal | None = Field(default=None, gt=0)
    eligibility_min_service_months: int = Field(default=0, ge=0)
    moratorium_months: int = Field(default=0, ge=0)
    accrue_interest_during_moratorium: bool = True
    # Never defaulted to zero in practice: a loan is not more urgent than
    # someone's salary, and the floor is what stops a recovery taking it all.
    minimum_net_pay: Decimal = Field(default=Decimal(0), ge=0)
    allow_partial_recovery: bool = False
    requires_approval: bool = True


class SchemeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    loan_type: LoanType
    interest_type: InterestType
    interest_rate: Decimal
    max_tenure_months: int
    max_amount: Decimal | None
    max_amount_as_multiple_of_salary: Decimal | None
    eligibility_min_service_months: int
    moratorium_months: int
    minimum_net_pay: Decimal
    allow_partial_recovery: bool
    requires_approval: bool
    active: bool


class OwnLoanApply(BaseModel):
    scheme_id: uuid.UUID
    principal: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    tenure_months: int = Field(gt=0, le=360)
    purpose: str | None = Field(default=None, max_length=2000)


class LoanApply(OwnLoanApply):
    employee_id: uuid.UUID


class LoanRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    employee_code: str | None = None
    employee_name: str | None = None
    scheme_id: uuid.UUID
    scheme_name: str | None = None
    status: LoanStatus
    principal: Decimal
    interest_rate: Decimal
    interest_type: InterestType
    tenure_months: int
    emi_amount: Decimal
    outstanding_principal: Decimal
    carried_forward: Decimal
    purpose: str | None
    decision_note: str | None
    applied_at: datetime | None
    approved_at: datetime | None
    disbursed_at: datetime | None
    closed_at: datetime | None


class RepaymentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    period: str
    amount: Decimal
    principal_component: Decimal
    interest_component: Decimal
    closing_balance: Decimal
    is_skipped: bool
    is_prepayment: bool
    reason: str


class LoanDetailRead(BaseModel):
    loan: LoanRead
    schedule: dict[str, Any] | None
    repayments: list[RepaymentRead]


class LoanDecision(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


class LoanRejection(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class LoanDisbursal(BaseModel):
    disbursed_on: date | None = None


class PrepaymentRequest(BaseModel):
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    mode: str = Field(pattern="^(reduce_emi|reduce_tenure)$")


class ComponentCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z0-9_]+$")
    name: str = Field(min_length=1, max_length=128)
    annual_entitlement: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    exemption_basis: str | None = Field(default=None, max_length=255)
    bill_required: bool = True
    carry_forward_within_year: bool = True
    reminder_months_before_year_end: int = Field(default=2, ge=0, le=11)


class ComponentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    annual_entitlement: Decimal
    exemption_basis: str | None
    bill_required: bool
    carry_forward_within_year: bool
    reminder_months_before_year_end: int
    active: bool


class ClaimVerify(BaseModel):
    status: ClaimStatus
    accepted_amount: Decimal = Field(default=Decimal(0), ge=0)
    verification_note: str | None = Field(default=None, max_length=2000)


class ClaimRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    employee_code: str | None = None
    employee_name: str | None = None
    component_code: str
    component_name: str | None = None
    financial_year: str
    amount: Decimal
    accepted_amount: Decimal
    status: ClaimStatus
    bill_date: date | None
    bill_reference: str | None
    has_bill: bool = False
    verification_note: str | None
    verified_at: datetime | None
    created_at: datetime


def _loan_read(
    loan: LoanAccount, label: tuple[str, str] | None = None, scheme_name: str | None = None
) -> LoanRead:
    read = LoanRead.model_validate(loan)
    if label:
        read.employee_code, read.employee_name = label
    read.scheme_name = scheme_name
    return read


def _claim_read(
    claim: ReimbursementClaim,
    label: tuple[str, str] | None = None,
    component_name: str | None = None,
) -> ClaimRead:
    read = ClaimRead.model_validate(claim)
    read.has_bill = bool(claim.bill_storage_key)
    if label:
        read.employee_code, read.employee_name = label
    read.component_name = component_name
    return read


async def _one_loan(session, user, loan_id: uuid.UUID) -> LoanRead:
    rows = await loan_service.list_loans(session, user)
    for loan, label, scheme_name in rows:
        if loan.id == loan_id:
            return _loan_read(loan, label, scheme_name)
    raise AssertionError("the loan was just read")  # pragma: no cover


# ── Schemes ─────────────────────────────────────────────────────────────────


@router.get("/loans/schemes", response_model=list[SchemeRead])
async def list_schemes(user: CurrentUser, session: DbSession) -> list[SchemeRead]:
    """The loan catalogue: what is offered, on what terms. About nobody in particular."""
    rows = await loan_service.list_schemes(session, user)
    return [SchemeRead.model_validate(row) for row in rows]


@router.post(
    "/loans/schemes",
    response_model=SchemeRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def create_scheme(
    payload: SchemeCreate, user: CurrentUser, session: DbSession, request: Request
) -> SchemeRead:
    scheme = await loan_service.create_scheme(
        session, user, payload.model_dump(), request=request
    )
    await session.commit()
    await session.refresh(scheme)
    return SchemeRead.model_validate(scheme)


# ── Loans: the employee's own ───────────────────────────────────────────────


@router.get("/loans/mine", response_model=list[LoanRead])
async def my_loans(user: CurrentUser, session: DbSession) -> list[LoanRead]:
    """The caller's own loans, found from user.employee_id."""
    own = permissions.own_employee_id(user)
    rows = await loan_service.list_loans(session, user, employee_id=own)
    return [_loan_read(loan, label, name) for loan, label, name in rows]


@router.post("/loans/mine", response_model=LoanRead, status_code=status.HTTP_201_CREATED)
async def apply_for_own_loan(
    payload: OwnLoanApply, user: CurrentUser, session: DbSession, request: Request
) -> LoanRead:
    """Apply for a loan for yourself."""
    own = permissions.own_employee_id(user)
    loan = await loan_service.apply_for_loan(
        session,
        user,
        employee_id=own,
        scheme_id=payload.scheme_id,
        principal=payload.principal,
        tenure_months=payload.tenure_months,
        purpose=payload.purpose,
        request=request,
    )
    await session.commit()
    return await _one_loan(session, user, loan.id)


# ── Loans: payroll ──────────────────────────────────────────────────────────


@router.post(
    "/loans",
    response_model=LoanRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*PAYROLL_ROLES))],
)
async def apply(
    payload: LoanApply, user: CurrentUser, session: DbSession, request: Request
) -> LoanRead:
    """Apply on somebody's behalf. The terms are snapshotted from the scheme now."""
    loan = await loan_service.apply_for_loan(
        session,
        user,
        employee_id=payload.employee_id,
        scheme_id=payload.scheme_id,
        principal=payload.principal,
        tenure_months=payload.tenure_months,
        purpose=payload.purpose,
        request=request,
    )
    await session.commit()
    return await _one_loan(session, user, loan.id)


@router.get(
    "/loans",
    response_model=list[LoanRead],
    dependencies=[Depends(require_roles(*PAYROLL_ROLES))],
)
async def list_loans(
    user: CurrentUser,
    session: DbSession,
    employee_id: uuid.UUID | None = None,
    loan_status: LoanStatus | None = None,
) -> list[LoanRead]:
    rows = await loan_service.list_loans(
        session, user, employee_id=employee_id, loan_status=loan_status
    )
    return [_loan_read(loan, label, name) for loan, label, name in rows]


@router.get("/loans/{loan_id}", response_model=LoanDetailRead)
async def loan_detail(loan_id: uuid.UUID, user: CurrentUser, session: DbSession) -> LoanDetailRead:
    """A loan with its schedule and recovery ledger — the borrower's own, or payroll's.

    The schedule is the stored one payroll recovers against, so the two cannot
    drift apart. Skipped instalments are ledger rows with their reason.
    """
    loan, repayments = await loan_service.loan_detail(session, user, loan_id)
    return LoanDetailRead(
        loan=await _one_loan(session, user, loan.id),
        schedule=loan.schedule or None,
        repayments=[RepaymentRead.model_validate(row) for row in repayments],
    )


@router.post(
    "/loans/{loan_id}/approve",
    response_model=LoanRead,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def approve_loan(
    loan_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    request: Request,
    payload: LoanDecision | None = None,
) -> LoanRead:
    await loan_service.decide_loan(
        session, user, loan_id, approve=True, note=payload.note if payload else None,
        request=request,
    )
    await session.commit()
    return await _one_loan(session, user, loan_id)


@router.post(
    "/loans/{loan_id}/reject",
    response_model=LoanRead,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def reject_loan(
    loan_id: uuid.UUID,
    payload: LoanRejection,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> LoanRead:
    await loan_service.decide_loan(
        session, user, loan_id, approve=False, note=payload.reason, request=request
    )
    await session.commit()
    return await _one_loan(session, user, loan_id)


@router.post(
    "/loans/{loan_id}/disburse",
    response_model=LoanRead,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def disburse_loan(
    loan_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    request: Request,
    payload: LoanDisbursal | None = None,
) -> LoanRead:
    await loan_service.disburse_loan(
        session, user, loan_id, disbursed_on=payload.disbursed_on if payload else None,
        request=request,
    )
    await session.commit()
    return await _one_loan(session, user, loan_id)


@router.post(
    "/loans/{loan_id}/prepay",
    dependencies=[Depends(require_roles(*PAYROLL_ROLES))],
)
async def prepay(
    loan_id: uuid.UUID,
    payload: PrepaymentRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    """Part-prepay and reschedule, reducing the EMI or the tenure as asked."""
    schedule = await loan_service.prepay_loan(
        session, user, loan_id, amount=payload.amount, mode=payload.mode, request=request
    )
    await session.commit()
    return schedule


# ── Reimbursement components ────────────────────────────────────────────────


@router.get("/reimbursements/components", response_model=list[ComponentRead])
async def list_components(user: CurrentUser, session: DbSession) -> list[ComponentRead]:
    """The reimbursement catalogue: entitlements and what each needs. Nobody in particular."""
    rows = await loan_service.list_components(session, user)
    return [ComponentRead.model_validate(row) for row in rows]


@router.post(
    "/reimbursements/components",
    response_model=ComponentRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def create_component(
    payload: ComponentCreate, user: CurrentUser, session: DbSession, request: Request
) -> ComponentRead:
    component = await loan_service.create_component(
        session, user, payload.model_dump(), request=request
    )
    await session.commit()
    await session.refresh(component)
    return ComponentRead.model_validate(component)


@router.post(
    "/reimbursements/reminders/run",
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def run_reminders(user: CurrentUser, session: DbSession) -> dict[str, int]:
    """Warn about entitlement that will become taxable at year end.

    What a monthly scheduler calls; there is none in this build.
    """
    raised = await loan_service.send_unclaimed_entitlement_reminders(
        session, user.tenant_id, today=date.today()
    )
    return {"notifications_raised": raised}


# ── Reimbursement claims ────────────────────────────────────────────────────


@router.post(
    "/reimbursements/claims",
    response_model=ClaimRead,
    status_code=status.HTTP_201_CREATED,
)
async def submit_claim(
    user: CurrentUser,
    session: DbSession,
    request: Request,
    component_code: str = Form(min_length=1, max_length=64),
    financial_year: str = Form(pattern=r"^\d{4}-\d{4}$"),
    amount: Decimal = Form(gt=0, max_digits=14, decimal_places=2),
    bill_date: date | None = Form(default=None),
    bill_reference: str | None = Form(default=None, max_length=128),
    employee_id: uuid.UUID | None = Form(default=None),
    file: UploadFile | None = File(default=None),
) -> ClaimRead:
    """Claim against your own entitlement, with the bill (PDF, PNG or JPEG).

    Payroll may pass `employee_id` to claim on someone's behalf.
    """
    target = permissions.resolve_employee(
        user, employee_id, detail="You can only claim reimbursements for yourself"
    )
    bill = await file.read(support.MAX_UPLOAD_BYTES + 1) if file is not None else None
    claim = await loan_service.submit_claim(
        session,
        user,
        employee_id=target,
        component_code=component_code,
        financial_year=financial_year,
        amount=amount,
        bill_date=bill_date,
        bill_reference=bill_reference,
        bill=bill or None,
        request=request,
    )
    await session.commit()
    await session.refresh(claim)
    return _claim_read(claim)


@router.get("/reimbursements/claims", response_model=list[ClaimRead])
async def list_claims(
    user: CurrentUser,
    session: DbSession,
    financial_year: str | None = None,
    claim_status: ClaimStatus | None = Query(default=None, alias="status"),
    employee_id: uuid.UUID | None = None,
) -> list[ClaimRead]:
    """Claims: your own, or everybody's for payroll."""
    rows = await loan_service.list_claims(
        session,
        user,
        employee_id=employee_id,
        financial_year=financial_year,
        claim_status=claim_status,
    )
    return [_claim_read(claim, label, name) for claim, label, name in rows]


@router.patch(
    "/reimbursements/claims/{claim_id}",
    response_model=ClaimRead,
    dependencies=[Depends(require_roles(*PAYROLL_ROLES))],
)
async def verify_claim(
    claim_id: uuid.UUID,
    payload: ClaimVerify,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> ClaimRead:
    """Verify a bill, in full or in part. A rejection or partial needs a note."""
    claim = await loan_service.verify_claim(
        session,
        user,
        claim_id,
        decision=payload.status,
        accepted_amount=payload.accepted_amount,
        note=payload.verification_note,
        request=request,
    )
    await session.commit()
    await session.refresh(claim)
    return _claim_read(claim)


@router.get("/reimbursements/claims/{claim_id}/bill")
async def claim_bill(
    claim_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> Response:
    data, mime, name = await loan_service.claim_bill(session, user, claim_id, request=request)
    await session.commit()
    return Response(content=data, media_type=mime, headers=support.attachment_headers(name))


@router.get("/reimbursements/position")
async def entitlement_position(
    user: CurrentUser,
    session: DbSession,
    financial_year: str = Query(pattern=r"^\d{4}-\d{4}$"),
    employee_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """Where you stand against each component, at any point in the year.

    Available all year, not only at year end — the taxable-balance surprise is
    avoidable, and only avoidable in advance.
    """
    target = permissions.resolve_employee(
        user, employee_id, detail="You can only see your own entitlements"
    )
    return await loan_service.entitlement_positions(
        session, user, employee_id=target, financial_year=financial_year
    )
