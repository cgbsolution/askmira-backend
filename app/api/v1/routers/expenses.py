"""Expense policy, claims, receipts, reports, travel and advances (slice 31).

`POST /expenses/claims` captures the claim **and returns the policy verdict in
the same response** — the limit, what has already been claimed against it, the
overage and what the employee must do next. There is deliberately no "evaluate
this claim" endpoint to call later.

Who may call what:

- Everybody, for themselves: claims, receipts, reports, trips, their advances and
  whether a new advance would be blocked. An employee id from the request goes
  through `app.core.permissions`; only finance may name somebody else.
- Finance (tenant admin, HR admin, HR ops): the review queue, decisions,
  advances, settlement, ageing, violation rates and configuration reads.
  Nobody decides their own claim, trip, advance or settlement.
- Tenant and HR admins: policy, tier, mileage, per-diem and settings writes.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any, Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)

from app.core import permissions
from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.expenses import (
    AdvanceStatusEnum,
    ExpenseClaimStatusEnum,
    TravelRequestStatusEnum,
)
from app.models.user import User, UserRole
from app.schemas.expenses import (
    AdvanceIn,
    AdvanceRead,
    BlockStatus,
    CityTierIn,
    CityTierRead,
    ClaimIn,
    ClaimRead,
    DecisionIn,
    ExchangeRateIn,
    ExchangeRateRead,
    ExpenseMeta,
    MileageRateIn,
    MileageRateRead,
    PerDiemRateIn,
    PerDiemRateRead,
    PolicyIn,
    PolicyRead,
    ReceiptRead,
    ReportCreate,
    ReportRead,
    ReportSubmitIn,
    ReviewQueue,
    SettingsIn,
    SettingsRead,
    SettleIn,
    SubmitIn,
    TravelRequestIn,
    TravelRequestRead,
)
from app.services.expenses import reads, receipt_store, receipts
from app.services.expenses import service as expenses

router = APIRouter(tags=["expenses"])

FINANCE_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN, UserRole.HR_OPS)
ADMIN_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN)

Scope = Literal["mine", "all"]


def _whose(user: User, scope: Scope, employee_id: uuid.UUID | None) -> uuid.UUID | None:
    """Whose records a list returns. `None` means everybody, for finance only."""
    if scope == "all":
        permissions.require_hr(user, detail="Only finance can list everybody's expenses.")
        return employee_id
    return permissions.resolve_employee(
        user, employee_id, detail="You can only see your own expenses."
    )


# ── Posture and reference ───────────────────────────────────────────────────


@router.get("/expenses/posture")
async def posture(user: CurrentUser) -> dict[str, Any]:
    """What the policy engine enforces, and what it does not."""
    return expenses.expense_posture()


@router.get("/expenses/meta", response_model=ExpenseMeta)
async def meta(user: CurrentUser, session: DbSession) -> ExpenseMeta:
    """Categories, vehicles and currencies the claim form may offer — from configuration."""
    return await reads.meta(session, user)


# ── Configuration ───────────────────────────────────────────────────────────


@router.get("/expenses/settings", response_model=SettingsRead)
async def get_settings(
    session: DbSession, user: CurrentUser, _finance=Depends(require_roles(*FINANCE_ROLES))
) -> SettingsRead:
    return await reads.get_settings(session, user)


@router.put("/expenses/settings", response_model=SettingsRead)
async def set_settings(
    body: SettingsIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> SettingsRead:
    row = await expenses.set_settings(
        session,
        user,
        conversion_convention=body.conversion_convention,
        rate_source=body.rate_source,
        advance_block_after_days=body.advance_block_after_days,
        request=request,
    )
    return SettingsRead.model_validate(row)


@router.get("/expenses/policies", response_model=list[PolicyRead])
async def list_policies(
    session: DbSession,
    user: CurrentUser,
    include_inactive: bool = False,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> list[PolicyRead]:
    return await reads.list_policies(session, user, include_inactive=include_inactive)


@router.post("/expenses/policies", status_code=status.HTTP_201_CREATED, response_model=PolicyRead)
async def create_policy(
    body: PolicyIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> PolicyRead:
    """Create a policy version. An existing one is never edited."""
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
    return await reads.get_policy(session, user, row.id)


@router.post("/expenses/policies/{policy_id}/deactivate", response_model=PolicyRead)
async def deactivate_policy(
    policy_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> PolicyRead:
    """Retire a policy. It is kept, because claims judged under it point at its rules."""
    row = await expenses.deactivate_policy(session, user, policy_id=policy_id, request=request)
    return await reads.get_policy(session, user, row.id)


@router.get("/expenses/city-tiers", response_model=list[CityTierRead])
async def list_city_tiers(
    session: DbSession, user: CurrentUser, _finance=Depends(require_roles(*FINANCE_ROLES))
) -> list[CityTierRead]:
    return await reads.list_city_tiers(session, user)


@router.put("/expenses/city-tiers", response_model=CityTierRead)
async def set_city_tier(
    body: CityTierIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> CityTierRead:
    rows = await reads.list_city_tiers(session, user)
    if not any(r.location_id == body.location_id for r in rows):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Location not found")
    await expenses.set_city_tier(
        session,
        user,
        location_id=body.location_id,
        city_tier=body.city_tier,
        note=body.note,
        request=request,
    )
    rows = await reads.list_city_tiers(session, user)
    return next(r for r in rows if r.location_id == body.location_id)


@router.get("/expenses/mileage-rates", response_model=list[MileageRateRead])
async def list_mileage_rates(
    session: DbSession, user: CurrentUser, _finance=Depends(require_roles(*FINANCE_ROLES))
) -> list[MileageRateRead]:
    return await reads.list_mileage_rates(session, user)


@router.post(
    "/expenses/mileage-rates", status_code=status.HTTP_201_CREATED, response_model=MileageRateRead
)
async def set_mileage_rate(
    body: MileageRateIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> MileageRateRead:
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
    return MileageRateRead.model_validate(row)


@router.get("/expenses/per-diem-rates", response_model=list[PerDiemRateRead])
async def list_per_diem_rates(
    session: DbSession, user: CurrentUser, _finance=Depends(require_roles(*FINANCE_ROLES))
) -> list[PerDiemRateRead]:
    return await reads.list_per_diem_rates(session, user)


@router.post(
    "/expenses/per-diem-rates", status_code=status.HTTP_201_CREATED, response_model=PerDiemRateRead
)
async def set_per_diem_rate(
    body: PerDiemRateIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> PerDiemRateRead:
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
    return PerDiemRateRead.model_validate(row)


@router.get("/expenses/exchange-rates", response_model=list[ExchangeRateRead])
async def list_exchange_rates(
    session: DbSession, user: CurrentUser, _finance=Depends(require_roles(*FINANCE_ROLES))
) -> list[ExchangeRateRead]:
    return await reads.list_exchange_rates(session, user)


@router.post(
    "/expenses/exchange-rates", status_code=status.HTTP_201_CREATED, response_model=ExchangeRateRead
)
async def record_exchange_rate(
    body: ExchangeRateIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> ExchangeRateRead:
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
    return ExchangeRateRead.model_validate(row)


# ── Receipts ────────────────────────────────────────────────────────────────


@router.post("/expenses/receipts", status_code=status.HTTP_201_CREATED, response_model=ReceiptRead)
async def upload_receipt(
    session: DbSession,
    user: CurrentUser,
    request: Request,
    file: UploadFile = File(...),
    employee_id: uuid.UUID | None = Form(default=None),
) -> ReceiptRead:
    """Upload a receipt before capturing the claim it supports.

    Size and type are checked on the bytes; the filename and the browser's
    content type are ignored.
    """
    owner = permissions.resolve_employee(
        user, employee_id, detail="You can only upload receipts for your own claims."
    )
    data = await file.read(receipts.MAX_BYTES + 1)
    row = await receipt_store.store_receipt(
        session, user, employee_id=owner, data=data, filename=file.filename, request=request
    )
    return ReceiptRead.model_validate(row)


@router.get("/expenses/receipts/{receipt_id}", response_model=None)
async def download_receipt(
    receipt_id: uuid.UUID, session: DbSession, user: CurrentUser
) -> Response:
    """The receipt, as an attachment, to its owner or finance."""
    row = await receipt_store.receipt_for_download(session, user, receipt_id)
    return Response(
        content=row.content,
        media_type=row.content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{row.display_name}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


# ── Claims ──────────────────────────────────────────────────────────────────


@router.post("/expenses/claims", status_code=status.HTTP_201_CREATED)
async def capture_claim(
    body: ClaimIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
) -> dict[str, Any]:
    """Capture a claim and return its policy verdict in the same response."""
    employee_id = permissions.resolve_employee(
        user, body.employee_id, detail="You can only file expense claims for yourself."
    )
    # A paper receipt seen in person is finance's call about somebody else's
    # claim. Anybody else — finance included, for their own — attaches the file.
    paper_receipt = body.has_receipt and permissions.is_hr(user) and (
        employee_id != user.employee_id
    )
    return await expenses.capture_claim(
        session,
        user,
        employee_id=employee_id,
        category_key=body.category_key,
        city_tier=body.city_tier,
        amount=body.amount,
        incurred_on=body.incurred_on,
        kind=body.kind,
        currency=body.currency,
        settlement_date=body.settlement_date,
        has_receipt=paper_receipt,
        justification=body.justification,
        description=body.description,
        travel_request_id=body.travel_request_id,
        receipt_document_id=body.receipt_document_id,
        distance_km=body.distance_km,
        route_km=body.route_km,
        vehicle_type=body.vehicle_type,
        per_diem_days=body.per_diem_days,
        report_id=body.report_id,
        request=request,
    )


@router.get("/expenses/claims", response_model=list[ClaimRead])
async def list_claims(
    session: DbSession,
    user: CurrentUser,
    scope: Scope = "mine",
    employee_id: uuid.UUID | None = None,
    status_filter: ExpenseClaimStatusEnum | None = None,
) -> list[ClaimRead]:
    return await reads.list_claims(
        session, user, employee_id=_whose(user, scope, employee_id), status_filter=status_filter
    )


@router.get("/expenses/claims/{claim_id}", response_model=ClaimRead)
async def get_claim(claim_id: uuid.UUID, session: DbSession, user: CurrentUser) -> ClaimRead:
    return await reads.get_claim(session, user, claim_id)


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
        session, user, claim_id=claim_id, justification=body.justification, request=request
    )
    return {
        "claim_id": str(claim.id),
        "status": claim.status.value,
        "policy_verdict": claim.policy_verdict.value,
        "flagged_for_approver": claim.flagged_for_approver,
        "justification": claim.justification,
    }


@router.post("/expenses/claims/{claim_id}/cancel", response_model=ClaimRead)
async def cancel_claim(
    claim_id: uuid.UUID, session: DbSession, user: CurrentUser, request: Request
) -> ClaimRead:
    await expenses.cancel_claim(session, user, claim_id=claim_id, request=request)
    return await reads.get_claim(session, user, claim_id)


@router.post("/expenses/claims/{claim_id}/decision")
async def decide_claim(
    claim_id: uuid.UUID,
    body: DecisionIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> dict[str, Any]:
    """Approve or reject, with a reason for a rejection. Never your own claim."""
    claim = await expenses.decide_claim(
        session, user, claim_id=claim_id, approve=body.approve, note=body.note, request=request
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
    """Breach rates by employee, department or category, with the peer rate."""
    return await expenses.violation_rates(session, user, scope=scope)


# ── Reports ─────────────────────────────────────────────────────────────────


@router.post("/expenses/reports", status_code=status.HTTP_201_CREATED, response_model=ReportRead)
async def create_report(
    body: ReportCreate, session: DbSession, user: CurrentUser, request: Request
) -> ReportRead:
    """Start a claim with line items. Lines are captured with `report_id`."""
    employee_id = permissions.resolve_employee(
        user, body.employee_id, detail="You can only start expense reports for yourself."
    )
    row = await expenses.create_report(
        session,
        user,
        employee_id=employee_id,
        title=body.title,
        travel_request_id=body.travel_request_id,
        request=request,
    )
    return await reads.get_report(session, user, row.id)


@router.get("/expenses/reports", response_model=list[ReportRead])
async def list_reports(
    session: DbSession,
    user: CurrentUser,
    scope: Scope = "mine",
    employee_id: uuid.UUID | None = None,
) -> list[ReportRead]:
    return await reads.list_reports(session, user, employee_id=_whose(user, scope, employee_id))


@router.get("/expenses/reports/{report_id}", response_model=ReportRead)
async def get_report(report_id: uuid.UUID, session: DbSession, user: CurrentUser) -> ReportRead:
    return await reads.get_report(session, user, report_id)


@router.post("/expenses/reports/{report_id}/submit", response_model=ReportRead)
async def submit_report(
    report_id: uuid.UUID,
    body: ReportSubmitIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
) -> ReportRead:
    """Submit every line together; a blocked line refuses the report and is named."""
    await expenses.submit_report(
        session, user, report_id=report_id, justifications=body.justifications, request=request
    )
    return await reads.get_report(session, user, report_id)


# ── Finance queue ───────────────────────────────────────────────────────────


@router.get("/expenses/review-queue", response_model=ReviewQueue)
async def review_queue(
    session: DbSession, user: CurrentUser, _finance=Depends(require_roles(*FINANCE_ROLES))
) -> ReviewQueue:
    """Claims, trips and advance settlements waiting on finance — never the caller's own."""
    return await reads.review_queue(session, user, today=date.today())


