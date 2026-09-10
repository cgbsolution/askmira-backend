"""Exit workflow and full & final settlement (slice 13)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.core.deps import CurrentUser, DbSession, require_roles, tenant_scoped
from app.models.audit import AuditAction
from app.models.exit_workflow import (
    ClearanceItem,
    ClearanceStatus,
    ClearanceTemplate,
    ExitRecord,
    ExitStatus,
    ExitType,
    Settlement,
    SettlementLine,
    SettlementStatus,
)
from app.models.user import UserRole
from app.services import audit
from app.services.exit_workflow import service as exit_service
from app.services.exit_workflow import settlement as settlement_engine

router = APIRouter(tags=["exit-workflow"])

HR_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN, UserRole.HR_OPS)
ADMIN_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN)
# Clearance is signed off by the department that owns it, not only by HR.
CLEARANCE_ROLES = (*HR_ROLES, UserRole.MANAGER)


# ── Contracts ───────────────────────────────────────────────────────────────


class ExitCreate(BaseModel):
    employee_id: uuid.UUID
    exit_type: ExitType
    resignation_date: date
    requested_last_working_day: date | None = None
    approved_last_working_day: date | None = None
    notice_period_days: int | None = Field(default=None, ge=0, le=365)
    reason: str | None = None


class ExitRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    exit_type: ExitType
    status: ExitStatus
    resignation_date: date
    requested_last_working_day: date | None
    approved_last_working_day: date | None
    notice_period_days: int
    notice_shortfall_days: int
    notice_waived: bool
    notice_waiver_reason: str | None
    access_revoked_at: datetime | None


class NoticeWaiver(BaseModel):
    reason: str = Field(min_length=1)


class ClearanceTemplateCreate(BaseModel):
    department: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=512)
    is_mandatory: bool = True
    display_order: int = 0


class ClearanceTemplateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    department: str
    description: str
    is_mandatory: bool
    display_order: int
    active: bool


class ClearanceItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    exit_record_id: uuid.UUID
    department: str
    description: str
    is_mandatory: bool
    status: ClearanceStatus
    recovery_amount: Decimal
    cleared_at: datetime | None
    waiver_reason: str | None
    notes: str | None


class ClearanceUpdate(BaseModel):
    outcome: ClearanceStatus
    recovery_amount: Decimal = Field(default=Decimal(0), ge=0)
    waiver_reason: str | None = None
    notes: str | None = None


class SettlementCompute(BaseModel):
    """The figures each owning slice produced, with the basis it produced them on.

    They are passed in rather than recomputed: a settlement that re-derives a
    gratuity figure can disagree with the `GratuityPayout` record it came from,
    and then two documents say different things about the same money.
    """

    final_month_salary: Decimal = Field(default=Decimal(0), ge=0)
    final_month_basis: str = ""
    leave_encashment: Decimal = Field(default=Decimal(0), ge=0)
    leave_encashment_basis: str = ""
    gratuity: Decimal = Field(default=Decimal(0), ge=0)
    gratuity_basis: str = ""
    pending_reimbursements: Decimal = Field(default=Decimal(0), ge=0)
    reimbursement_basis: str = ""
    pro_rata_bonus: Decimal = Field(default=Decimal(0), ge=0)
    bonus_basis: str = ""
    pending_arrears: Decimal = Field(default=Decimal(0), ge=0)
    arrears_basis: str = ""

    notice_shortfall: Decimal = Field(default=Decimal(0), ge=0)
    notice_basis: str = ""
    loan_outstanding: Decimal = Field(default=Decimal(0), ge=0)
    loan_basis: str = ""
    excess_leave_taken: Decimal = Field(default=Decimal(0), ge=0)
    excess_leave_basis: str = ""
    other_recoveries: Decimal = Field(default=Decimal(0), ge=0)
    other_recovery_basis: str = ""

    # Signed: negative is a refund to the employee, which is common for
    # someone leaving part-way through a year.
    final_tds: Decimal = Decimal(0)
    tax_basis: str = ""
    tax_working: dict[str, Any] = Field(default_factory=dict)


class SettlementLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    line_type: str
    code: str
    label: str
    amount: Decimal
    basis: str
    source: str | None
    display_order: int


class SettlementRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    exit_record_id: uuid.UUID
    employee_id: uuid.UUID
    status: SettlementStatus
    total_earnings: Decimal
    total_recoveries: Decimal
    total_tax: Decimal
    net_amount: Decimal
    is_demand: bool
    hold_reason: str | None
    computed_at: datetime | None
    approved_at: datetime | None
    paid_at: datetime | None


class HoldRequest(BaseModel):
    reason: str = Field(min_length=1)


class NoticeRecoveryRequest(BaseModel):
    """The wage basis is a contractual term, so it is supplied rather than
    derived. Computing notice recovery on gross when the contract says basic
    overcharges by a factor of two or more, and it is the most disputed line on
    a settlement."""

    shortfall_days: int = Field(ge=0)
    monthly_wage: Decimal = Field(gt=0)
    days_in_month: int = Field(gt=0, le=31)
    wage_basis_label: str = Field(min_length=1)


# ── Exits ───────────────────────────────────────────────────────────────────


@router.post(
    "/exits",
    response_model=ExitRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def create_exit(
    payload: ExitCreate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> ExitRead:
    record = await exit_service.initiate_exit(
        session,
        user,
        employee_id=payload.employee_id,
        exit_type=payload.exit_type,
        resignation_date=payload.resignation_date,
        requested_last_working_day=payload.requested_last_working_day,
        approved_last_working_day=payload.approved_last_working_day,
        notice_period_days=payload.notice_period_days,
        reason=payload.reason,
        request=request,
    )
    await session.commit()
    await session.refresh(record)
    return ExitRead.model_validate(record)


@router.get(
    "/exits",
    response_model=list[ExitRead],
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def list_exits(
    user: CurrentUser,
    session: DbSession,
    exit_status: ExitStatus | None = None,
) -> list[ExitRead]:
    stmt = select(ExitRecord).order_by(ExitRecord.resignation_date.desc())
    if exit_status is not None:
        stmt = stmt.where(ExitRecord.status == exit_status)
    rows = (await session.scalars(tenant_scoped(stmt, ExitRecord, user))).all()
    return [ExitRead.model_validate(row) for row in rows]


@router.post(
    "/exits/{exit_id}/waive-notice",
    response_model=ExitRead,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def waive_notice(
    exit_id: uuid.UUID,
    payload: NoticeWaiver,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> ExitRead:
    """Waive a notice shortfall, with a reason.

    Waiving recovery is giving away money, so it is recorded with who did it
    and why.
    """
    record = await session.scalar(
        tenant_scoped(select(ExitRecord).where(ExitRecord.id == exit_id), ExitRecord, user)
    )
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Exit record not found")

    record.notice_waived = True
    record.notice_waived_by_user_id = user.id
    record.notice_waiver_reason = payload.reason
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.APPROVE,
        entity_type="exit_record",
        entity_id=record.id,
        summary=(
            f"Waived {record.notice_shortfall_days} day(s) of notice shortfall: "
            f"{payload.reason}"
        ),
        changes={"notice_waiver_reason": payload.reason},
        request=request,
    )
    await session.commit()
    await session.refresh(record)
    return ExitRead.model_validate(record)


@router.post(
    "/exits/{exit_id}/revoke-access",
    response_model=ExitRead,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def revoke(
    exit_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> ExitRead:
    """Deactivate the login. Everything else is retained — see slice 26."""
    record = await exit_service.revoke_access(
        session, user, exit_id=exit_id, request=request
    )
    await session.commit()
    await session.refresh(record)
    return ExitRead.model_validate(record)


# ── Clearance ───────────────────────────────────────────────────────────────


@router.get(
    "/exits/clearance-templates",
    response_model=list[ClearanceTemplateRead],
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def list_templates(
    user: CurrentUser, session: DbSession
) -> list[ClearanceTemplateRead]:
    rows = (
        await session.scalars(
            tenant_scoped(
                select(ClearanceTemplate).order_by(ClearanceTemplate.display_order),
                ClearanceTemplate,
                user,
            )
        )
    ).all()
    return [ClearanceTemplateRead.model_validate(row) for row in rows]


@router.post(
    "/exits/clearance-templates",
    response_model=ClearanceTemplateRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def create_template(
    payload: ClearanceTemplateCreate,
    user: CurrentUser,
    session: DbSession,
) -> ClearanceTemplateRead:
    template = ClearanceTemplate(tenant_id=user.tenant_id, **payload.model_dump())
    session.add(template)
    await session.commit()
    await session.refresh(template)
    return ClearanceTemplateRead.model_validate(template)


@router.get(
    "/exits/{exit_id}/clearance",
    response_model=list[ClearanceItemRead],
    dependencies=[Depends(require_roles(*CLEARANCE_ROLES))],
)
async def list_clearance(
    exit_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[ClearanceItemRead]:
    rows = (
        await session.scalars(
            tenant_scoped(
                select(ClearanceItem)
                .where(ClearanceItem.exit_record_id == exit_id)
                .order_by(ClearanceItem.department),
                ClearanceItem,
                user,
            )
        )
    ).all()
    return [ClearanceItemRead.model_validate(row) for row in rows]


@router.patch(
    "/exits/clearance/{item_id}",
    response_model=ClearanceItemRead,
    dependencies=[Depends(require_roles(*CLEARANCE_ROLES))],
)
async def update_clearance(
    item_id: uuid.UUID,
    payload: ClearanceUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> ClearanceItemRead:
    item = await exit_service.clear_item(
        session,
        user,
        item_id=item_id,
        outcome=payload.outcome,
        recovery_amount=payload.recovery_amount,
        waiver_reason=payload.waiver_reason,
        notes=payload.notes,
        request=request,
    )
    await session.commit()
    await session.refresh(item)
    return ClearanceItemRead.model_validate(item)


# ── Settlement ──────────────────────────────────────────────────────────────


@router.post(
    "/exits/notice-recovery",
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def notice_recovery(payload: NoticeRecoveryRequest) -> dict[str, str]:
    """Compute a notice shortfall recovery and its basis line.

    Separate from the settlement so the figure can be shown, discussed and
    agreed before it lands on a statement.
    """
    try:
        amount, basis = settlement_engine.notice_shortfall_recovery(
            shortfall_days=payload.shortfall_days,
            monthly_wage=payload.monthly_wage,
            days_in_month=payload.days_in_month,
            wage_basis_label=payload.wage_basis_label,
        )
    except settlement_engine.SettlementError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return {"amount": str(amount), "basis": basis}


@router.post(
    "/exits/{exit_id}/settlement",
    response_model=SettlementRead,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def compute(
    exit_id: uuid.UUID,
    payload: SettlementCompute,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> SettlementRead:
    """Compute the settlement. Blocked while mandatory clearance is pending."""
    settlement = await exit_service.compute_settlement(
        session,
        user,
        exit_id=exit_id,
        inputs=settlement_engine.SettlementInput(**payload.model_dump()),
        request=request,
    )
    await session.commit()
    await session.refresh(settlement)
    return SettlementRead.model_validate(settlement)


@router.get(
    "/exits/{exit_id}/settlement/lines",
    response_model=list[SettlementLineRead],
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def settlement_lines(
    exit_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[SettlementLineRead]:
    """Every line with its basis.

    The employee reads this on the day they lose access to anyone who could
    explain it, so each line has to stand on its own.
    """
    settlement = await session.scalar(
        tenant_scoped(
            select(Settlement).where(Settlement.exit_record_id == exit_id), Settlement, user
        )
    )
    if settlement is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No settlement has been computed"
        )
    rows = (
        await session.scalars(
            tenant_scoped(
                select(SettlementLine)
                .where(SettlementLine.settlement_id == settlement.id)
                .order_by(SettlementLine.display_order),
                SettlementLine,
                user,
            )
        )
    ).all()
    return [SettlementLineRead.model_validate(row) for row in rows]


@router.post(
    "/exits/settlements/{settlement_id}/hold",
    response_model=SettlementRead,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def hold(
    settlement_id: uuid.UUID,
    payload: HoldRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> SettlementRead:
    """Computed but not paid, with the reason recorded."""
    settlement = await exit_service.hold_settlement(
        session, user, settlement_id=settlement_id, reason=payload.reason, request=request
    )
    await session.commit()
    await session.refresh(settlement)
    return SettlementRead.model_validate(settlement)


@router.post(
    "/exits/settlements/{settlement_id}/approve",
    response_model=SettlementRead,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def approve_settlement(
    settlement_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> SettlementRead:
    """Sign off for payment — or, where the net is negative, for a demand.

    The two are different outcomes and the status says which, so a demand is
    never mistaken for a payment awaiting a bank run.
    """
    settlement = await session.scalar(
        tenant_scoped(
            select(Settlement).where(Settlement.id == settlement_id), Settlement, user
        )
    )
    if settlement is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Settlement not found")
    if settlement.status is SettlementStatus.PAID:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="This settlement has been paid."
        )

    is_demand = Decimal(str(settlement.net_amount)) < 0
    settlement.status = (
        SettlementStatus.DEMAND_RAISED if is_demand else SettlementStatus.APPROVED
    )
    settlement.approved_by_user_id = user.id
    settlement.approved_at = datetime.now(tz=None).astimezone()
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.APPROVE,
        entity_type="settlement",
        entity_id=settlement.id,
        summary=(
            f"Raised a demand for {-Decimal(str(settlement.net_amount))} against the "
            "employee"
            if is_demand
            else f"Approved a settlement of {settlement.net_amount} for payment"
        ),
        changes={"net_amount": str(settlement.net_amount), "is_demand": is_demand},
        request=request,
    )
    await session.commit()
    await session.refresh(settlement)
    return SettlementRead.model_validate(settlement)
