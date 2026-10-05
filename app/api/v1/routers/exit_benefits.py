"""Gratuity and leave encashment endpoints (slice 08)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.core import permissions
from app.core.deps import CurrentUser, DbSession, require_roles, tenant_scoped
from app.models.audit import AuditAction
from app.models.exit_benefits import (
    EncashmentPolicy,
    EncashmentStatus,
    ExitReason,
    GratuityAccrual,
    GratuityConfig,
    GratuityPayout,
    ServiceRounding,
)
from app.models.user import UserRole
from app.services import audit
from app.services.exit_benefits import service as exit_service

router = APIRouter(tags=["exit-benefits"])

PAYROLL_ROLES = (UserRole.ADMIN, UserRole.SUB_ADMIN)


# ── Contracts ───────────────────────────────────────────────────────────────


class GratuityConfigUpsert(BaseModel):
    """Every field is a number the SME sets. None is a default in code."""

    eligibility_years: int = Field(ge=0, le=20)
    eligibility_days_in_final_year: int | None = Field(
        default=None,
        ge=1,
        le=365,
        description=(
            "Days served in a partial final year that count it as complete. Null "
            "switches the treatment off. Contested ground — a tenant's counsel "
            "decides, and this codebase takes no position."
        ),
    )
    formula_basis: list[str] = Field(min_length=1)
    days_per_year_divisor: int = Field(gt=0, le=31)
    days_per_year: Decimal = Field(gt=0)
    max_exempt_amount: Decimal = Field(ge=0)
    max_payable_amount: Decimal | None = None
    service_rounding: ServiceRounding = ServiceRounding.NEAREST_YEAR_SIX_MONTH_RULE
    unpaid_leave_break_threshold_days: int | None = None
    include_notice_period_in_service: bool = True
    eligibility_waived_reasons: list[str] = Field(
        default_factory=lambda: [ExitReason.DEATH.value, ExitReason.DISABLEMENT.value]
    )


class GratuityConfigRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    eligibility_years: int
    eligibility_days_in_final_year: int | None
    formula_basis: list[str]
    days_per_year_divisor: int
    days_per_year: Decimal
    max_exempt_amount: Decimal
    max_payable_amount: Decimal | None
    service_rounding: ServiceRounding
    unpaid_leave_break_threshold_days: int | None
    include_notice_period_in_service: bool
    eligibility_waived_reasons: list[str]
    is_approved: bool
    approved_at: datetime | None


class AccrualRunRequest(BaseModel):
    as_of: date


class AccrualRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    employee_code: str | None = None
    employee_name: str | None = None
    department_id: uuid.UUID | None = None
    as_of_date: date
    service_days: int
    service_years: Decimal
    is_eligible: bool
    monthly_wages: Decimal
    eligible_amount: Decimal
    working: dict


class ExitGratuityRequest(BaseModel):
    employee_id: uuid.UUID
    exit_date: date
    exit_reason: ExitReason
    unpaid_leave_days: int = Field(default=0, ge=0)


class PayoutRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    employee_code: str | None = None
    employee_name: str | None = None
    exit_date: date
    exit_reason: ExitReason
    service_years: Decimal
    gross_amount: Decimal
    exempt_amount: Decimal
    taxable_amount: Decimal
    eligibility_waived: bool
    working: dict


class EncashmentPolicyUpsert(BaseModel):
    leave_type_code: str = Field(min_length=1, max_length=32)
    encashable_on_exit: bool = True
    encashable_in_service: bool = False
    max_days_per_year: Decimal | None = None
    cap_days: Decimal | None = None
    min_balance_after_encashment: Decimal = Field(default=Decimal(0), ge=0)
    formula_basis: list[str] = Field(min_length=1)
    days_per_month_divisor: int = Field(gt=0, le=31)
    exempt_on_retirement: bool = False
    retirement_exemption_ceiling: Decimal | None = None
    in_service_fully_taxable: bool = True


class EncashmentPolicyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    leave_type_code: str
    encashable_on_exit: bool
    encashable_in_service: bool
    max_days_per_year: Decimal | None
    cap_days: Decimal | None
    min_balance_after_encashment: Decimal
    formula_basis: list[str]
    days_per_month_divisor: int
    exempt_on_retirement: bool
    retirement_exemption_ceiling: Decimal | None
    in_service_fully_taxable: bool
    active: bool


class EncashmentRequestCreate(BaseModel):
    employee_id: uuid.UUID | None = None
    leave_type_code: str = Field(min_length=1, max_length=32)
    days: Decimal = Field(gt=0)
    financial_year: str = Field(pattern=r"^\d{4}-\d{2}$")


class EncashmentDecision(BaseModel):
    approve: bool
    note: str | None = Field(default=None, max_length=1000)


class EncashmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    employee_code: str | None = None
    employee_name: str | None = None
    leave_type_code: str
    financial_year: str
    days_requested: Decimal
    days_approved: Decimal
    daily_rate: Decimal
    gross_amount: Decimal
    exempt_amount: Decimal
    taxable_amount: Decimal
    is_exit_encashment: bool
    status: EncashmentStatus
    decision_note: str | None = None
    decided_at: datetime | None = None
    created_at: datetime
    working: dict


async def _named(session, user, reads: list, *, department: bool = False) -> list:
    """Fill employee code and name (and department) on read models, in one query."""
    people = await exit_service.employee_details(
        session, user, {read.employee_id for read in reads}
    )
    for read in reads:
        person = people.get(read.employee_id)
        if person is not None:
            read.employee_code = person.employee_code
            read.employee_name = person.full_name
            if department:
                read.department_id = person.department_id
    return reads


def _resolve_employee(user, supplied: uuid.UUID | None) -> uuid.UUID:
    """Whose encashment record this touches: the caller's own, unless HR."""
    return permissions.resolve_employee(
        user, supplied, detail="You can only manage your own encashment requests"
    )


