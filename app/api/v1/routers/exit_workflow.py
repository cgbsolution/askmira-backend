"""Exit workflow and full & final settlement (slice 13).

Thin: parse, gate, delegate, serialise. Managers reach the exit and clearance
routes for their own reporting line; who may see or sign off what is decided in
`app.services.exit_workflow.operations` through `app.core.permissions`.
Settlement routes are HR's.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.exit_workflow import (
    ClearanceOwnerRole,
    ClearanceStatus,
    ExitStatus,
    ExitType,
    SettlementStatus,
)
from app.models.user import UserRole
from app.schemas.payroll_ops import IssuedDocumentRead
from app.services.exit_workflow import operations
from app.services.exit_workflow import service as exit_service
from app.services.exit_workflow import settlement as settlement_engine
from app.services.filings import support

router = APIRouter(tags=["exit-workflow"])

HR_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN, UserRole.HR_OPS)
ADMIN_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN)
# Exits and clearance are reached by managers too; which exit is theirs is
# decided per record in the service.
MANAGER_ROLES = (*HR_ROLES, UserRole.MANAGER)


# ── Contracts ───────────────────────────────────────────────────────────────


class ExitCreate(BaseModel):
    employee_id: uuid.UUID
    exit_type: ExitType
    resignation_date: date
    requested_last_working_day: date | None = None
    approved_last_working_day: date | None = None
    notice_period_days: int | None = Field(default=None, ge=0, le=365)
    reason: str | None = Field(default=None, max_length=2000)


class ExitRead(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    employee_code: str | None
    employee_name: str | None
    exit_type: ExitType
    status: ExitStatus
    resignation_date: date
    requested_last_working_day: date | None
    approved_last_working_day: date | None
    notice_period_days: int
    notice_shortfall_days: int
    notice_waived: bool
    notice_waiver_reason: str | None
    reason: str | None
    access_revoked_at: datetime | None
    clearance_total: int
    clearance_pending_mandatory: int
    settlement_id: uuid.UUID | None
    settlement_status: SettlementStatus | None
    created_at: datetime


class ReasonRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class NoteRequest(BaseModel):
    note: str = Field(min_length=1, max_length=2000)


class ClearanceTemplateCreate(BaseModel):
    department: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=512)
    is_mandatory: bool = True
    display_order: int = Field(default=0, ge=0)
    owner_role: ClearanceOwnerRole = ClearanceOwnerRole.HR
    owner_user_id: uuid.UUID | None = None


class ClearanceTemplateRead(BaseModel):
    id: uuid.UUID
    department: str
    description: str
    is_mandatory: bool
    display_order: int
    active: bool
    owner_role: str
    owner_user_id: uuid.UUID | None
    owner_name: str | None


class ClearanceOwnerRead(BaseModel):
    user_id: uuid.UUID
    full_name: str
    role: str


class ClearanceItemRead(BaseModel):
    id: uuid.UUID
    exit_record_id: uuid.UUID
    department: str
    description: str
    is_mandatory: bool
    status: ClearanceStatus
    recovery_amount: Decimal
    cleared_at: datetime | None
    cleared_by_name: str | None
    waiver_reason: str | None
    notes: str | None
    owner_role: str
    owner_user_id: uuid.UUID | None
    owner_name: str | None
    can_update: bool


class ClearanceUpdate(BaseModel):
    outcome: ClearanceStatus
    recovery_amount: Decimal = Field(default=Decimal(0), ge=0, max_digits=14, decimal_places=2)
    waiver_reason: str | None = Field(default=None, max_length=2000)
    notes: str | None = Field(default=None, max_length=2000)


_Amount = Field(default=Decimal(0), ge=0, max_digits=14, decimal_places=2)


class SettlementCompute(BaseModel):
    """The figures each owning slice produced, with the basis it produced them on.

    They are passed in rather than recomputed: a settlement that re-derives a
    gratuity figure can disagree with the `GratuityPayout` record it came from,
    and then two documents say different things about the same money.
    """

    final_month_salary: Decimal = _Amount
    final_month_basis: str = Field(default="", max_length=2000)
    leave_encashment: Decimal = _Amount
    leave_encashment_basis: str = Field(default="", max_length=2000)
    gratuity: Decimal = _Amount
    gratuity_basis: str = Field(default="", max_length=2000)
    pending_reimbursements: Decimal = _Amount
    reimbursement_basis: str = Field(default="", max_length=2000)
    pro_rata_bonus: Decimal = _Amount
    bonus_basis: str = Field(default="", max_length=2000)
    pending_arrears: Decimal = _Amount
    arrears_basis: str = Field(default="", max_length=2000)

    notice_shortfall: Decimal = _Amount
    notice_basis: str = Field(default="", max_length=2000)
    loan_outstanding: Decimal = _Amount
    loan_basis: str = Field(default="", max_length=2000)
    excess_leave_taken: Decimal = _Amount
    excess_leave_basis: str = Field(default="", max_length=2000)
    other_recoveries: Decimal = _Amount
    other_recovery_basis: str = Field(default="", max_length=2000)

    # Signed: negative is a refund to the employee, which is common for
    # someone leaving part-way through a year.
    final_tds: Decimal = Field(default=Decimal(0), max_digits=14, decimal_places=2)
    tax_basis: str = Field(default="", max_length=2000)
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
    id: uuid.UUID
    exit_record_id: uuid.UUID
    employee_id: uuid.UUID
    employee_code: str | None
    employee_name: str | None
    exit_type: ExitType | None
    last_working_day: date | None
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
    statement_letter_id: uuid.UUID | None


class NoticeRecoveryRequest(BaseModel):
    """The wage basis is a contractual term, so it is supplied rather than
    derived. Computing notice recovery on gross when the contract says basic
    overcharges by a factor of two or more, and it is the most disputed line on
    a settlement."""

    shortfall_days: int = Field(ge=0)
    monthly_wage: Decimal = Field(gt=0)
    days_in_month: int = Field(gt=0, le=31)
    wage_basis_label: str = Field(min_length=1)


class ExitLetterRequest(BaseModel):
    kind: Literal["relieving", "experience"]


class LetterGateRead(BaseModel):
    allowed: bool
    reason: str
    requires_settlement: bool
    settlement_status: SettlementStatus | None
    hold_reason: str | None


class ExitPolicy(BaseModel):
    letters_require_settlement: bool


async def _exit_read(session, user, record) -> ExitRead:
    return ExitRead(**await operations.exit_read(session, user, record))


async def _settlement_read(session, user, settlement) -> SettlementRead:
    rows = await operations.settlement_reads(session, user, [settlement])
    return SettlementRead(**rows[0])


# ── Policy, templates, settlements list ─────────────────────────────────────
# Registered before `/exits/{exit_id}`: a literal path must match before the
# path parameter tries to read it as an id.


@router.get(
    "/exits/policy",
    response_model=ExitPolicy,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def get_policy(user: CurrentUser, session: DbSession) -> ExitPolicy:
    return ExitPolicy(**await operations.exit_policy(session, user))


@router.put(
    "/exits/policy",
    response_model=ExitPolicy,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def set_policy(
    payload: ExitPolicy, user: CurrentUser, session: DbSession, request: Request
) -> ExitPolicy:
    result = await operations.set_exit_policy(
        session,
        user,
        letters_require_settlement=payload.letters_require_settlement,
        request=request,
    )
    await session.commit()
    return ExitPolicy(**result)


@router.get(
    "/exits/clearance-templates",
    response_model=list[ClearanceTemplateRead],
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def list_templates(user: CurrentUser, session: DbSession) -> list[ClearanceTemplateRead]:
    return [ClearanceTemplateRead(**row) for row in await operations.list_templates(session, user)]


@router.post(
    "/exits/clearance-templates",
    response_model=ClearanceTemplateRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def create_template(
    payload: ClearanceTemplateCreate, user: CurrentUser, session: DbSession, request: Request
) -> ClearanceTemplateRead:
    template = await operations.create_template(
        session,
        user,
        department=payload.department,
        description=payload.description,
        is_mandatory=payload.is_mandatory,
        display_order=payload.display_order,
        owner_role=payload.owner_role,
        owner_user_id=payload.owner_user_id,
        request=request,
    )
    await session.commit()
    rows = await operations.list_templates(session, user)
    return ClearanceTemplateRead(**next(row for row in rows if row["id"] == template.id))


@router.get(
    "/exits/clearance-owners",
    response_model=list[ClearanceOwnerRead],
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def clearance_owners(user: CurrentUser, session: DbSession) -> list[ClearanceOwnerRead]:
    return [ClearanceOwnerRead(**row) for row in await operations.owner_candidates(session, user)]


@router.get(
    "/exits/settlements",
    response_model=list[SettlementRead],
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def list_settlements(
    user: CurrentUser,
    session: DbSession,
    settlement_status: SettlementStatus | None = Query(default=None, alias="status"),
) -> list[SettlementRead]:
    rows = await operations.list_settlements(
        session, user, settlement_status=settlement_status
    )
    return [SettlementRead(**row) for row in rows]


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


# ── Exits ───────────────────────────────────────────────────────────────────


@router.post(
    "/exits",
    response_model=ExitRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*MANAGER_ROLES))],
)
async def create_exit(
    payload: ExitCreate, user: CurrentUser, session: DbSession, request: Request
) -> ExitRead:
    """Open an exit: HR for anyone, a manager for their reporting line, nobody for themselves."""
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
    return await _exit_read(session, user, record)


@router.get(
    "/exits",
    response_model=list[ExitRead],
    dependencies=[Depends(require_roles(*MANAGER_ROLES))],
)
async def list_exits(
    user: CurrentUser, session: DbSession, exit_status: ExitStatus | None = None
) -> list[ExitRead]:
    """HR sees every exit; a manager their reporting line and items they own."""
    return [
        ExitRead(**row)
        for row in await operations.list_exits(session, user, exit_status=exit_status)
    ]


@router.get(
    "/exits/{exit_id}",
    response_model=ExitRead,
    dependencies=[Depends(require_roles(*MANAGER_ROLES))],
)
async def get_exit(exit_id: uuid.UUID, user: CurrentUser, session: DbSession) -> ExitRead:
    record = await operations.visible_exit(session, user, exit_id)
    return await _exit_read(session, user, record)


@router.post(
    "/exits/{exit_id}/cancel",
    response_model=ExitRead,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def cancel_exit(
    exit_id: uuid.UUID, payload: ReasonRequest, user: CurrentUser, session: DbSession,
    request: Request,
) -> ExitRead:
    record = await operations.cancel_exit(
        session, user, exit_id, reason=payload.reason, request=request
    )
    await session.commit()
    return await _exit_read(session, user, record)


@router.post(
    "/exits/{exit_id}/waive-notice",
    response_model=ExitRead,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def waive_notice(
    exit_id: uuid.UUID, payload: ReasonRequest, user: CurrentUser, session: DbSession,
    request: Request,
) -> ExitRead:
    """Waive a notice shortfall, with a reason. Waiving recovery gives money away."""
    record = await operations.waive_notice(
        session, user, exit_id, reason=payload.reason, request=request
    )
    await session.commit()
    return await _exit_read(session, user, record)


@router.post(
    "/exits/{exit_id}/revoke-access",
    response_model=ExitRead,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def revoke(
    exit_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> ExitRead:
    """Deactivate the login. Everything else is retained — see slice 26."""
    record = await exit_service.revoke_access(session, user, exit_id=exit_id, request=request)
    await session.commit()
    await session.refresh(record)
    return await _exit_read(session, user, record)


# ── Clearance ───────────────────────────────────────────────────────────────


@router.get(
    "/exits/{exit_id}/clearance",
    response_model=list[ClearanceItemRead],
    dependencies=[Depends(require_roles(*MANAGER_ROLES))],
)
async def list_clearance(
    exit_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[ClearanceItemRead]:
    return [
        ClearanceItemRead(**row)
        for row in await operations.list_clearance(session, user, exit_id)
    ]


@router.patch(
    "/exits/clearance/{item_id}",
    response_model=ClearanceItemRead,
    dependencies=[Depends(require_roles(*MANAGER_ROLES))],
)
async def update_clearance(
    item_id: uuid.UUID,
    payload: ClearanceUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> ClearanceItemRead:
    """Sign an item off: its owner, the line manager for a manager item, or HR."""
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
    rows = await operations.list_clearance(session, user, item.exit_record_id)
    return ClearanceItemRead(**next(row for row in rows if row["id"] == item.id))


# ── Letters ─────────────────────────────────────────────────────────────────


@router.get(
    "/exits/{exit_id}/letter-gate",
    response_model=LetterGateRead,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def letter_gate(exit_id: uuid.UUID, user: CurrentUser, session: DbSession) -> LetterGateRead:
    record = await operations.visible_exit(session, user, exit_id)
    return LetterGateRead(**await operations.letter_gate(session, user, record.employee_id))


@router.post(
    "/exits/{exit_id}/letters",
    response_model=IssuedDocumentRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def issue_letter(
    exit_id: uuid.UUID,
    payload: ExitLetterRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> IssuedDocumentRead:
    """A relieving or experience letter — refused while the settlement is on hold,
    and, where the workspace says so, until it is approved."""
    letter = await operations.issue_exit_letter(
        session, user, exit_id, kind=payload.kind, request=request
    )
    await session.commit()
    await session.refresh(letter)
    return IssuedDocumentRead.from_model(letter)


# ── Settlement ──────────────────────────────────────────────────────────────


@router.get(
    "/exits/{exit_id}/settlement/sources",
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def settlement_sources(
    exit_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    """Figures other modules already hold for this leaver, to prefill the settlement."""
    return await operations.settlement_sources(session, user, exit_id)


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
    return await _settlement_read(session, user, settlement)


@router.get(
    "/exits/{exit_id}/settlement",
    response_model=SettlementRead,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def get_settlement(
    exit_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> SettlementRead:
    settlement = await operations.settlement_for_exit(session, user, exit_id)
    return await _settlement_read(session, user, settlement)


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
    rows = await operations.settlement_lines(session, user, exit_id)
    return [SettlementLineRead.model_validate(row) for row in rows]


@router.post(
    "/exits/settlements/{settlement_id}/hold",
    response_model=SettlementRead,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def hold(
    settlement_id: uuid.UUID,
    payload: ReasonRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> SettlementRead:
    """Computed but not paid, with the reason recorded. Letters wait too."""
    settlement = await exit_service.hold_settlement(
        session, user, settlement_id=settlement_id, reason=payload.reason, request=request
    )
    await session.commit()
    await session.refresh(settlement)
    return await _settlement_read(session, user, settlement)


@router.post(
    "/exits/settlements/{settlement_id}/release-hold",
    response_model=SettlementRead,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def release_hold(
    settlement_id: uuid.UUID,
    payload: NoteRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> SettlementRead:
    settlement = await operations.release_hold(
        session, user, settlement_id, note=payload.note, request=request
    )
    await session.commit()
    await session.refresh(settlement)
    return await _settlement_read(session, user, settlement)


@router.post(
    "/exits/settlements/{settlement_id}/approve",
    response_model=SettlementRead,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def approve_settlement(
    settlement_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> SettlementRead:
    """Sign off for payment — or, where the net is negative, for a demand."""
    settlement = await operations.approve_settlement(
        session, user, settlement_id, request=request
    )
    await session.commit()
    await session.refresh(settlement)
    return await _settlement_read(session, user, settlement)


@router.post(
    "/exits/settlements/{settlement_id}/statement",
    response_model=IssuedDocumentRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def issue_statement(
    settlement_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> IssuedDocumentRead:
    """The itemised settlement statement, issued through the letter engine."""
    letter = await operations.issue_statement(session, user, settlement_id, request=request)
    await session.commit()
    await session.refresh(letter)
    return IssuedDocumentRead.from_model(letter)


@router.get(
    "/exits/settlements/{settlement_id}/statement/pdf",
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def statement_pdf(
    settlement_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> Response:
    data, name = await operations.statement_pdf(session, user, settlement_id)
    return Response(
        content=data, media_type="application/pdf", headers=support.attachment_headers(name)
    )
