"""Requisition approval and offer management (slice 19).

Who may sign a step is decided by `app.services.hiring.approvers`: never the
person who raised the requisition or drafted the offer, and only somebody holding
the role the step names.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.offer import ApprovalScope, OfferComponentKind, OfferStatus
from app.models.user import UserRole
from app.services.hiring import approval_chain, chain_rules
from app.services.hiring import offers as offer_service
from app.services.hiring import requisitions as requisition_service

router = APIRouter(tags=["offers"])

HR_ROLES = (UserRole.ADMIN, UserRole.SUB_ADMIN)
ADMIN_ROLES = (UserRole.ADMIN,)
# Anyone who might be a step in a chain: HR, and anybody who manages people.
# Which of them may decide *this* one is settled by the chain and
# `approvers.can_decide_step`, not by the route.


# ── Contracts ───────────────────────────────────────────────────────────────


class ChainRuleCreate(BaseModel):
    """One band in an approval chain.

    `ctc_max` is exclusive and `None` means unbounded — the top band, which
    every rule set must have or the largest offers resolve to no approver.
    """

    scope: ApprovalScope
    sequence: int = Field(ge=1, le=100)
    approver_role: str = Field(min_length=1, max_length=64)
    label: str | None = Field(default=None, max_length=128)
    ctc_min: Decimal = Field(default=Decimal(0), ge=0)
    ctc_max: Decimal | None = Field(default=None, gt=0)
    grades: list[str] = Field(default_factory=list)
    only_when_over_band: bool = False


class ChainRuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    scope: ApprovalScope
    sequence: int
    approver_role: str
    label: str | None
    ctc_min: Decimal
    ctc_max: Decimal | None
    grades: list[str]
    only_when_over_band: bool
    is_active: bool


class RequisitionDecision(BaseModel):
    approve: bool
    note: str | None = Field(default=None, max_length=2000)


class OfferComponentCreate(BaseModel):
    kind: OfferComponentKind
    label: str = Field(min_length=1, max_length=128)
    amount: Decimal = Field(gt=0)
    payable_terms: str | None = Field(default=None, max_length=500)
    counts_toward_ctc: bool = False


class OfferCreate(BaseModel):
    candidate_id: uuid.UUID
    code: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9-]+$")
    salary_structure_id: uuid.UUID
    ctc_annual: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    joining_date: date
    designation: str = Field(min_length=1, max_length=255)
    grade: str | None = Field(default=None, max_length=32)
    joining_location_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    legal_entity_id: uuid.UUID | None = None
    probation_months: int = Field(default=6, ge=0, le=24)
    notice_period_days: int = Field(default=60, ge=0, le=365)
    valid_until: date | None = None
    components: list[OfferComponentCreate] = Field(default_factory=list, max_length=20)


class OfferTermsUpdate(BaseModel):
    """Only the terms that can move. Anything material voids the approvals."""

    ctc_annual: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    salary_structure_id: uuid.UUID | None = None
    grade: str | None = Field(default=None, max_length=32)
    designation: str | None = Field(default=None, max_length=255)
    joining_date: date | None = None
    joining_location_id: uuid.UUID | None = None
    probation_months: int | None = Field(default=None, ge=0, le=24)
    notice_period_days: int | None = Field(default=None, ge=0, le=365)
    valid_until: date | None = None


class OfferRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    candidate_id: uuid.UUID
    requisition_id: uuid.UUID
    ctc_annual: Decimal
    grade: str | None
    designation: str
    joining_date: date
    status: str
    valid_until: date | None
    released_at: datetime | None
    responded_at: datetime | None
    outcome_reason: str | None
    resigned_elsewhere: bool
    converted_employee_id: uuid.UUID | None


class OfferDecision(BaseModel):
    approve: bool
    note: str | None = Field(default=None, max_length=2000)


class ReleaseRequest(BaseModel):
    template_id: uuid.UUID
    valid_until: date | None = None


class AcceptRequest(BaseModel):
    #: The e-signature envelope or scanned acceptance this was recorded from.
    signature_reference: str | None = Field(default=None, max_length=255)


class ReasonRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class RevokeRequest(ReasonRequest):
    # Surfaced to HR rather than logged: revoking on someone who has already
    # resigned elsewhere carries legal and reputational exposure.
    candidate_resigned_elsewhere: bool = False


class ConvertRequest(BaseModel):
    """The only two fields that do not already exist before joining."""

    employee_code: str = Field(min_length=1, max_length=32)
    work_email: str = Field(min_length=3, max_length=320)


# ── Approval chain configuration ────────────────────────────────────────────


@router.post(
    "/hiring/approval-rules",
    response_model=ChainRuleRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def create_chain_rule(
    payload: ChainRuleCreate, user: CurrentUser, session: DbSession, request: Request
) -> ChainRuleRead:
    rule = await chain_rules.create_rule(
        session, user, values=payload.model_dump(), request=request
    )
    await session.commit()
    await session.refresh(rule)
    return ChainRuleRead.model_validate(rule)


@router.get(
    "/hiring/approval-rules",
    response_model=list[ChainRuleRead],
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def list_chain_rules(
    user: CurrentUser, session: DbSession, scope: ApprovalScope | None = None
) -> list[ChainRuleRead]:
    rows = await chain_rules.list_rules(session, user, scope=scope)
    return [ChainRuleRead.model_validate(r) for r in rows]


@router.get(
    "/hiring/approval-rules/validate",
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def validate_chain_rules(
    user: CurrentUser, session: DbSession, scope: ApprovalScope
) -> dict[str, Any]:
    """Check a rule set covers every CTC before an offer discovers it does not."""
    rules = await requisition_service.load_rules(session, user, scope)
    try:
        approval_chain.validate_rules(rules)
    except approval_chain.ApprovalChainError as exc:
        return {"valid": False, "problem": str(exc)}
    return {"valid": True, "rule_count": len(rules)}


# ── Requisition approval ────────────────────────────────────────────────────


@router.post(
    "/hiring/requisitions/{requisition_id}/submit",
    dependencies=[Depends(require_roles(*HR_ROLES, line_managers=True))],
)
async def submit_requisition(
    requisition_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    request: Request,
    live_headcount: int | None = None,
) -> dict[str, Any]:
    """Send a draft for approval. HR, or the requisition's own hiring team."""
    result = await requisition_service.submit_requisition(
        session,
        user,
        requisition_id=requisition_id,
        live_headcount=live_headcount,
        request=request,
    )
    await session.commit()
    return result