# ── Gratuity configuration ──────────────────────────────────────────────────


@router.get(
    "/gratuity/config",
    response_model=GratuityConfigRead | None,
    dependencies=[Depends(require_roles(*PAYROLL_ROLES))],
)
async def get_config(user: CurrentUser, session: DbSession) -> GratuityConfigRead | None:
    config = await session.scalar(
        tenant_scoped(select(GratuityConfig), GratuityConfig, user)
    )
    return GratuityConfigRead.model_validate(config) if config else None


@router.put(
    "/gratuity/config",
    response_model=GratuityConfigRead,
    dependencies=[Depends(require_roles(UserRole.ADMIN))],
)
async def upsert_config(
    payload: GratuityConfigUpsert, user: CurrentUser, session: DbSession, request: Request
) -> GratuityConfigRead:
    """Set the gratuity rules.

    Editing clears the SME approval: a changed eligibility period or exemption
    ceiling is a new set of rules and needs signing off again. Carrying the old
    approval forward would make the gate meaningless.
    """
    config = await session.scalar(
        tenant_scoped(select(GratuityConfig), GratuityConfig, user)
    )
    if config is None:
        config = GratuityConfig(tenant_id=user.tenant_id, **payload.model_dump())
        session.add(config)
    else:
        for field_name, value in payload.model_dump().items():
            setattr(config, field_name, value)
        config.approved_at = None
        config.approved_by_user_id = None

    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.RULE_CHANGE,
        entity_type="gratuity_config",
        entity_id=config.id,
        actor=user,
        changes=payload.model_dump(mode="json"),
        summary="Updated gratuity configuration; SME approval cleared",
        request=request,
    )
    await session.commit()
    await session.refresh(config)
    return GratuityConfigRead.model_validate(config)