# ── Travel and advances ─────────────────────────────────────────────────────


@router.post(
    "/expenses/travel-requests",
    status_code=status.HTTP_201_CREATED,
    response_model=TravelRequestRead,
)
async def create_travel_request(
    body: TravelRequestIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
) -> TravelRequestRead:
    employee_id = permissions.resolve_employee(
        user, body.employee_id, detail="You can only raise travel requests for yourself."
    )
    row = await expenses.create_travel_request(
        session,
        user,
        employee_id=employee_id,
        purpose=body.purpose,
        from_date=body.from_date,
        to_date=body.to_date,
        destinations=body.destinations,
        estimated_cost=body.estimated_cost,
        advance_requested=body.advance_requested,
        request=request,
    )
    return await reads.get_travel_request(session, user, row.id)


@router.get("/expenses/travel-requests", response_model=list[TravelRequestRead])
async def list_travel_requests(
    session: DbSession,
    user: CurrentUser,
    scope: Scope = "mine",
    employee_id: uuid.UUID | None = None,
    status_filter: TravelRequestStatusEnum | None = None,
) -> list[TravelRequestRead]:
    return await reads.list_travel_requests(
        session, user, employee_id=_whose(user, scope, employee_id), status_filter=status_filter
    )


@router.post(
    "/expenses/travel-requests/{travel_request_id}/cancel", response_model=TravelRequestRead
)
async def cancel_travel_request(
    travel_request_id: uuid.UUID, session: DbSession, user: CurrentUser, request: Request
) -> TravelRequestRead:
    await expenses.cancel_travel_request(
        session, user, travel_request_id=travel_request_id, request=request
    )
    return await reads.get_travel_request(session, user, travel_request_id)


