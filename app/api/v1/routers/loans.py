"""Loans, advances and reimbursement components (slice 14)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.core.deps import CurrentUser, DbSession, require_roles, tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.loans import (
    ClaimStatus,
    InterestType,
    LoanAccount,
    LoanRepayment,
    LoanScheme,
    LoanStatus,
    LoanType,
    ReimbursementClaim,
    ReimbursementComponent,
)
from app.models.user import UserRole
from app.services import audit
from app.services.loans import amortisation, reimbursements

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
    approver_roles: list[str] = Field(default_factory=list)


class SchemeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    loan_type: LoanType
    interest_type: InterestType
    interest_rate: Decimal
    max_tenure_months: int
    max_amount: Decimal | None
    minimum_net_pay: Decimal
    allow_partial_recovery: bool
    requires_approval: bool
    active: bool


class LoanApply(BaseModel):
    employee_id: uuid.UUID
    scheme_id: uuid.UUID
    principal: Decimal = Field(gt=0)
    tenure_months: int = Field(gt=0, le=360)
    purpose: str | None = None


class LoanRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    scheme_id: uuid.UUID
    status: LoanStatus
    principal: Decimal
    interest_rate: Decimal
    interest_type: InterestType
    tenure_months: int
    emi_amount: Decimal
    outstanding_principal: Decimal
    carried_forward: Decimal
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


class PrepaymentRequest(BaseModel):
    amount: Decimal = Field(gt=0)
    mode: str = Field(pattern="^(reduce_emi|reduce_tenure)$")


class ComponentCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    annual_entitlement: Decimal = Field(gt=0)
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
    reminder_months_before_year_end: int
    active: bool


class ClaimCreate(BaseModel):
    employee_id: uuid.UUID
    component_code: str = Field(min_length=1, max_length=64)
    financial_year: str = Field(pattern=r"^\d{4}-\d{4}$")
    amount: Decimal = Field(gt=0)
    bill_storage_key: str | None = None
    bill_date: date | None = None
    bill_reference: str | None = Field(default=None, max_length=128)


class ClaimVerify(BaseModel):
    status: ClaimStatus
    accepted_amount: Decimal = Field(default=Decimal(0), ge=0)
    verification_note: str | None = None


class ClaimRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    component_code: str
    financial_year: str
    amount: Decimal
    accepted_amount: Decimal
    status: ClaimStatus
    bill_date: date | None
    verification_note: str | None
    verified_at: datetime | None


# ── Schemes ─────────────────────────────────────────────────────────────────


@router.get(
    "/loans/schemes",
    response_model=list[SchemeRead],
    dependencies=[Depends(require_roles(*PAYROLL_ROLES))],
)
async def list_schemes(user: CurrentUser, session: DbSession) -> list[SchemeRead]:
    rows = (
        await session.scalars(
            tenant_scoped(select(LoanScheme).order_by(LoanScheme.name), LoanScheme, user)
        )
    ).all()
    return [SchemeRead.model_validate(row) for row in rows]


@router.post(
    "/loans/schemes",
    response_model=SchemeRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def create_scheme(
    payload: SchemeCreate, user: CurrentUser, session: DbSession
) -> SchemeRead:
    scheme = LoanScheme(tenant_id=user.tenant_id, **payload.model_dump())
    session.add(scheme)
    await session.commit()
    await session.refresh(scheme)
    return SchemeRead.model_validate(scheme)


# ── Loans ───────────────────────────────────────────────────────────────────


@router.post(
    "/loans",
    response_model=LoanRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*PAYROLL_ROLES))],
)
async def apply(
    payload: LoanApply,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> LoanRead:
    """Apply for a loan. The terms are snapshotted from the scheme now.

    A scheme's rate changing next year must not retroactively alter what an
    existing borrower owes.
    """
    scheme = await session.scalar(
        tenant_scoped(
            select(LoanScheme).where(LoanScheme.id == payload.scheme_id), LoanScheme, user
        )
    )
    if scheme is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Loan scheme not found")

    employee = await session.scalar(
        tenant_scoped(
            select(Employee).where(Employee.id == payload.employee_id), Employee, user
        )
    )
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if scheme.max_amount and payload.principal > Decimal(str(scheme.max_amount)):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"{payload.principal} exceeds the scheme's maximum of "
                f"{scheme.max_amount}."
            ),
        )
    if payload.tenure_months > scheme.max_tenure_months:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"A tenure of {payload.tenure_months} months exceeds the scheme's "
                f"maximum of {scheme.max_tenure_months}."
            ),
        )
    if employee.date_of_joining and scheme.eligibility_min_service_months:
        served = (date.today() - employee.date_of_joining).days // 30
        if served < scheme.eligibility_min_service_months:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"{employee.employee_code} has {served} month(s) of service against "
                    f"the {scheme.eligibility_min_service_months} this scheme requires."
                ),
            )

    try:
        schedule = amortisation.build_schedule(
            principal=payload.principal,
            annual_rate=Decimal(str(scheme.interest_rate)),
            tenure_months=payload.tenure_months,
            interest_type=scheme.interest_type.value,
            moratorium_months=scheme.moratorium_months,
            accrue_interest_during_moratorium=scheme.accrue_interest_during_moratorium,
        )
    except amortisation.AmortisationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    loan = LoanAccount(
        tenant_id=user.tenant_id,
        employee_id=payload.employee_id,
        scheme_id=scheme.id,
        status=LoanStatus.APPLIED if scheme.requires_approval else LoanStatus.APPROVED,
        principal=payload.principal,
        interest_rate=scheme.interest_rate,
        interest_type=scheme.interest_type,
        tenure_months=payload.tenure_months,
        emi_amount=schedule.emi,
        outstanding_principal=payload.principal,
        purpose=payload.purpose,
        applied_at=datetime.now(UTC),
        schedule=schedule.as_dict(),
    )
    session.add(loan)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="loan_account",
        entity_id=loan.id,
        summary=(
            f"{employee.employee_code} applied for {payload.principal} over "
            f"{payload.tenure_months} months at {scheme.interest_rate}% "
            f"({scheme.interest_type.value})"
        ),
        changes={
            "principal": str(payload.principal),
            "emi": str(schedule.emi),
            "total_interest": str(schedule.total_interest),
        },
        request=request,
    )
    await session.commit()
    await session.refresh(loan)
    return LoanRead.model_validate(loan)


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
    stmt = select(LoanAccount).order_by(LoanAccount.created_at.desc())
    if employee_id is not None:
        stmt = stmt.where(LoanAccount.employee_id == employee_id)
    if loan_status is not None:
        stmt = stmt.where(LoanAccount.status == loan_status)
    rows = (await session.scalars(tenant_scoped(stmt, LoanAccount, user))).all()
    return [LoanRead.model_validate(row) for row in rows]


@router.get(
    "/loans/{loan_id}/schedule",
    dependencies=[Depends(require_roles(*PAYROLL_ROLES))],
)
async def get_schedule(
    loan_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    """The amortisation ledger the borrower sees.

    The same one payroll recovers against — stored rather than rebuilt, so the
    two cannot drift apart.
    """
    loan = await session.scalar(
        tenant_scoped(select(LoanAccount).where(LoanAccount.id == loan_id), LoanAccount, user)
    )
    if loan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Loan not found")
    return loan.schedule


@router.get(
    "/loans/{loan_id}/repayments",
    response_model=list[RepaymentRead],
    dependencies=[Depends(require_roles(*PAYROLL_ROLES))],
)
async def list_repayments(
    loan_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[RepaymentRead]:
    """Every recovery, and every skipped one.

    A skipped instalment is a row too — a ledger that records only what was
    recovered cannot explain a gap, and "why was nothing deducted in July" is a
    question the borrower will ask.
    """
    rows = (
        await session.scalars(
            tenant_scoped(
                select(LoanRepayment)
                .where(LoanRepayment.loan_account_id == loan_id)
                .order_by(LoanRepayment.period),
                LoanRepayment,
                user,
            )
        )
    ).all()
    return [RepaymentRead.model_validate(row) for row in rows]


@router.post(
    "/loans/{loan_id}/approve",
    response_model=LoanRead,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def approve_loan(
    loan_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> LoanRead:
    loan = await session.scalar(
        tenant_scoped(select(LoanAccount).where(LoanAccount.id == loan_id), LoanAccount, user)
    )
    if loan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Loan not found")
    if loan.status is not LoanStatus.APPLIED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A loan in status '{loan.status.value}' is not awaiting approval.",
        )

    loan.status = LoanStatus.APPROVED
    loan.approved_by_user_id = user.id
    loan.approved_at = datetime.now(UTC)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.APPROVE,
        entity_type="loan_account",
        entity_id=loan.id,
        summary=f"Approved a loan of {loan.principal}",
        changes={"principal": str(loan.principal)},
        request=request,
    )
    await session.commit()
    await session.refresh(loan)
    return LoanRead.model_validate(loan)


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
    """Part-prepay and reschedule.

    `mode` decides what the borrower actually asked for — a lower instalment,
    or a shorter loan. Both are ordinary requests and picking one for them is
    how a product ends up arguing about what they wanted.
    """
    loan = await session.scalar(
        tenant_scoped(select(LoanAccount).where(LoanAccount.id == loan_id), LoanAccount, user)
    )
    if loan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Loan not found")
    if loan.status not in (LoanStatus.DISBURSED, LoanStatus.ACTIVE):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A loan in status '{loan.status.value}' cannot be prepaid.",
        )

    recovered = (
        await session.scalars(
            tenant_scoped(
                select(LoanRepayment).where(
                    LoanRepayment.loan_account_id == loan.id,
                    LoanRepayment.is_skipped.is_(False),
                ),
                LoanRepayment,
                user,
            )
        )
    ).all()
    remaining = max(1, loan.tenure_months - len(recovered))

    try:
        schedule = amortisation.recompute_after_prepayment(
            outstanding_principal=Decimal(str(loan.outstanding_principal)),
            annual_rate=Decimal(str(loan.interest_rate)),
            remaining_months=remaining,
            prepayment=payload.amount,
            mode=payload.mode,
            current_emi=Decimal(str(loan.emi_amount)),
            interest_type=loan.interest_type.value,
        )
    except amortisation.AmortisationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    loan.outstanding_principal = schedule.principal
    loan.emi_amount = schedule.emi
    loan.tenure_months = len(recovered) + schedule.tenure_months
    loan.schedule = schedule.as_dict()
    session.add(
        LoanRepayment(
            tenant_id=user.tenant_id,
            loan_account_id=loan.id,
            period=date.today().strftime("%Y-%m"),
            amount=payload.amount,
            principal_component=payload.amount,
            interest_component=Decimal(0),
            closing_balance=schedule.principal,
            is_prepayment=True,
            reason=schedule.notes[0] if schedule.notes else "Part-prepayment.",
        )
    )
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="loan_account",
        entity_id=loan.id,
        summary=f"Part-prepayment of {payload.amount} ({payload.mode})",
        changes={
            "amount": str(payload.amount),
            "mode": payload.mode,
            "new_emi": str(schedule.emi),
            "new_tenure_months": schedule.tenure_months,
        },
        request=request,
    )
    await session.commit()
    return schedule.as_dict()


# ── Reimbursement components ────────────────────────────────────────────────


@router.get(
    "/reimbursements/components",
    response_model=list[ComponentRead],
    dependencies=[Depends(require_roles(*PAYROLL_ROLES))],
)
async def list_components(user: CurrentUser, session: DbSession) -> list[ComponentRead]:
    rows = (
        await session.scalars(
            tenant_scoped(
                select(ReimbursementComponent).order_by(ReimbursementComponent.code),
                ReimbursementComponent,
                user,
            )
        )
    ).all()
    return [ComponentRead.model_validate(row) for row in rows]


@router.post(
    "/reimbursements/components",
    response_model=ComponentRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def create_component(
    payload: ComponentCreate, user: CurrentUser, session: DbSession
) -> ComponentRead:
    component = ReimbursementComponent(tenant_id=user.tenant_id, **payload.model_dump())
    session.add(component)
    await session.commit()
    await session.refresh(component)
    return ComponentRead.model_validate(component)


@router.post(
    "/reimbursements/claims",
    response_model=ClaimRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*PAYROLL_ROLES, UserRole.EMPLOYEE))],
)
async def submit_claim(
    payload: ClaimCreate, user: CurrentUser, session: DbSession
) -> ClaimRead:
    component = await session.scalar(
        tenant_scoped(
            select(ReimbursementComponent).where(
                ReimbursementComponent.code == payload.component_code
            ),
            ReimbursementComponent,
            user,
        )
    )
    if component is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"'{payload.component_code}' is not a reimbursement component.",
        )
    if component.bill_required and not payload.bill_storage_key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"{component.name} is exempt only against a bill, and none was "
                "attached."
            ),
        )

    claim = ReimbursementClaim(tenant_id=user.tenant_id, **payload.model_dump())
    session.add(claim)
    await session.commit()
    await session.refresh(claim)
    return ClaimRead.model_validate(claim)


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
    """Verify a bill, in full or in part.

    A rejection or a partial acceptance needs a note: "rejected" with no reason
    is not a decision the employee can act on.
    """
    claim = await session.scalar(
        tenant_scoped(
            select(ReimbursementClaim).where(ReimbursementClaim.id == claim_id),
            ReimbursementClaim,
            user,
        )
    )
    if claim is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Claim not found")

    if payload.status in (ClaimStatus.REJECTED, ClaimStatus.PARTIAL) and not (
        payload.verification_note or ""
    ).strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "A rejection or a partial acceptance needs a note. Without one the "
                "employee cannot tell what to fix."
            ),
        )
    if payload.accepted_amount > Decimal(str(claim.amount)):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"{payload.accepted_amount} cannot be accepted against a claim of "
                f"{claim.amount}."
            ),
        )

    claim.status = payload.status
    claim.accepted_amount = (
        payload.accepted_amount if payload.status is not ClaimStatus.REJECTED else Decimal(0)
    )
    claim.verification_note = payload.verification_note
    claim.verified_by_user_id = user.id
    claim.verified_at = datetime.now(UTC)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="reimbursement_claim",
        entity_id=claim.id,
        summary=(
            f"{claim.component_code} claim of {claim.amount} marked "
            f"{payload.status.value}, {claim.accepted_amount} accepted"
        ),
        changes={
            "status": payload.status.value,
            "accepted_amount": str(claim.accepted_amount),
            "note": payload.verification_note,
        },
        request=request,
    )
    await session.commit()
    await session.refresh(claim)
    return ClaimRead.model_validate(claim)


@router.get(
    "/reimbursements/position",
    dependencies=[Depends(require_roles(*PAYROLL_ROLES, UserRole.EMPLOYEE))],
)
async def entitlement_position(
    user: CurrentUser,
    session: DbSession,
    employee_id: uuid.UUID,
    financial_year: str = Query(pattern=r"^\d{4}-\d{4}$"),
) -> list[dict[str, Any]]:
    """Where the employee stands against each component, at any point.

    Available all year, not only at year end — the taxable-balance surprise is
    avoidable, and only avoidable in advance.
    """
    components = (
        await session.scalars(
            tenant_scoped(
                select(ReimbursementComponent).where(
                    ReimbursementComponent.active.is_(True)
                ),
                ReimbursementComponent,
                user,
            )
        )
    ).all()

    out: list[dict[str, Any]] = []
    for component in components:
        claims = (
            await session.scalars(
                tenant_scoped(
                    select(ReimbursementClaim).where(
                        ReimbursementClaim.employee_id == employee_id,
                        ReimbursementClaim.component_code == component.code,
                        ReimbursementClaim.financial_year == financial_year,
                    ),
                    ReimbursementClaim,
                    user,
                )
            )
        ).all()
        position = reimbursements.position(
            component_code=component.code,
            financial_year=financial_year,
            entitlement=Decimal(str(component.annual_entitlement)),
            claims=[
                {
                    "amount": Decimal(str(c.amount)),
                    "accepted_amount": Decimal(str(c.accepted_amount)),
                    "status": c.status.value,
                }
                for c in claims
            ],
        )
        year_end = date(int(financial_year.split("-")[1]), 3, 31)
        warn, message = reimbursements.should_warn(
            today=date.today(),
            financial_year_end=year_end,
            unclaimed=position.unclaimed,
            reminder_months_before_year_end=component.reminder_months_before_year_end,
        )
        out.append({**position.as_dict(), "should_warn": warn, "warning": message})

    return out
