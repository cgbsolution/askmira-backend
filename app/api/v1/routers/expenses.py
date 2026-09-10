"""Expense policy, claims, travel and advances (slice 31).

`POST /expenses/claims` is the endpoint the slice exists for. It captures the
claim **and returns the policy verdict in the same response** — the limit, what
has already been claimed against it, the overage and what the employee must do
next. There is deliberately no "evaluate this claim" endpoint to call later:
an employee who finds out three weeks after the fact that their claim was out of
policy is an employee who stops claiming properly.

`GET /expenses/posture` says what this slice enforces and, line by line, what it
does not.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.expenses import (
    CityTierEnum,
    ClaimKindEnum,
    ConversionConventionEnum,
    LimitTypeEnum,
    ViolationActionEnum,
)
from app.models.user import UserRole
from app.services.expenses import service as expenses

router = APIRouter(tags=["expenses"])

FINANCE_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN, UserRole.HR_OPS)
ADMIN_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN)


# ── Contracts ───────────────────────────────────────────────────────────────


class RuleIn(BaseModel):
    category_key: str = Field(min_length=1, max_length=80)
    city_tier: CityTierEnum
    limit_type: LimitTypeEnum
    limit_amount: Decimal = Field(ge=0)
    violation_action: ViolationActionEnum
    requires_receipt: bool = True
    #: Below this, no receipt is asked for. A ₹40 auto fare with a receipt
    #: demand is a policy that produces fake receipts.
    receipt_threshold: Decimal = Field(default=Decimal("0"), ge=0)
    requires_pre_approval: bool = False
    is_per_diem: bool = False


class PolicyIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    effective_from: date
    rules: list[RuleIn] = Field(min_length=1)
    description: str | None = Field(default=None, max_length=2000)
    #: Empty means every grade. Unconfigured and none-of-them are different
    #: states.
    applicable_grades: list[str] = Field(default_factory=list)
    applicable_locations: list[uuid.UUID] = Field(default_factory=list)


class SettingsIn(BaseModel):
    conversion_convention: ConversionConventionEnum | None = None
    rate_source: str | None = Field(default=None, max_length=120)
    advance_block_after_days: int | None = Field(default=None, ge=1, le=365)


class CityTierIn(BaseModel):
    location_id: uuid.UUID
    city_tier: CityTierEnum
    note: str | None = Field(default=None, max_length=500)


class MileageRateIn(BaseModel):
    vehicle_type: str = Field(min_length=1, max_length=40)
    rate_per_km: Decimal = Field(gt=0)
    effective_from: date
    tolerance_percent: Decimal | None = Field(default=None, ge=0, le=200)


class PerDiemRateIn(BaseModel):
    city_tier: CityTierEnum
    grade: str = Field(min_length=1, max_length=40)
    amount: Decimal = Field(gt=0)
    effective_from: date
    includes_meals: bool = True
    includes_accommodation: bool = False


class ExchangeRateIn(BaseModel):
    from_currency: str = Field(min_length=3, max_length=3)
    rate: Decimal = Field(gt=0)
    quote_date: date
    source: str = Field(min_length=1, max_length=120)
    to_currency: str = Field(default="INR", min_length=3, max_length=3)


class ClaimIn(BaseModel):
    employee_id: uuid.UUID
    category_key: str = Field(min_length=1, max_length=80)
    city_tier: CityTierEnum
    amount: Decimal = Field(gt=0)
    incurred_on: date
    kind: ClaimKindEnum = ClaimKindEnum.RECEIPTED
    currency: str = Field(default="INR", min_length=3, max_length=3)
    settlement_date: date | None = None
    has_receipt: bool = False
    justification: str | None = Field(default=None, max_length=2000)
    description: str | None = Field(default=None, max_length=2000)
    travel_request_id: uuid.UUID | None = None
    receipt_document_id: uuid.UUID | None = None
    distance_km: Decimal | None = Field(default=None, ge=0)
    route_km: Decimal | None = Field(default=None, ge=0)
    vehicle_type: str | None = Field(default=None, max_length=40)
    per_diem_days: int | None = Field(default=None, ge=1, le=366)


class SubmitIn(BaseModel):
    justification: str | None = Field(default=None, max_length=2000)


class DecisionIn(BaseModel):
    approve: bool
    note: str | None = Field(default=None, max_length=2000)


class TravelRequestIn(BaseModel):
    employee_id: uuid.UUID
    purpose: str = Field(min_length=1, max_length=2000)
    from_date: date
    to_date: date
    destinations: list[dict[str, Any]] = Field(default_factory=list)
    estimated_cost: Decimal = Field(default=Decimal("0"), ge=0)
    advance_requested: Decimal = Field(default=Decimal("0"), ge=0)


class AdvanceIn(BaseModel):
    travel_request_id: uuid.UUID
    amount: Decimal = Field(gt=0)
    issued_on: date
    disbursement_batch_id: uuid.UUID | None = None


class SettleIn(BaseModel):
    settled_on: date


# ── Posture ─────────────────────────────────────────────────────────────────


@router.get("/expenses/posture")
async def posture(user: CurrentUser) -> dict[str, Any]:
    """What the policy engine enforces, and what it does not.

    Not role-gated. An employee is entitled to know that a blocked claim is
    blocked by a rule and not by somebody's opinion.
    """
    return expenses.expense_posture()


# ── Configuration ───────────────────────────────────────────────────────────


@router.put("/expenses/settings")
async def set_settings(
    body: SettingsIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    row = await expenses.set_settings(
        session,
        user,
        conversion_convention=body.conversion_convention,
        rate_source=body.rate_source,
        advance_block_after_days=body.advance_block_after_days,
        request=request,
    )
    return {
        "conversion_convention": row.conversion_convention.value,
        "rate_source": row.rate_source,
        "reporting_currency": row.reporting_currency,
        "advance_block_after_days": row.advance_block_after_days,
    }


@router.post("/expenses/policies", status_code=status.HTTP_201_CREATED)
async def create_policy(
    body: PolicyIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    """Create a policy version. An existing one is never edited.

    Same rule as `PayrollRuleSet`: a March claim must keep reading as having
    been judged against March's limit.
    """
    row = await expenses.create_policy(
        session,
        user,
        name=body.name,
        effective_from=body.effective_from,
        rules=[r.model_dump() for r in body.rules],
        description=body.description,
        applicable_grades=body.applicable_grades,
        applicable_locations=[str(x) for x in body.applicable_locations],
        request=request,
    )
    return {
        "policy_id": str(row.id),
        "name": row.name,
        "effective_from": row.effective_from.isoformat(),
        "rule_count": len(body.rules),
    }


@router.put("/expenses/city-tiers")
async def set_city_tier(
    body: CityTierIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    row = await expenses.set_city_tier(
        session,
        user,
        location_id=body.location_id,
        city_tier=body.city_tier,
        note=body.note,
        request=request,
    )
    return {
        "location_id": str(row.location_id),
        "city_tier": row.city_tier.value,
    }


@router.post("/expenses/mileage-rates", status_code=status.HTTP_201_CREATED)
async def set_mileage_rate(
    body: MileageRateIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    """Add a mileage rate from a date. Earlier claims keep the earlier rate."""
    row = await expenses.set_mileage_rate(
        session,
        user,
        vehicle_type=body.vehicle_type,
        rate_per_km=body.rate_per_km,
        effective_from=body.effective_from,
        tolerance_percent=body.tolerance_percent,
        request=request,
    )
    return {
        "mileage_rate_id": str(row.id),
        "vehicle_type": row.vehicle_type,
        "rate_per_km": str(row.rate_per_km),
        "effective_from": row.effective_from.isoformat(),
    }


@router.post("/expenses/per-diem-rates", status_code=status.HTTP_201_CREATED)
async def set_per_diem_rate(
    body: PerDiemRateIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    row = await expenses.set_per_diem_rate(
        session,
        user,
        city_tier=body.city_tier,
        grade=body.grade,
        amount=body.amount,
        effective_from=body.effective_from,
        includes_meals=body.includes_meals,
        includes_accommodation=body.includes_accommodation,
        request=request,
    )
    return {
        "per_diem_rate_id": str(row.id),
        "grade": row.grade,
        "city_tier": row.city_tier.value,
        "amount": str(row.amount),
        "requires_receipt": False,
    }


@router.post("/expenses/exchange-rates", status_code=status.HTTP_201_CREATED)
async def record_exchange_rate(
    body: ExchangeRateIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> dict[str, Any]:
    """Record a quote. No feed is connected; rates are entered."""
    row = await expenses.record_exchange_rate(
        session,
        user,
        from_currency=body.from_currency,
        rate=body.rate,
        quote_date=body.quote_date,
        source=body.source,
        to_currency=body.to_currency,
        request=request,
    )
    return {
        "exchange_rate_id": str(row.id),
        "pair": f"{row.from_currency}/{row.to_currency}",
        "rate": str(row.rate),
        "quote_date": row.quote_date.isoformat(),
        "source": row.source,
    }


# ── Claims ──────────────────────────────────────────────────────────────────


@router.post("/expenses/claims", status_code=status.HTTP_201_CREATED)
async def capture_claim(
    body: ClaimIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
) -> dict[str, Any]:
    """Capture a claim and return its policy verdict in the same response.

    The verdict carries the limit, what has already been claimed against it, the
    overage and what the employee has to do next. This is the endpoint the slice
    exists for: policy that only appears at approval is a document, not a
    control.
    """
    return await expenses.capture_claim(
        session,
        user,
        employee_id=body.employee_id,
        category_key=body.category_key,
        city_tier=body.city_tier,
        amount=body.amount,
        incurred_on=body.incurred_on,
        kind=body.kind,
        currency=body.currency,
        settlement_date=body.settlement_date,
        has_receipt=body.has_receipt,
        justification=body.justification,
        description=body.description,
        travel_request_id=body.travel_request_id,
        receipt_document_id=body.receipt_document_id,
        distance_km=body.distance_km,
        route_km=body.route_km,
        vehicle_type=body.vehicle_type,
        per_diem_days=body.per_diem_days,
        request=request,
    )


@router.post("/expenses/claims/{claim_id}/submit")
async def submit_claim(
    claim_id: uuid.UUID,
    body: SubmitIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
) -> dict[str, Any]:
    """Submit a captured claim, honouring the verdict it was given at capture."""
    claim = await expenses.submit_claim(
        session,
        user,
        claim_id=claim_id,
        justification=body.justification,
        request=request,
    )
    return {
        "claim_id": str(claim.id),
        "status": claim.status.value,
        "policy_verdict": claim.policy_verdict.value,
        "flagged_for_approver": claim.flagged_for_approver,
        "justification": claim.justification,
    }


@router.post("/expenses/claims/{claim_id}/decision")
async def decide_claim(
    claim_id: uuid.UUID,
    body: DecisionIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> dict[str, Any]:
    claim = await expenses.decide_claim(
        session,
        user,
        claim_id=claim_id,
        approve=body.approve,
        note=body.note,
        request=request,
    )
    return {
        "claim_id": str(claim.id),
        "status": claim.status.value,
        "decided_at": claim.decided_at.isoformat() if claim.decided_at else None,
    }


@router.get("/expenses/violation-rates")
async def violation_rates(
    session: DbSession,
    user: CurrentUser,
    scope: str = "department",
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> dict[str, Any]:
    """Breach rates by employee, department or category, with the reading.

    Persistent violation is either a policy problem or a behaviour problem. The
    peer rate is what distinguishes them, so it is returned beside every group.
    """
    return await expenses.violation_rates(session, user, scope=scope)


# ── Travel and advances ─────────────────────────────────────────────────────


@router.post("/expenses/travel-requests", status_code=status.HTTP_201_CREATED)
async def create_travel_request(
    body: TravelRequestIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
) -> dict[str, Any]:
    row = await expenses.create_travel_request(
        session,
        user,
        employee_id=body.employee_id,
        purpose=body.purpose,
        from_date=body.from_date,
        to_date=body.to_date,
        destinations=body.destinations,
        estimated_cost=body.estimated_cost,
        advance_requested=body.advance_requested,
        request=request,
    )
    return {
        "travel_request_id": str(row.id),
        "status": row.status.value,
        "advance_requested": str(row.advance_requested),
    }


@router.post("/expenses/travel-requests/{travel_request_id}/decision")
async def decide_travel_request(
    travel_request_id: uuid.UUID,
    body: DecisionIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> dict[str, Any]:
    row = await expenses.decide_travel_request(
        session,
        user,
        travel_request_id=travel_request_id,
        approve=body.approve,
        note=body.note,
        request=request,
    )
    return {
        "travel_request_id": str(row.id),
        "status": row.status.value,
        "approvals": row.approvals,
    }


@router.post("/expenses/advances", status_code=status.HTTP_201_CREATED)
async def issue_advance(
    body: AdvanceIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> dict[str, Any]:
    """Issue an advance against an approved trip.

    Returns 409 with the blocking advance named when an older one is unsettled
    past the tenant's threshold. Refusing without saying which advance is the
    problem sends the employee to finance, and finance says yes.
    """
    row = await expenses.issue_advance(
        session,
        user,
        travel_request_id=body.travel_request_id,
        amount=body.amount,
        issued_on=body.issued_on,
        disbursement_batch_id=body.disbursement_batch_id,
        request=request,
    )
    return {
        "advance_id": str(row.id),
        "travel_request_id": str(row.travel_request_id),
        "amount": str(row.amount),
        "issued_on": row.issued_on.isoformat(),
        "status": row.status.value,
    }


@router.post("/expenses/advances/{advance_id}/settle")
async def settle_advance(
    advance_id: uuid.UUID,
    body: SettleIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> dict[str, Any]:
    """Settle an advance against its own trip's approved claims.

    Nothing here can net across trips: the claims that reach the engine are
    selected by the advance's `travel_request_id`.
    """
    return await expenses.settle_advance(
        session,
        user,
        advance_id=advance_id,
        settled_on=body.settled_on,
        request=request,
    )


@router.get("/expenses/advances/ageing")
async def advance_ageing(
    session: DbSession,
    user: CurrentUser,
    as_of: date | None = None,
    employee_id: uuid.UUID | None = None,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> dict[str, Any]:
    """Outstanding advances by age, for the person who chases them."""
    return await expenses.advance_ageing(
        session,
        user,
        as_of=as_of or date.today(),
        employee_id=employee_id,
    )


@router.get("/expenses/advances/block-check")
async def block_check(
    session: DbSession,
    user: CurrentUser,
    employee_id: uuid.UUID,
    as_of: date | None = None,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> dict[str, Any]:
    """Whether this employee may take another advance, and why not.

    Separate from issuing so the answer can be shown before somebody fills in a
    form, rather than as a refusal after they have.
    """
    report = await expenses.advance_ageing(
        session, user, as_of=as_of or date.today(), employee_id=employee_id
    )
    if report["blocking_count"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{report['blocking_count']} advance(s) unsettled beyond "
                f"{report['block_after_days']} days, totalling "
                f"{report['total_outstanding']}. Submit the claims for those "
                "trips, or repay the balance, and a new advance can be issued."
            ),
        )
    return {"permitted": True, "ageing": report}
