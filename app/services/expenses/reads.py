"""Expense reads: claims, reports, trips, advances, the finance queue and config.

Scope is decided by the router through `app.core.permissions` for lists (whose
records), and here for single records (a claim or report somebody asked for by
id). The finance queue never contains the caller's own items: nobody decides
their own claim, trip or settlement.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.employee import Employee
from app.models.expenses import (
    AdvanceSettlement,
    AdvanceStatusEnum,
    CityTierEnum,
    ClaimKindEnum,
    ExchangeRateQuote,
    ExpenseClaim,
    ExpenseClaimStatusEnum,
    ExpensePolicy,
    ExpensePolicyRule,
    ExpenseReport,
    MileageRate,
    PerDiemRate,
    PolicyVerdictEnum,
    TravelAdvance,
    TravelRequest,
    TravelRequestStatusEnum,
)
from app.models.org import Location
from app.models.user import User
from app.schemas.expenses import (
    AdvanceRead,
    BlockStatus,
    CityTierRead,
    ClaimRead,
    ExchangeRateRead,
    ExpenseMeta,
    MileageRateRead,
    PerDiemRateRead,
    PolicyRead,
    ReportRead,
    ReviewQueue,
    RuleRead,
    SettingsRead,
    TravelRequestRead,
)
from app.services.expenses import advances as advance_engine
from app.services.expenses import receipts
from app.services.expenses import service as expense_service

_LIST_LIMIT = 500

#: Verdicts that stop a line from being submitted even with a reason.
_BLOCKING_VERDICTS = {
    PolicyVerdictEnum.BLOCKED,
    PolicyVerdictEnum.RECEIPT_REQUIRED,
    PolicyVerdictEnum.PRE_APPROVAL_REQUIRED,
}


def _d(value) -> Decimal:
    return Decimal(str(value if value is not None else 0))


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def claim_read(claim: ExpenseClaim, employee: Employee | None) -> ClaimRead:
    return ClaimRead.model_validate(claim).model_copy(
        update={
            "employee_code": employee.employee_code if employee else None,
            "employee_name": employee.full_name if employee else None,
        }
    )


# ── Claims ──────────────────────────────────────────────────────────────────


async def list_claims(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID | None,
    status_filter: ExpenseClaimStatusEnum | None = None,
) -> list[ClaimRead]:
    """`employee_id=None` lists everybody — the router allows that only for finance."""
    stmt = select(ExpenseClaim, Employee).join(Employee, Employee.id == ExpenseClaim.employee_id)
    if employee_id is not None:
        stmt = stmt.where(ExpenseClaim.employee_id == employee_id)
    if status_filter is not None:
        stmt = stmt.where(ExpenseClaim.status == status_filter)
    stmt = stmt.order_by(ExpenseClaim.incurred_on.desc(), ExpenseClaim.created_at.desc()).limit(
        _LIST_LIMIT
    )
    rows = (await session.execute(tenant_scoped(stmt, ExpenseClaim, user))).all()
    return [claim_read(claim, employee) for claim, employee in rows]


async def get_claim(session: AsyncSession, user: User, claim_id: uuid.UUID) -> ClaimRead:
    row = (
        await session.execute(
            tenant_scoped(
                select(ExpenseClaim, Employee)
                .join(Employee, Employee.id == ExpenseClaim.employee_id)
                .where(ExpenseClaim.id == claim_id),
                ExpenseClaim,
                user,
            )
        )
    ).first()
    if row is None:
        raise _not_found("No such claim in this tenant.")
    claim, employee = row
    permissions.require_self_or_hr(user, claim.employee_id, detail="That claim is not yours.")
    return claim_read(claim, employee)


# ── Reports ─────────────────────────────────────────────────────────────────


def _report_read(
    report: ExpenseReport, employee: Employee | None, lines: list[ClaimRead]
) -> ReportRead:
    live = [line for line in lines if line.status is not ExpenseClaimStatusEnum.CANCELLED]
    return ReportRead(
        id=report.id,
        employee_id=report.employee_id,
        employee_name=employee.full_name if employee else None,
        title=report.title,
        travel_request_id=report.travel_request_id,
        status=report.status,
        submitted_at=report.submitted_at,
        decided_at=report.decided_at,
        created_at=report.created_at,
        line_count=len(live),
        total_amount=sum((_d(line.amount) for line in live), Decimal("0")),
        blocked_lines=sum(
            1
            for line in live
            if line.status is ExpenseClaimStatusEnum.DRAFT
            and line.policy_verdict in _BLOCKING_VERDICTS
        ),
        lines=lines,
    )


async def _reports(
    session: AsyncSession, user: User, stmt
) -> list[ReportRead]:
    pairs = (await session.execute(tenant_scoped(stmt, ExpenseReport, user))).all()
    if not pairs:
        return []
    ids = [report.id for report, _ in pairs]
    line_rows = (
        await session.execute(
            tenant_scoped(
                select(ExpenseClaim, Employee)
                .join(Employee, Employee.id == ExpenseClaim.employee_id)
                .where(ExpenseClaim.report_id.in_(ids))
                .order_by(ExpenseClaim.incurred_on),
                ExpenseClaim,
                user,
            )
        )
    ).all()
    by_report: dict[uuid.UUID, list[ClaimRead]] = {}
    for claim, employee in line_rows:
        by_report.setdefault(claim.report_id, []).append(claim_read(claim, employee))
    return [
        _report_read(report, employee, by_report.get(report.id, []))
        for report, employee in pairs
    ]


async def list_reports(
    session: AsyncSession, user: User, *, employee_id: uuid.UUID | None
) -> list[ReportRead]:
    stmt = select(ExpenseReport, Employee).join(
        Employee, Employee.id == ExpenseReport.employee_id
    )
    if employee_id is not None:
        stmt = stmt.where(ExpenseReport.employee_id == employee_id)
    stmt = stmt.order_by(ExpenseReport.created_at.desc()).limit(_LIST_LIMIT)
    return await _reports(session, user, stmt)


async def get_report(session: AsyncSession, user: User, report_id: uuid.UUID) -> ReportRead:
    stmt = (
        select(ExpenseReport, Employee)
        .join(Employee, Employee.id == ExpenseReport.employee_id)
        .where(ExpenseReport.id == report_id)
    )
    found = await _reports(session, user, stmt)
    if not found:
        raise _not_found("No such expense report in this tenant.")
    permissions.require_self_or_hr(
        user, found[0].employee_id, detail="That expense report is not yours."
    )
    return found[0]


# ── Travel ──────────────────────────────────────────────────────────────────


async def _issued_by_trip(
    session: AsyncSession, user: User, trip_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Decimal]:
    if not trip_ids:
        return {}
    stmt = tenant_scoped(
        select(TravelAdvance.travel_request_id, func.sum(TravelAdvance.amount))
        .where(
            TravelAdvance.travel_request_id.in_(trip_ids),
            TravelAdvance.status != AdvanceStatusEnum.CANCELLED,
        )
        .group_by(TravelAdvance.travel_request_id),
        TravelAdvance,
        user,
    )
    return {k: _d(v) for k, v in (await session.execute(stmt)).all()}


def _trip_read(
    trip: TravelRequest, employee: Employee | None, issued: Decimal
) -> TravelRequestRead:
    return TravelRequestRead(
        id=trip.id,
        employee_id=trip.employee_id,
        employee_code=employee.employee_code if employee else None,
        employee_name=employee.full_name if employee else None,
        purpose=trip.purpose,
        from_date=trip.from_date,
        to_date=trip.to_date,
        destinations=list(trip.destinations or []),
        estimated_cost=_d(trip.estimated_cost),
        advance_requested=_d(trip.advance_requested),
        advance_issued=issued,
        status=trip.status,
        approvals=list(trip.approvals or []),
        decided_at=trip.decided_at,
        created_at=trip.created_at,
    )


async def _trips(session: AsyncSession, user: User, stmt) -> list[TravelRequestRead]:
    pairs = (await session.execute(tenant_scoped(stmt, TravelRequest, user))).all()
    issued = await _issued_by_trip(session, user, [trip.id for trip, _ in pairs])
    return [
        _trip_read(trip, employee, issued.get(trip.id, Decimal("0")))
        for trip, employee in pairs
    ]


async def list_travel_requests(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID | None,
    status_filter: TravelRequestStatusEnum | None = None,
) -> list[TravelRequestRead]:
    stmt = select(TravelRequest, Employee).join(
        Employee, Employee.id == TravelRequest.employee_id
    )
    if employee_id is not None:
        stmt = stmt.where(TravelRequest.employee_id == employee_id)
    if status_filter is not None:
        stmt = stmt.where(TravelRequest.status == status_filter)
    stmt = stmt.order_by(TravelRequest.from_date.desc()).limit(_LIST_LIMIT)
    return await _trips(session, user, stmt)


async def get_travel_request(
    session: AsyncSession, user: User, travel_request_id: uuid.UUID
) -> TravelRequestRead:
    stmt = (
        select(TravelRequest, Employee)
        .join(Employee, Employee.id == TravelRequest.employee_id)
        .where(TravelRequest.id == travel_request_id)
    )
    found = await _trips(session, user, stmt)
    if not found:
        raise _not_found("No such travel request in this tenant.")
    return found[0]


# ── Advances ────────────────────────────────────────────────────────────────


def _facts(row: TravelAdvance) -> advance_engine.AdvanceFacts:
    return advance_engine.AdvanceFacts(
        advance_id=str(row.id),
        trip_id=str(row.travel_request_id),
        employee_id=str(row.employee_id),
        amount=_d(row.amount),
        issued_on=row.issued_on,
        claimed_against=_d(row.claimed_against),
        settled_on=row.settled_on,
        recovered=_d(row.recovered_amount),
        refunded=_d(row.refunded_amount),
    )


async def _advances(
    session: AsyncSession, user: User, stmt, *, today: date
) -> list[AdvanceRead]:
    rows = (await session.execute(tenant_scoped(stmt, TravelAdvance, user))).all()
    if not rows:
        return []
    settings = await expense_service.settings_row(session, user)
    ids = [advance.id for advance, _, _ in rows]
    refs = {
        advance_id: ref
        for advance_id, ref in (
            await session.execute(
                tenant_scoped(
                    select(AdvanceSettlement.advance_id, AdvanceSettlement.payroll_recovery_ref)
                    .where(AdvanceSettlement.advance_id.in_(ids)),
                    AdvanceSettlement,
                    user,
                )
            )
        ).all()
    }
    out: list[AdvanceRead] = []
    for advance, trip, employee in rows:
        facts = _facts(advance)
        age = facts.age_days(today)
        outstanding = facts.outstanding
        out.append(
            AdvanceRead(
                id=advance.id,
                travel_request_id=advance.travel_request_id,
                trip_purpose=trip.purpose if trip else None,
                trip_to_date=trip.to_date if trip else None,
                employee_id=advance.employee_id,
                employee_code=employee.employee_code if employee else None,
                employee_name=employee.full_name if employee else None,
                amount=_d(advance.amount),
                issued_on=advance.issued_on,
                status=advance.status,
                claimed_against=_d(advance.claimed_against),
                settled_on=advance.settled_on,
                recovered_amount=_d(advance.recovered_amount),
                refunded_amount=_d(advance.refunded_amount),
                outstanding=outstanding,
                age_days=age,
                is_blocking=(
                    not facts.is_settled
                    and outstanding > 0
                    and age > settings.advance_block_after_days
                ),
                payroll_recovery_ref=refs.get(advance.id),
            )
        )
    return out


def _advance_stmt():
    return (
        select(TravelAdvance, TravelRequest, Employee)
        .join(TravelRequest, TravelRequest.id == TravelAdvance.travel_request_id)
        .join(Employee, Employee.id == TravelAdvance.employee_id)
    )


async def list_advances(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID | None,
    status_filter: AdvanceStatusEnum | None = None,
    today: date,
) -> list[AdvanceRead]:
    stmt = _advance_stmt()
    if employee_id is not None:
        stmt = stmt.where(TravelAdvance.employee_id == employee_id)
    if status_filter is not None:
        stmt = stmt.where(TravelAdvance.status == status_filter)
    stmt = stmt.order_by(TravelAdvance.issued_on.desc()).limit(_LIST_LIMIT)
    return await _advances(session, user, stmt, today=today)


async def get_advance(
    session: AsyncSession, user: User, advance_id: uuid.UUID, *, today: date
) -> AdvanceRead:
    found = await _advances(
        session, user, _advance_stmt().where(TravelAdvance.id == advance_id), today=today
    )
    if not found:
        raise _not_found("No such advance in this tenant.")
    return found[0]


async def block_status(
    session: AsyncSession, user: User, *, employee_id: uuid.UUID, today: date
) -> BlockStatus:
    """Whether this person may take another advance, said before they ask."""
    rows = (
        await session.scalars(
            tenant_scoped(
                select(TravelAdvance).where(
                    TravelAdvance.employee_id == employee_id,
                    TravelAdvance.status != AdvanceStatusEnum.CANCELLED,
                ),
                TravelAdvance,
                user,
            )
        )
    ).all()
    settings = await expense_service.settings_row(session, user)
    decision = advance_engine.advance_block(
        [_facts(r) for r in rows],
        today=today,
        block_after_days=settings.advance_block_after_days,
    )
    return BlockStatus(
        employee_id=employee_id,
        permitted=decision.permitted,
        message=decision.message,
        block_after_days=settings.advance_block_after_days,
        blocking_count=len(decision.blocking),
        total_outstanding=decision.total_outstanding,
    )


# ── The finance queue ───────────────────────────────────────────────────────


async def review_queue(session: AsyncSession, user: User, *, today: date) -> ReviewQueue:
    """Everything waiting on finance, minus anything about the caller."""
    own = user.employee_id

    claim_stmt = (
        select(ExpenseClaim, Employee)
        .join(Employee, Employee.id == ExpenseClaim.employee_id)
        .where(ExpenseClaim.status == ExpenseClaimStatusEnum.SUBMITTED)
        .order_by(ExpenseClaim.submitted_at)
    )
    claim_rows = (await session.execute(tenant_scoped(claim_stmt, ExpenseClaim, user))).all()

    trip_stmt = (
        select(TravelRequest, Employee)
        .join(Employee, Employee.id == TravelRequest.employee_id)
        .where(TravelRequest.status == TravelRequestStatusEnum.SUBMITTED)
        .order_by(TravelRequest.from_date)
    )
    trips = await _trips(session, user, trip_stmt)

    advances = await _advances(
        session,
        user,
        _advance_stmt()
        .where(TravelAdvance.status == AdvanceStatusEnum.ISSUED)
        .order_by(TravelRequest.to_date),
        today=today,
    )

    excluded = (
        sum(1 for claim, _ in claim_rows if claim.employee_id == own)
        + sum(1 for trip in trips if trip.employee_id == own)
        + sum(1 for advance in advances if advance.employee_id == own)
        if own is not None
        else 0
    )
    return ReviewQueue(
        claims=[claim_read(c, e) for c, e in claim_rows if c.employee_id != own],
        travel_requests=[t for t in trips if t.employee_id != own],
        advances_to_settle=[a for a in advances if a.employee_id != own],
        excluded_own=excluded,
    )


# ── Configuration ───────────────────────────────────────────────────────────


async def _policy_reads(session: AsyncSession, user: User, policies) -> list[PolicyRead]:
    if not policies:
        return []
    rules = (
        await session.scalars(
            tenant_scoped(
                select(ExpensePolicyRule)
                .where(ExpensePolicyRule.policy_id.in_([p.id for p in policies]))
                .order_by(ExpensePolicyRule.category_key, ExpensePolicyRule.city_tier),
                ExpensePolicyRule,
                user,
            )
        )
    ).all()
    by_policy: dict[uuid.UUID, list[RuleRead]] = {}
    for rule in rules:
        by_policy.setdefault(rule.policy_id, []).append(RuleRead.model_validate(rule))
    return [
        PolicyRead(
            id=p.id,
            name=p.name,
            description=p.description,
            effective_from=p.effective_from,
            applicable_grades=[str(g) for g in (p.applicable_grades or [])],
            applicable_locations=[str(x) for x in (p.applicable_locations or [])],
            is_active=p.is_active,
            created_at=p.created_at,
            rules=by_policy.get(p.id, []),
        )
        for p in policies
    ]


async def list_policies(
    session: AsyncSession, user: User, *, include_inactive: bool = False
) -> list[PolicyRead]:
    stmt = select(ExpensePolicy).order_by(
        ExpensePolicy.effective_from.desc(), ExpensePolicy.name
    )
    if not include_inactive:
        stmt = stmt.where(ExpensePolicy.is_active.is_(True))
    policies = (await session.scalars(tenant_scoped(stmt, ExpensePolicy, user))).all()
    return await _policy_reads(session, user, list(policies))


async def get_policy(session: AsyncSession, user: User, policy_id: uuid.UUID) -> PolicyRead:
    policies = (
        await session.scalars(
            tenant_scoped(
                select(ExpensePolicy).where(ExpensePolicy.id == policy_id), ExpensePolicy, user
            )
        )
    ).all()
    found = await _policy_reads(session, user, list(policies))
    if not found:
        raise _not_found("No such expense policy in this tenant.")
    return found[0]


async def list_city_tiers(session: AsyncSession, user: User) -> list[CityTierRead]:
    """Every location, with its tier or none — an unclassified office is visible."""
    from app.models.expenses import LocationCityTier

    locations = (
        await session.scalars(
            tenant_scoped(select(Location).order_by(Location.name), Location, user)
        )
    ).all()
    tiers = {
        row.location_id: row
        for row in (
            await session.scalars(tenant_scoped(select(LocationCityTier), LocationCityTier, user))
        ).all()
    }
    return [
        CityTierRead(
            location_id=loc.id,
            location_name=loc.name,
            city=loc.city,
            city_tier=tiers[loc.id].city_tier if loc.id in tiers else None,
            note=tiers[loc.id].note if loc.id in tiers else None,
        )
        for loc in locations
    ]


async def list_mileage_rates(session: AsyncSession, user: User) -> list[MileageRateRead]:
    stmt = tenant_scoped(
        select(MileageRate).order_by(MileageRate.vehicle_type, MileageRate.effective_from.desc()),
        MileageRate,
        user,
    )
    return [MileageRateRead.model_validate(r) for r in (await session.scalars(stmt)).all()]


async def list_per_diem_rates(session: AsyncSession, user: User) -> list[PerDiemRateRead]:
    stmt = tenant_scoped(
        select(PerDiemRate).order_by(
            PerDiemRate.city_tier, PerDiemRate.grade, PerDiemRate.effective_from.desc()
        ),
        PerDiemRate,
        user,
    )
    return [PerDiemRateRead.model_validate(r) for r in (await session.scalars(stmt)).all()]


async def list_exchange_rates(session: AsyncSession, user: User) -> list[ExchangeRateRead]:
    stmt = tenant_scoped(
        select(ExchangeRateQuote)
        .order_by(ExchangeRateQuote.quote_date.desc(), ExchangeRateQuote.from_currency)
        .limit(_LIST_LIMIT),
        ExchangeRateQuote,
        user,
    )
    return [ExchangeRateRead.model_validate(r) for r in (await session.scalars(stmt)).all()]


async def get_settings(session: AsyncSession, user: User) -> SettingsRead:
    return SettingsRead.model_validate(await expense_service.settings_row(session, user))


async def meta(session: AsyncSession, user: User) -> ExpenseMeta:
    """What the claim form offers. Every option comes from configuration."""
    settings = await expense_service.settings_row(session, user)
    reporting = settings.reporting_currency.upper()

    categories = (
        await session.scalars(
            tenant_scoped(
                select(ExpensePolicyRule.category_key)
                .join(ExpensePolicy, ExpensePolicy.id == ExpensePolicyRule.policy_id)
                .where(ExpensePolicy.is_active.is_(True))
                .distinct()
                .order_by(ExpensePolicyRule.category_key),
                ExpensePolicyRule,
                user,
            )
        )
    ).all()
    vehicles = (
        await session.scalars(
            tenant_scoped(
                select(MileageRate.vehicle_type).distinct().order_by(MileageRate.vehicle_type),
                MileageRate,
                user,
            )
        )
    ).all()
    currencies = (
        await session.scalars(
            tenant_scoped(
                select(ExchangeRateQuote.from_currency)
                .where(ExchangeRateQuote.to_currency == reporting)
                .distinct()
                .order_by(ExchangeRateQuote.from_currency),
                ExchangeRateQuote,
                user,
            )
        )
    ).all()
    return ExpenseMeta(
        reporting_currency=reporting,
        conversion_convention=settings.conversion_convention,
        rate_source=settings.rate_source,
        categories=list(categories),
        vehicle_types=list(vehicles),
        currencies=list(currencies),
        city_tiers=list(CityTierEnum),
        claim_kinds=list(ClaimKindEnum),
        receipt_max_bytes=receipts.MAX_BYTES,
        receipt_types=sorted(receipts.ALLOWED_TYPES),
    )