@router.post(
    "/gratuity/config/approve",
    response_model=GratuityConfigRead,
    dependencies=[Depends(require_roles(UserRole.ADMIN))],
)
async def approve_config(
    user: CurrentUser, session: DbSession, request: Request
) -> GratuityConfigRead:
    """Record the Compliance SME sign-off.

    Nothing computes gratuity until this exists.
    """
    from datetime import UTC

    config = await session.scalar(
        tenant_scoped(select(GratuityConfig), GratuityConfig, user)
    )
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No gratuity configuration exists"
        )

    config.approved_by_user_id = user.id
    config.approved_at = datetime.now(UTC)

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.APPROVE,
        entity_type="gratuity_config",
        entity_id=config.id,
        actor=user,
        changes={
            "eligibility_years": config.eligibility_years,
            "eligibility_days_in_final_year": config.eligibility_days_in_final_year,
            "wage_basis": config.formula_basis,
            "exemption_ceiling": str(config.max_exempt_amount),
        },
        summary="Compliance SME approved the gratuity configuration",
        request=request,
    )
    await session.commit()
    await session.refresh(config)
    return GratuityConfigRead.model_validate(config)


# ── Accrual and liability ───────────────────────────────────────────────────


@router.post(
    "/gratuity/accrue",
    dependencies=[Depends(require_roles(*PAYROLL_ROLES))],
)
async def run_accrual(
    payload: AccrualRunRequest, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    """Recompute accrued gratuity across the workforce.

    Runs monthly on a schedule; exposed so finance can refresh it before a
    close. Idempotent — running it twice on the same date changes nothing.
    """
    accruals = await exit_service.accrue_gratuity(
        session, user, as_of=payload.as_of, request=request
    )
    total = sum((Decimal(str(a.eligible_amount)) for a in accruals), start=Decimal(0))
    return {
        "as_of": payload.as_of.isoformat(),
        "employees": len(accruals),
        "total_accrued_liability": str(total),
    }


@router.get(
    "/gratuity/accruals",
    response_model=list[AccrualRead],
    dependencies=[Depends(require_roles(*PAYROLL_ROLES))],
)
async def list_accruals(
    user: CurrentUser, session: DbSession, as_of: date
) -> list[AccrualRead]:
    stmt = tenant_scoped(
        select(GratuityAccrual)
        .where(GratuityAccrual.as_of_date == as_of)
        .order_by(GratuityAccrual.eligible_amount.desc()),
        GratuityAccrual,
        user,
    )
    reads = [AccrualRead.model_validate(row) for row in (await session.scalars(stmt)).all()]
    return await _named(session, user, reads, department=True)


@router.get(
    "/gratuity/accrual-dates",
    response_model=list[date],
    dependencies=[Depends(require_roles(*PAYROLL_ROLES))],
)
async def accrual_dates(user: CurrentUser, session: DbSession) -> list[date]:
    """Dates an accrual has been run for, newest first."""
    return await exit_service.accrual_dates(session, user)


@router.get(
    "/gratuity/liability",
    dependencies=[Depends(require_roles(*PAYROLL_ROLES))],
)
async def liability(user: CurrentUser, session: DbSession, as_of: date) -> dict[str, Any]:
    """Accrued liability by department and tenure band.

    Finance and auditors both ask for this. The response carries a
    `reconciles` flag — the breakdowns must tie back to the total to the
    rupee, and a report that does not tie out is worse than no report because
    somebody will book against it.
    """
    return await exit_service.liability_report(session, user, as_of)


@router.post(
    "/gratuity/payout",
    response_model=PayoutRead,
    dependencies=[Depends(require_roles(*PAYROLL_ROLES))],
)
async def compute_payout(
    payload: ExitGratuityRequest, user: CurrentUser, session: DbSession, request: Request
) -> PayoutRead:
    """Compute gratuity at exit.

    The taxable excess above the exemption is recorded so the TDS computation
    picks it up. A gratuity paid without reaching TDS under-deducts.
    """
    payout = await exit_service.compute_exit_gratuity(
        session,
        user,
        employee_id=payload.employee_id,
        exit_date=payload.exit_date,
        exit_reason=payload.exit_reason,
        unpaid_leave_days=payload.unpaid_leave_days,
        request=request,
    )
    return (await _named(session, user, [PayoutRead.model_validate(payout)]))[0]


@router.get(
    "/gratuity/payouts",
    response_model=list[PayoutRead],
    dependencies=[Depends(require_roles(*PAYROLL_ROLES))],
)
async def list_payouts(user: CurrentUser, session: DbSession) -> list[PayoutRead]:
    stmt = tenant_scoped(
        select(GratuityPayout).order_by(GratuityPayout.exit_date.desc()),
        GratuityPayout,
        user,
    )
    reads = [PayoutRead.model_validate(row) for row in (await session.scalars(stmt)).all()]
    return await _named(session, user, reads)


# ── Encashment ──────────────────────────────────────────────────────────────


@router.get("/encashment/policies", response_model=list[EncashmentPolicyRead])
async def list_policies(user: CurrentUser, session: DbSession) -> list[EncashmentPolicyRead]:
    stmt = tenant_scoped(
        select(EncashmentPolicy).order_by(EncashmentPolicy.leave_type_code),
        EncashmentPolicy,
        user,
    )
    return [
        EncashmentPolicyRead.model_validate(row) for row in (await session.scalars(stmt)).all()
    ]


@router.put(
    "/encashment/policies",
    response_model=EncashmentPolicyRead,
    dependencies=[Depends(require_roles(UserRole.ADMIN))],
)
async def upsert_policy(
    payload: EncashmentPolicyUpsert, user: CurrentUser, session: DbSession, request: Request
) -> EncashmentPolicyRead:
    existing = await session.scalar(
        tenant_scoped(
            select(EncashmentPolicy).where(
                EncashmentPolicy.leave_type_code == payload.leave_type_code
            ),
            EncashmentPolicy,
            user,
        )
    )
    if existing is None:
        existing = EncashmentPolicy(tenant_id=user.tenant_id, **payload.model_dump())
        session.add(existing)
    else:
        for field_name, value in payload.model_dump().items():
            setattr(existing, field_name, value)

    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.RULE_CHANGE,
        entity_type="encashment_policy",
        entity_id=existing.id,
        actor=user,
        changes=payload.model_dump(mode="json"),
        summary=f"Updated the {payload.leave_type_code} encashment policy",
        request=request,
    )
    await session.commit()
    await session.refresh(existing)
    return EncashmentPolicyRead.model_validate(existing)