@router.post(
    "/expenses/travel-requests/{travel_request_id}/decision", response_model=TravelRequestRead
)
async def decide_travel_request(
    travel_request_id: uuid.UUID,
    body: DecisionIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> TravelRequestRead:
    row = await expenses.decide_travel_request(
        session,
        user,
        travel_request_id=travel_request_id,
        approve=body.approve,
        note=body.note,
        request=request,
    )
    return await reads.get_travel_request(session, user, row.id)


@router.get("/expenses/advances", response_model=list[AdvanceRead])
async def list_advances(
    session: DbSession,
    user: CurrentUser,
    scope: Scope = "mine",
    employee_id: uuid.UUID | None = None,
    status_filter: AdvanceStatusEnum | None = None,
) -> list[AdvanceRead]:
    return await reads.list_advances(
        session,
        user,
        employee_id=_whose(user, scope, employee_id),
        status_filter=status_filter,
        today=date.today(),
    )


@router.get("/expenses/advances/block-status", response_model=BlockStatus)
async def block_status(
    session: DbSession,
    user: CurrentUser,
    employee_id: uuid.UUID | None = None,
    as_of: date | None = None,
) -> BlockStatus:
    """Whether a new advance would be blocked, said before anybody asks for one."""
    target = permissions.resolve_employee(
        user, employee_id, detail="You can only check your own advances."
    )
    return await reads.block_status(session, user, employee_id=target, today=as_of or date.today())


@router.post("/expenses/advances", status_code=status.HTTP_201_CREATED, response_model=AdvanceRead)
async def issue_advance(
    body: AdvanceIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> AdvanceRead:
    """Issue an advance against an approved trip; 409 naming the blocking advance."""
    row = await expenses.issue_advance(
        session,
        user,
        travel_request_id=body.travel_request_id,
        amount=body.amount,
        issued_on=body.issued_on,
        disbursement_batch_id=body.disbursement_batch_id,
        request=request,
    )
    return await reads.get_advance(session, user, row.id, today=date.today())


@router.post("/expenses/advances/{advance_id}/settle")
async def settle_advance(
    advance_id: uuid.UUID,
    body: SettleIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> dict[str, Any]:
    """Settle an advance against its own trip's approved claims."""
    return await expenses.settle_advance(
        session, user, advance_id=advance_id, settled_on=body.settled_on, request=request
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
        session, user, as_of=as_of or date.today(), employee_id=employee_id
    )


@router.get("/expenses/advances/block-check")
async def block_check(
    session: DbSession,
    user: CurrentUser,
    employee_id: uuid.UUID,
    as_of: date | None = None,
    _finance=Depends(require_roles(*FINANCE_ROLES)),
) -> dict[str, Any]:
    """Whether this employee may take another advance; 409 with the reason if not."""
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