@router.post(
    "/hiring/requisitions/{requisition_id}/decide",
    dependencies=[Depends(require_roles(*HR_ROLES, line_managers=True))],
)
async def decide_requisition(
    requisition_id: uuid.UUID,
    payload: RequisitionDecision,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    result = await requisition_service.approve_requisition(
        session,
        user,
        requisition_id=requisition_id,
        approve=payload.approve,
        note=payload.note,
        request=request,
    )
    await session.commit()
    return result


# ── Offers ──────────────────────────────────────────────────────────────────


@router.post(
    "/offers",
    response_model=OfferRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def create_offer(
    payload: OfferCreate, user: CurrentUser, session: DbSession, request: Request
) -> OfferRead:
    offer = await offer_service.create_offer(
        session,
        user,
        candidate_id=payload.candidate_id,
        code=payload.code,
        salary_structure_id=payload.salary_structure_id,
        ctc_annual=payload.ctc_annual,
        joining_date=payload.joining_date,
        designation=payload.designation,
        grade=payload.grade,
        joining_location_id=payload.joining_location_id,
        department_id=payload.department_id,
        legal_entity_id=payload.legal_entity_id,
        probation_months=payload.probation_months,
        notice_period_days=payload.notice_period_days,
        valid_until=payload.valid_until,
        components=[c.model_dump() for c in payload.components],
        request=request,
    )
    await session.commit()
    await session.refresh(offer)
    return OfferRead.model_validate(offer)


@router.get(
    "/offers", dependencies=[Depends(require_roles(*HR_ROLES))]
)
async def list_offers(
    user: CurrentUser,
    session: DbSession,
    requisition_id: uuid.UUID | None = None,
    offer_status: OfferStatus | None = None,
) -> list[dict[str, Any]]:
    return await offer_service.list_offer_summaries(
        session, user, requisition_id=requisition_id, offer_status=offer_status
    )


@router.post("/offers/lapse", dependencies=[Depends(require_roles(*HR_ROLES))])
async def lapse_offers(
    user: CurrentUser, session: DbSession, as_of: date | None = None
) -> dict[str, Any]:
    """Expire released offers past their validity date. Also scheduler-callable."""
    result = await offer_service.lapse_due_offers(session, user, as_of=as_of)
    await session.commit()
    return result


@router.get("/offers/{offer_id}", dependencies=[Depends(require_roles(*HR_ROLES))])
async def get_offer(offer_id: uuid.UUID, user: CurrentUser, session: DbSession) -> dict[str, Any]:
    return await offer_service.offer_detail(session, user, offer_id=offer_id)


@router.get(
    "/offers/{offer_id}/approval",
    dependencies=[Depends(require_roles(*HR_ROLES, line_managers=True))],
)
async def offer_approval_view(
    offer_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    """Everything an approver needs — deviation and parity first — in one call.

    HR, or the requisition's hiring manager. Nobody else in the tenant reads an
    offer's CTC.
    """
    return await offer_service.approval_view(session, user, offer_id=offer_id)


@router.patch(
    "/offers/{offer_id}/terms",
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def update_offer_terms(
    offer_id: uuid.UUID,
    payload: OfferTermsUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Nothing to change."
        )
    result = await offer_service.update_offer_terms(
        session, user, offer_id=offer_id, changes=changes, request=request
    )
    await session.commit()
    return result


@router.post("/offers/{offer_id}/submit", dependencies=[Depends(require_roles(*HR_ROLES))])
async def submit_offer(
    offer_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    result = await offer_service.submit_offer(
        session, user, offer_id=offer_id, request=request
    )
    await session.commit()
    return result


@router.post(
    "/offers/{offer_id}/decide",
    dependencies=[Depends(require_roles(*HR_ROLES, line_managers=True))],
)
async def decide_offer(
    offer_id: uuid.UUID,
    payload: OfferDecision,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    result = await offer_service.approve_offer(
        session,
        user,
        offer_id=offer_id,
        approve=payload.approve,
        note=payload.note,
        request=request,
    )
    await session.commit()
    return result


@router.post("/offers/{offer_id}/release", dependencies=[Depends(require_roles(*HR_ROLES))])
async def release_offer(
    offer_id: uuid.UUID,
    payload: ReleaseRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    result = await offer_service.release_offer(
        session,
        user,
        offer_id=offer_id,
        template_id=payload.template_id,
        valid_until=payload.valid_until,
        request=request,
    )
    await session.commit()
    return result


@router.post("/offers/{offer_id}/accept", dependencies=[Depends(require_roles(*HR_ROLES))])
async def accept_offer(
    offer_id: uuid.UUID,
    payload: AcceptRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    """Record an acceptance HR received outside the portal (by email, on paper).

    A candidate normally accepts in their portal by typing their name against the
    letter they were shown (`POST /public/candidate/{token}/offers/{id}/respond`),
    which keeps the typed name, time, address and the letter's hash.
    """
    result = await offer_service.accept_offer(
        session,
        user,
        offer_id=offer_id,
        signature_reference=payload.signature_reference,
        request=request,
    )
    await session.commit()
    return result


@router.post("/offers/{offer_id}/decline", dependencies=[Depends(require_roles(*HR_ROLES))])
async def decline_offer(
    offer_id: uuid.UUID,
    payload: ReasonRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    result = await offer_service.decline_offer(
        session, user, offer_id=offer_id, reason=payload.reason, request=request
    )
    await session.commit()
    return result


@router.post("/offers/{offer_id}/drop", dependencies=[Depends(require_roles(*HR_ROLES))])
async def record_drop(
    offer_id: uuid.UUID,
    payload: ReasonRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    """Accepted, then did not join. A different failure from a decline."""
    result = await offer_service.record_drop(
        session, user, offer_id=offer_id, reason=payload.reason, request=request
    )
    await session.commit()
    return result


@router.post("/offers/{offer_id}/revoke", dependencies=[Depends(require_roles(*ADMIN_ROLES))])
async def revoke_offer(
    offer_id: uuid.UUID,
    payload: RevokeRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    result = await offer_service.revoke_offer(
        session,
        user,
        offer_id=offer_id,
        reason=payload.reason,
        candidate_resigned_elsewhere=payload.candidate_resigned_elsewhere,
        request=request,
    )
    await session.commit()
    return result


@router.post("/offers/{offer_id}/convert", dependencies=[Depends(require_roles(*HR_ROLES))])
async def convert_offer(
    offer_id: uuid.UUID,
    payload: ConvertRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    result = await offer_service.convert_to_employee(
        session,
        user,
        offer_id=offer_id,
        employee_code=payload.employee_code,
        work_email=payload.work_email,
        request=request,
    )
    await session.commit()
    return result


@router.get(
    "/offers/{offer_id}/history",
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def offer_history(
    offer_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[dict[str, Any]]:
    """Every transition, with the facts snapshotted at the time."""
    return await offer_service.offer_history(session, user, offer_id=offer_id)