@router.post(
    "/encashment/requests",
    response_model=EncashmentRead,
    status_code=status.HTTP_201_CREATED,
)
async def request_encashment(
    payload: EncashmentRequestCreate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> EncashmentRead:
    """Raise an in-service encashment.

    The balance comes from the leave ledger; this does not recompute it.
    """
    employee_id = _resolve_employee(user, payload.employee_id)
    record = await exit_service.request_encashment(
        session,
        user,
        employee_id=employee_id,
        leave_type_code=payload.leave_type_code,
        days=payload.days,
        financial_year=payload.financial_year,
        is_exit=False,
        request=request,
    )
    return (await _named(session, user, [EncashmentRead.model_validate(record)]))[0]


@router.get("/encashment/requests", response_model=list[EncashmentRead])
async def list_encashments(
    user: CurrentUser,
    session: DbSession,
    employee_id: uuid.UUID | None = None,
    pending_only: bool = False,
) -> list[EncashmentRead]:
    """Encashments: yours, your reporting line's if you manage, or everyone's for HR."""
    rows = await exit_service.list_encashments(
        session, user, employee_id=employee_id, pending_only=pending_only
    )
    reads = [EncashmentRead.model_validate(row) for row in rows]
    return await _named(session, user, reads)


@router.patch(
    "/encashment/requests/{encashment_id}",
    response_model=EncashmentRead,
    dependencies=[Depends(require_roles(*PAYROLL_ROLES, line_managers=True))],
)
async def decide_encashment(
    encashment_id: uuid.UUID,
    payload: EncashmentDecision,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> EncashmentRead:
    record = await exit_service.decide_encashment(
        session,
        user,
        encashment_id,
        approve=payload.approve,
        note=payload.note,
        request=request,
    )
    return (await _named(session, user, [EncashmentRead.model_validate(record)]))[0]
