"""The DB-facing half of expenses: capture, submission, travel and advances.

The limits, the aggregation windows, the rate resolution, the settlement
arithmetic and the block all live in the pure modules beside this
(`policy`, `rates`, `advances`). An invariant checks the isolation.

═══════════════════════════════════════════════════════════════════════════════
EVALUATION HAPPENS IN `capture_claim`, WHICH IS THE POINT OF THE SLICE
═══════════════════════════════════════════════════════════════════════════════

There is no `evaluate at approval` path. The verdict is produced when the claim
is captured, returned in the same response, and written to the row. An employee
who learns three weeks later that their claim was out of policy — after an
approval queue and a rejection — is an employee who stops claiming properly, and
finance loses the only signal it had about where its limits are wrong.

`submit_claim` re-reads the stored verdict rather than recomputing it. That is
deliberate: a limit tightened between capture and submission must not turn a
claim the employee was told was fine into a breach they cannot see.

**What is and is not wired**, stated once here:

- A recovery from an under-spent advance is recorded with a `payroll_recovery_ref`
  of `None` and **nothing in payroll picks it up yet**. The balance is therefore
  visible and unrecovered rather than silently netted.
- Advance issue records `disbursement_batch_id` when given; **nothing puts an
  advance into a payment file automatically**.
- Exchange rates are read from `exchange_rate_quotes`, which is populated by an
  admin endpoint. **No rate feed is connected.**
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.expenses import (
    AdvanceSettlement,
    AdvanceStatusEnum,
    CityTierEnum,
    ClaimKindEnum,
    ConversionConventionEnum,
    ExchangeRateQuote,
    ExpenseClaim,
    ExpenseClaimStatusEnum,
    ExpensePolicy,
    ExpensePolicyRule,
    ExpensePolicyViolation,
    LimitTypeEnum,
    LocationCityTier,
    MileageRate,
    PerDiemRate,
    PolicyVerdictEnum,
    SettlementOutcomeEnum,
    TenantExpenseSettings,
    TravelAdvance,
    TravelRequest,
    TravelRequestStatusEnum,
    ViolationActionEnum,
)
from app.models.user import User
from app.services import audit
from app.services.expenses import advances as advance_engine
from app.services.expenses import policy as policy_engine
from app.services.expenses import rates as rate_engine

__all__ = [
    "advance_ageing",
    "capture_claim",
    "create_policy",
    "create_travel_request",
    "decide_claim",
    "decide_travel_request",
    "expense_posture",
    "issue_advance",
    "record_exchange_rate",
    "set_city_tier",
    "set_mileage_rate",
    "set_per_diem_rate",
    "set_settings",
    "settle_advance",
    "submit_claim",
    "violation_rates",
]


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _d(value: Any) -> Decimal:
    """Every amount off a row becomes a Decimal before it is used.

    `Numeric` already returns one, but a default of `0` on a freshly built row
    is an int until it has been through the database, and mixing the two in
    money arithmetic is how a float creeps in.
    """
    return Decimal(str(value if value is not None else 0))


# ══════════════════════════════════════════════════════════════════════════════
# Settings, policy and rates
# ══════════════════════════════════════════════════════════════════════════════


async def _settings(session: AsyncSession, user: User) -> TenantExpenseSettings:
    stmt = tenant_scoped(select(TenantExpenseSettings), TenantExpenseSettings, user)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        row = TenantExpenseSettings(tenant_id=user.tenant_id)
        session.add(row)
        await session.flush()
    return row


async def set_settings(
    session: AsyncSession,
    user: User,
    *,
    conversion_convention: ConversionConventionEnum | None = None,
    rate_source: str | None = None,
    advance_block_after_days: int | None = None,
    request: Request | None = None,
) -> TenantExpenseSettings:
    row = await _settings(session, user)
    if conversion_convention is not None:
        row.conversion_convention = conversion_convention
    if rate_source is not None:
        row.rate_source = rate_source
    if advance_block_after_days is not None:
        if advance_block_after_days < 1:
            raise _bad_request(
                "A block threshold of zero days would block every employee who "
                "has ever taken an advance, including one issued this morning."
            )
        row.advance_block_after_days = advance_block_after_days

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.RULE_CHANGE,
        entity_type="tenant_expense_settings",
        entity_id=row.id,
        actor=user,
        summary=(
            f"Expense settings: convert at {row.conversion_convention.value}, "
            f"source {row.rate_source}, advances block after "
            f"{row.advance_block_after_days} days."
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def create_policy(
    session: AsyncSession,
    user: User,
    *,
    name: str,
    effective_from: date,
    rules: list[dict[str, Any]],
    description: str | None = None,
    applicable_grades: list[str] | None = None,
    applicable_locations: list[str] | None = None,
    request: Request | None = None,
) -> ExpensePolicy:
    """A new policy version. Never an edit of an existing one.

    Same rule as `PayrollRuleSet`, for the same reason: a claim evaluated in
    March under a ₹8,000 hotel limit must still read as having been evaluated
    under ₹8,000 after the limit moves in April.
    """
    if not rules:
        raise _bad_request(
            "A policy with no rules governs nothing — every claim under it would "
            "return 'no rule' and pass. Add at least one limit."
        )

    row = ExpensePolicy(
        tenant_id=user.tenant_id,
        name=name,
        description=description,
        effective_from=effective_from,
        applicable_grades=list(applicable_grades or []),
        applicable_locations=[str(x) for x in (applicable_locations or [])],
        created_by_user_id=user.id,
        is_active=True,
    )
    session.add(row)
    await session.flush()

    seen: set[tuple[str, CityTierEnum]] = set()
    for spec in rules:
        key = (str(spec["category_key"]), CityTierEnum(spec["city_tier"]))
        if key in seen:
            raise _bad_request(
                f"Two rules for {key[0]} in tier {key[1].value}. The second "
                "would be unreachable and nobody could tell which limit "
                "applies."
            )
        seen.add(key)
        session.add(
            ExpensePolicyRule(
                tenant_id=user.tenant_id,
                policy_id=row.id,
                category_key=key[0],
                city_tier=key[1],
                limit_type=LimitTypeEnum(spec["limit_type"]),
                limit_amount=_d(spec["limit_amount"]),
                violation_action=ViolationActionEnum(spec["violation_action"]),
                requires_receipt=bool(spec.get("requires_receipt", True)),
                receipt_threshold=_d(spec.get("receipt_threshold", 0)),
                requires_pre_approval=bool(spec.get("requires_pre_approval", False)),
                is_per_diem=bool(spec.get("is_per_diem", False)),
            )
        )

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.RULE_CHANGE,
        entity_type="expense_policy",
        entity_id=row.id,
        actor=user,
        summary=(
            f"Expense policy '{name}' v{effective_from.isoformat()} created "
            f"with {len(rules)} rule(s)."
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def set_city_tier(
    session: AsyncSession,
    user: User,
    *,
    location_id: uuid.UUID,
    city_tier: CityTierEnum,
    note: str | None = None,
    request: Request | None = None,
) -> LocationCityTier:
    stmt = tenant_scoped(
        select(LocationCityTier).where(LocationCityTier.location_id == location_id),
        LocationCityTier,
        user,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        row = LocationCityTier(
            tenant_id=user.tenant_id, location_id=location_id, city_tier=city_tier
        )
        session.add(row)
    else:
        row.city_tier = city_tier
    row.note = note
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.RULE_CHANGE,
        entity_type="location_city_tier",
        entity_id=location_id,
        actor=user,
        summary=f"Location classified as tier {city_tier.value}.",
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def set_mileage_rate(
    session: AsyncSession,
    user: User,
    *,
    vehicle_type: str,
    rate_per_km: Decimal,
    effective_from: date,
    tolerance_percent: Decimal | None = None,
    request: Request | None = None,
) -> MileageRate:
    """A rate change inserts a row. It never updates one.

    A March claim was reimbursed at the March rate and the payment has been
    made; recomputing it from April's rate produces a number that matches
    nothing anybody paid.
    """
    row = MileageRate(
        tenant_id=user.tenant_id,
        vehicle_type=vehicle_type,
        rate_per_km=_d(rate_per_km),
        effective_from=effective_from,
        tolerance_percent=_d(tolerance_percent if tolerance_percent is not None else 15),
    )
    session.add(row)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.RULE_CHANGE,
        entity_type="mileage_rate",
        entity_id=row.id,
        actor=user,
        summary=(
            f"Mileage rate for {vehicle_type} set to {rate_per_km}/km from "
            f"{effective_from.isoformat()}. Earlier claims keep their rate."
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def set_per_diem_rate(
    session: AsyncSession,
    user: User,
    *,
    city_tier: CityTierEnum,
    grade: str,
    amount: Decimal,
    effective_from: date,
    includes_meals: bool = True,
    includes_accommodation: bool = False,
    request: Request | None = None,
) -> PerDiemRate:
    row = PerDiemRate(
        tenant_id=user.tenant_id,
        city_tier=city_tier,
        grade=grade,
        amount=_d(amount),
        effective_from=effective_from,
        includes_meals=includes_meals,
        includes_accommodation=includes_accommodation,
    )
    session.add(row)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.RULE_CHANGE,
        entity_type="per_diem_rate",
        entity_id=row.id,
        actor=user,
        summary=(
            f"Per-diem for grade {grade} in tier {city_tier.value} set to "
            f"{amount} from {effective_from.isoformat()}."
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def record_exchange_rate(
    session: AsyncSession,
    user: User,
    *,
    from_currency: str,
    rate: Decimal,
    quote_date: date,
    source: str,
    to_currency: str = "INR",
    request: Request | None = None,
) -> ExchangeRateQuote:
    row = ExchangeRateQuote(
        tenant_id=user.tenant_id,
        from_currency=from_currency.upper(),
        to_currency=to_currency.upper(),
        rate=_d(rate),
        quote_date=quote_date,
        source=source,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


# ══════════════════════════════════════════════════════════════════════════════
# Resolving what applies to this claim
# ══════════════════════════════════════════════════════════════════════════════


async def _applicable_rules(
    session: AsyncSession,
    user: User,
    *,
    employee: Employee,
    as_of: date,
) -> tuple[ExpensePolicy | None, list[ExpensePolicyRule]]:
    """The policy in force for this employee on this date, and its rules.

    Resolved by the claim's own date. Where several policies apply, the most
    recently effective wins; a tie is refused rather than resolved arbitrarily,
    because an arbitrary winner is a limit nobody chose and the employee cannot
    be told which one they are being held to.
    """
    stmt = tenant_scoped(
        select(ExpensePolicy)
        .where(ExpensePolicy.is_active.is_(True))
        .where(ExpensePolicy.effective_from <= as_of),
        ExpensePolicy,
        user,
    )
    candidates = list((await session.execute(stmt)).scalars())

    def applies(p: ExpensePolicy) -> bool:
        grades = list(p.applicable_grades or [])
        locations = list(p.applicable_locations or [])
        # An empty list means "every grade" / "every location". Unconfigured
        # and none-of-them have to be different states, or a policy saved
        # before grades were set would silently govern nobody.
        if grades and (employee.grade or "") not in grades:
            return False
        return not (locations and str(employee.location_id) not in locations)

    matching = [p for p in candidates if applies(p)]
    if not matching:
        return None, []

    latest = max(p.effective_from for p in matching)
    winners = [p for p in matching if p.effective_from == latest]
    if len(winners) > 1:
        raise _bad_request(
            f"{len(winners)} policies effective {latest.isoformat()} apply to "
            "this employee. Which limit governs is not something this system "
            "should pick for you — narrow the grades or locations on one of "
            f"them: {sorted(p.name for p in winners)}."
        )

    policy = winners[0]
    rule_stmt = tenant_scoped(
        select(ExpensePolicyRule).where(ExpensePolicyRule.policy_id == policy.id),
        ExpensePolicyRule,
        user,
    )
    return policy, list((await session.execute(rule_stmt)).scalars())


def _to_engine_rule(row: ExpensePolicyRule) -> policy_engine.ExpenseRule:
    return policy_engine.ExpenseRule(
        category_key=row.category_key,
        city_tier=policy_engine.CityTier(row.city_tier.value),
        limit_type=policy_engine.LimitType(row.limit_type.value),
        limit_amount=_d(row.limit_amount),
        violation_action=policy_engine.ViolationAction(row.violation_action.value),
        requires_receipt=row.requires_receipt,
        receipt_threshold=_d(row.receipt_threshold),
        requires_pre_approval=row.requires_pre_approval,
        is_per_diem=row.is_per_diem,
    )


async def _prior_claims(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    category_key: str,
) -> list[policy_engine.PriorClaim]:
    """What already counts toward an aggregating limit.

    Cancelled and rejected claims are excluded: a claim the company refused is
    not spend, and counting it would consume a limit the employee never used.
    """
    stmt = tenant_scoped(
        select(ExpenseClaim)
        .where(ExpenseClaim.employee_id == employee_id)
        .where(ExpenseClaim.category_key == category_key)
        .where(
            ExpenseClaim.status.notin_(
                [
                    ExpenseClaimStatusEnum.CANCELLED,
                    ExpenseClaimStatusEnum.REJECTED,
                ]
            )
        ),
        ExpenseClaim,
        user,
    )
    return [
        policy_engine.PriorClaim(
            category_key=row.category_key,
            amount=_d(row.amount),
            incurred_on=row.incurred_on,
            trip_id=str(row.travel_request_id) if row.travel_request_id else None,
        )
        for row in (await session.execute(stmt)).scalars()
    ]


async def _convert_if_foreign(
    session: AsyncSession,
    user: User,
    *,
    amount: Decimal,
    currency: str,
    incurred_on: date,
    settlement_date: date | None,
) -> tuple[Decimal, rate_engine.ConvertedAmount | None]:
    settings = await _settings(session, user)
    reporting = settings.reporting_currency.upper()
    if currency.upper() == reporting:
        return policy_engine.money(amount), None

    convention = rate_engine.ConversionConvention(
        settings.conversion_convention.value
    )
    rate_date = (
        settlement_date
        if convention is rate_engine.ConversionConvention.SETTLEMENT_DATE
        else incurred_on
    )
    if rate_date is None:
        raise _bad_request(
            "This tenant converts at the settlement date and this claim has not "
            "settled. Falling back to the transaction date would give a "
            "different number under a convention nobody chose."
        )

    stmt = tenant_scoped(
        select(ExchangeRateQuote)
        .where(ExchangeRateQuote.from_currency == currency.upper())
        .where(ExchangeRateQuote.to_currency == reporting)
        .where(ExchangeRateQuote.quote_date <= rate_date)
        .order_by(ExchangeRateQuote.quote_date.desc()),
        ExchangeRateQuote,
        user,
    )
    quote = (await session.execute(stmt)).scalars().first()
    if quote is None:
        raise _bad_request(
            f"No {currency.upper()}/{reporting} rate on or before "
            f"{rate_date.isoformat()}. Inventing one would put a number in the "
            "ledger that no source stands behind — record the quote first."
        )

    try:
        converted = rate_engine.convert(
            amount=amount,
            currency=currency.upper(),
            transaction_date=incurred_on,
            settlement_date=settlement_date,
            convention=convention,
            rate=_d(quote.rate),
            rate_source=quote.source,
            to_currency=reporting,
        )
    except rate_engine.RateError as exc:
        raise _bad_request(str(exc)) from exc
    return converted.converted_amount, converted


# ══════════════════════════════════════════════════════════════════════════════
# Capture — where the verdict is produced
# ══════════════════════════════════════════════════════════════════════════════


async def capture_claim(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    category_key: str,
    city_tier: CityTierEnum,
    amount: Decimal,
    incurred_on: date,
    kind: ClaimKindEnum = ClaimKindEnum.RECEIPTED,
    currency: str = "INR",
    settlement_date: date | None = None,
    has_receipt: bool = False,
    justification: str | None = None,
    description: str | None = None,
    travel_request_id: uuid.UUID | None = None,
    receipt_document_id: uuid.UUID | None = None,
    distance_km: Decimal | None = None,
    route_km: Decimal | None = None,
    vehicle_type: str | None = None,
    per_diem_days: int | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """Capture a claim and tell the employee the verdict in the same breath.

    This is the whole point of the slice. The alternative — store it now,
    evaluate at approval — is what produces the employee who finds out three
    weeks later, and it is the reason expense policy in most products is a
    document rather than a control.

    A blocked claim is still stored, as a draft that cannot be submitted. Not
    storing it would throw away the strongest signal finance has about where a
    limit is wrong.
    """
    emp_stmt = tenant_scoped(
        select(Employee).where(Employee.id == employee_id), Employee, user
    )
    employee = (await session.execute(emp_stmt)).scalar_one_or_none()
    if employee is None:
        raise _not_found("No such employee in this tenant.")

    trip: TravelRequest | None = None
    if travel_request_id is not None:
        trip_stmt = tenant_scoped(
            select(TravelRequest).where(TravelRequest.id == travel_request_id),
            TravelRequest,
            user,
        )
        trip = (await session.execute(trip_stmt)).scalar_one_or_none()
        if trip is None:
            raise _not_found("No such travel request in this tenant.")
        if trip.employee_id != employee_id:
            raise _bad_request(
                "That trip belongs to a different employee. A claim on somebody "
                "else's trip would settle their advance."
            )

    reported, converted = await _convert_if_foreign(
        session,
        user,
        amount=_d(amount),
        currency=currency,
        incurred_on=incurred_on,
        settlement_date=settlement_date,
    )

    mileage: rate_engine.MileageClaim | None = None
    if kind is ClaimKindEnum.MILEAGE:
        if distance_km is None or vehicle_type is None:
            raise _bad_request(
                "A mileage claim needs a distance and a vehicle type — without "
                "them there is nothing to value and nothing to check."
            )
        rate_rows = list(
            (
                await session.execute(
                    tenant_scoped(select(MileageRate), MileageRate, user)
                )
            ).scalars()
        )
        try:
            rate_row = rate_engine.rate_on(
                rate_rows, incurred_on, vehicle_type=vehicle_type
            )
            mileage = rate_engine.mileage_claim(
                distance_km=_d(distance_km),
                rate=rate_engine.MileageRate(
                    vehicle_type=rate_row.vehicle_type,
                    rate_per_km=_d(rate_row.rate_per_km),
                    effective_from=rate_row.effective_from,
                    tolerance_percent=_d(rate_row.tolerance_percent),
                ),
                route_km=_d(route_km) if route_km is not None else None,
            )
        except rate_engine.RateError as exc:
            raise _bad_request(str(exc)) from exc
        reported = mileage.amount

    if kind is ClaimKindEnum.PER_DIEM:
        if per_diem_days is None or per_diem_days < 1:
            raise _bad_request("A per-diem claim needs at least one day.")
        pd_rows = list(
            (
                await session.execute(
                    tenant_scoped(select(PerDiemRate), PerDiemRate, user)
                )
            ).scalars()
        )
        try:
            pd_row = rate_engine.rate_on(
                pd_rows,
                incurred_on,
                city_tier=city_tier,
                grade=employee.grade or "",
            )
            pd_claim = rate_engine.per_diem_claim(
                days=per_diem_days,
                rate=rate_engine.PerDiemRate(
                    city_tier=policy_engine.CityTier(pd_row.city_tier.value),
                    grade=pd_row.grade,
                    amount=_d(pd_row.amount),
                    effective_from=pd_row.effective_from,
                    includes_meals=pd_row.includes_meals,
                    includes_accommodation=pd_row.includes_accommodation,
                ),
            )
        except rate_engine.RateError as exc:
            raise _bad_request(str(exc)) from exc
        reported = pd_claim.amount
        #: Never demanded, never recorded as missing. A per-diem has no receipt
        #: because there is nothing to receipt.
        has_receipt = True

    _, rule_rows = await _applicable_rules(
        session, user, employee=employee, as_of=incurred_on
    )
    engine_rules = [_to_engine_rule(r) for r in rule_rows]

    claim_facts = policy_engine.ClaimFacts(
        category_key=category_key,
        city_tier=policy_engine.CityTier(city_tier.value),
        amount=reported,
        incurred_on=incurred_on,
        has_receipt=has_receipt,
        justification=justification or "",
        trip_id=str(travel_request_id) if travel_request_id else None,
        pre_approved=bool(trip and trip.status is TravelRequestStatusEnum.APPROVED),
    )
    priors = await _prior_claims(
        session, user, employee_id=employee_id, category_key=category_key
    )
    verdict = policy_engine.evaluate(
        claim_facts, rules=engine_rules, priors=priors
    )

    matched_rule_id: uuid.UUID | None = None
    if verdict.rule is not None:
        for row in rule_rows:
            if (
                row.category_key == verdict.rule.category_key
                and row.city_tier.value == verdict.rule.city_tier.value
            ):
                matched_rule_id = row.id
                break

    claim = ExpenseClaim(
        tenant_id=user.tenant_id,
        employee_id=employee_id,
        department_id=employee.department_id,
        travel_request_id=travel_request_id,
        category_key=category_key,
        city_tier=city_tier,
        kind=kind,
        amount=reported,
        original_amount=converted.original_amount if converted else reported,
        original_currency=(
            converted.original_currency if converted else currency.upper()
        ),
        exchange_rate=converted.rate if converted else None,
        exchange_rate_date=converted.rate_date if converted else None,
        conversion_convention=(
            ConversionConventionEnum(converted.convention.value) if converted else None
        ),
        exchange_rate_source=converted.rate_source if converted else None,
        incurred_on=incurred_on,
        description=description,
        has_receipt=has_receipt,
        receipt_document_id=receipt_document_id,
        distance_km=_d(distance_km) if distance_km is not None else None,
        route_km=_d(route_km) if route_km is not None else None,
        vehicle_type=vehicle_type,
        per_diem_days=per_diem_days,
        status=ExpenseClaimStatusEnum.DRAFT,
        policy_verdict=PolicyVerdictEnum(verdict.kind.value),
        verdict_message=verdict.message,
        applied_rule_id=matched_rule_id,
        limit_amount=verdict.limit_amount,
        already_claimed=verdict.already_claimed,
        overage=verdict.overage,
        justification=justification,
        flagged_for_approver=verdict.kind is policy_engine.VerdictKind.FLAGGED,
        exceeds_route_tolerance=bool(mileage and mileage.exceeds_tolerance),
        evaluated_at=datetime.now(UTC),
    )
    session.add(claim)
    await session.flush()

    if verdict.is_violation:
        session.add(
            ExpensePolicyViolation(
                tenant_id=user.tenant_id,
                claim_id=claim.id,
                employee_id=employee_id,
                department_id=employee.department_id,
                category_key=category_key,
                city_tier=city_tier,
                verdict=PolicyVerdictEnum(verdict.kind.value),
                violation_action=(
                    ViolationActionEnum(verdict.rule.violation_action.value)
                    if verdict.rule
                    else ViolationActionEnum.FLAG
                ),
                limit_amount=verdict.limit_amount,
                claim_amount=reported,
                overage=verdict.overage,
                incurred_on=incurred_on,
            )
        )

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="expense_claim",
        entity_id=claim.id,
        actor=user,
        summary=(
            f"Claim captured: {category_key} {reported} — "
            f"{verdict.kind.value}."
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(claim)

    return {
        "claim_id": str(claim.id),
        "status": claim.status.value,
        "amount": str(claim.amount),
        "original_amount": str(claim.original_amount),
        "original_currency": claim.original_currency,
        "verdict": verdict.as_dict(),
        "mileage": mileage.as_dict() if mileage else None,
        "conversion": converted.as_dict() if converted else None,
        # Said here, at capture, and not left to the submit call to discover.
        "may_submit": verdict.permits_submission,
    }


async def submit_claim(
    session: AsyncSession,
    user: User,
    *,
    claim_id: uuid.UUID,
    justification: str | None = None,
    request: Request | None = None,
) -> ExpenseClaim:
    """Submit a captured claim, honouring the verdict it was given.

    The stored verdict decides. Re-evaluating here against today's policy would
    let a limit tightened overnight turn a claim the employee was told was fine
    into one they cannot submit and cannot understand.
    """
    stmt = tenant_scoped(
        select(ExpenseClaim).where(ExpenseClaim.id == claim_id), ExpenseClaim, user
    )
    claim = (await session.execute(stmt)).scalar_one_or_none()
    if claim is None:
        raise _not_found("No such claim in this tenant.")
    permissions.require_self_or_hr(
        user, claim.employee_id, detail="You can only submit your own expense claims."
    )
    if claim.status is not ExpenseClaimStatusEnum.DRAFT:
        raise _bad_request(
            f"This claim is already {claim.status.value}. Submitting it again "
            "would create a second approval for one expense."
        )

    if justification and not claim.justification:
        claim.justification = justification

    verdict = policy_engine.VerdictKind(claim.policy_verdict.value)
    if verdict is policy_engine.VerdictKind.NEEDS_JUSTIFICATION:
        # The reason is what unblocks it, and it can arrive here rather than at
        # capture — an employee who is told at capture and writes the reason on
        # the next screen has done exactly what the policy asked.
        if not (claim.justification or "").strip():
            raise _bad_request(
                "This claim is over the limit and the policy requires a written "
                f"reason. {claim.verdict_message}"
            )
        # The breach does not stop being a breach because it was explained. The
        # approver sees the flag and the reason together, which is the whole
        # point of REQUIRE_JUSTIFICATION rather than BLOCK.
        claim.flagged_for_approver = True
    elif not verdict.permits_submission:
        raise _bad_request(
            f"This claim cannot be submitted. {claim.verdict_message}"
        )

    claim.status = ExpenseClaimStatusEnum.SUBMITTED
    claim.submitted_at = datetime.now(UTC)

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="expense_claim",
        entity_id=claim.id,
        actor=user,
        summary=(
            f"Claim submitted ({claim.policy_verdict.value})"
            + (" — flagged for the approver." if claim.flagged_for_approver else ".")
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(claim)
    return claim


async def decide_claim(
    session: AsyncSession,
    user: User,
    *,
    claim_id: uuid.UUID,
    approve: bool,
    note: str | None = None,
    request: Request | None = None,
) -> ExpenseClaim:
    stmt = tenant_scoped(
        select(ExpenseClaim).where(ExpenseClaim.id == claim_id), ExpenseClaim, user
    )
    claim = (await session.execute(stmt)).scalar_one_or_none()
    if claim is None:
        raise _not_found("No such claim in this tenant.")
    if claim.status is not ExpenseClaimStatusEnum.SUBMITTED:
        raise _bad_request(
            f"Only a submitted claim can be decided; this one is "
            f"{claim.status.value}."
        )
    if claim.employee_id == getattr(user, "employee_id", None):
        raise _bad_request("A claim cannot be approved by the person who made it.")

    claim.status = (
        ExpenseClaimStatusEnum.APPROVED if approve else ExpenseClaimStatusEnum.REJECTED
    )
    claim.decided_by_user_id = user.id
    claim.decided_at = datetime.now(UTC)
    claim.decision_note = note

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.APPROVE if approve else AuditAction.REJECT,
        entity_type="expense_claim",
        entity_id=claim.id,
        actor=user,
        summary=f"Claim {'approved' if approve else 'rejected'}: {note or ''}".strip(),
        request=request,
    )
    await session.commit()
    await session.refresh(claim)
    return claim


# ══════════════════════════════════════════════════════════════════════════════
# Violation rates
# ══════════════════════════════════════════════════════════════════════════════


async def violation_rates(
    session: AsyncSession,
    user: User,
    *,
    scope: str = "department",
) -> dict[str, Any]:
    """Breach rates by employee, department or category, with a reading.

    A department at 60% is not sixty per cent dishonest — it is a department
    whose limits do not match what its work costs. One person at 60% inside a
    department at 5% is a different conversation. The rate alone does not
    distinguish them, so the peer rate is passed in and the interpretation says
    which reading it supports.
    """
    if scope not in {"employee", "department", "category"}:
        raise _bad_request(
            "Scope must be employee, department or category — those are the "
            "three groupings that separate a policy problem from a behaviour "
            "one."
        )

    column = {
        "employee": ExpenseClaim.employee_id,
        "department": ExpenseClaim.department_id,
        "category": ExpenseClaim.category_key,
    }[scope]

    total_stmt = tenant_scoped(
        select(column, func.count()).group_by(column), ExpenseClaim, user
    )
    totals = {str(k): int(v) for k, v in (await session.execute(total_stmt)).all()}

    viol_column = {
        "employee": ExpensePolicyViolation.employee_id,
        "department": ExpensePolicyViolation.department_id,
        "category": ExpensePolicyViolation.category_key,
    }[scope]
    viol_stmt = tenant_scoped(
        select(viol_column, func.count()).group_by(viol_column),
        ExpensePolicyViolation,
        user,
    )
    violations = {str(k): int(v) for k, v in (await session.execute(viol_stmt)).all()}

    rows = [
        policy_engine.ViolationRate(
            scope=scope,
            key=key,
            total_claims=total,
            violations=violations.get(key, 0),
        )
        for key, total in sorted(totals.items())
    ]

    overall_total = sum(r.total_claims for r in rows)
    overall_violations = sum(r.violations for r in rows)
    peer_rate = (
        overall_violations / overall_total if overall_total >= 10 else None
    )

    return {
        "scope": scope,
        "peer_rate": round(peer_rate, 3) if peer_rate is not None else None,
        "groups": [r.as_dict(peer_rate=peer_rate) for r in rows],
        "note": (
            "A high rate across a whole group usually means the limit is wrong. "
            "A high rate for one person inside a compliant group is a different "
            "conversation. The peer rate is what tells them apart."
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Travel and advances
# ══════════════════════════════════════════════════════════════════════════════


async def create_travel_request(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    purpose: str,
    from_date: date,
    to_date: date,
    destinations: list[dict[str, Any]] | None = None,
    estimated_cost: Decimal = Decimal("0"),
    advance_requested: Decimal = Decimal("0"),
    request: Request | None = None,
) -> TravelRequest:
    if to_date < from_date:
        raise _bad_request("A trip cannot end before it starts.")
    row = TravelRequest(
        tenant_id=user.tenant_id,
        employee_id=employee_id,
        purpose=purpose,
        from_date=from_date,
        to_date=to_date,
        destinations=list(destinations or []),
        estimated_cost=_d(estimated_cost),
        advance_requested=_d(advance_requested),
        status=TravelRequestStatusEnum.SUBMITTED,
        approvals=[],
    )
    session.add(row)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="travel_request",
        entity_id=row.id,
        actor=user,
        summary=(
            f"Travel request {from_date.isoformat()} to {to_date.isoformat()}, "
            f"advance requested {advance_requested}."
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def decide_travel_request(
    session: AsyncSession,
    user: User,
    *,
    travel_request_id: uuid.UUID,
    approve: bool,
    note: str | None = None,
    request: Request | None = None,
) -> TravelRequest:
    stmt = tenant_scoped(
        select(TravelRequest).where(TravelRequest.id == travel_request_id),
        TravelRequest,
        user,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise _not_found("No such travel request in this tenant.")
    if row.status is not TravelRequestStatusEnum.SUBMITTED:
        raise _bad_request(
            f"Only a submitted travel request can be decided; this one is "
            f"{row.status.value}."
        )

    row.status = (
        TravelRequestStatusEnum.APPROVED if approve else TravelRequestStatusEnum.REJECTED
    )
    # Appended, never rewritten: the approval trail is evidence for a
    # disbursement and a rewritten one is not evidence.
    row.approvals = [
        *list(row.approvals or []),
        {
            "user_id": str(user.id),
            "decision": "approved" if approve else "rejected",
            "at": datetime.now(UTC).isoformat(),
            "note": note or "",
        },
    ]
    row.decided_by_user_id = user.id
    row.decided_at = datetime.now(UTC)

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.APPROVE if approve else AuditAction.REJECT,
        entity_type="travel_request",
        entity_id=row.id,
        actor=user,
        summary=f"Travel request {row.status.value}: {note or ''}".strip(),
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def _advance_facts(
    session: AsyncSession, user: User, *, employee_id: uuid.UUID
) -> list[advance_engine.AdvanceFacts]:
    stmt = tenant_scoped(
        select(TravelAdvance)
        .where(TravelAdvance.employee_id == employee_id)
        .where(TravelAdvance.status != AdvanceStatusEnum.CANCELLED),
        TravelAdvance,
        user,
    )
    return [
        advance_engine.AdvanceFacts(
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
        for row in (await session.execute(stmt)).scalars()
    ]


async def issue_advance(
    session: AsyncSession,
    user: User,
    *,
    travel_request_id: uuid.UUID,
    amount: Decimal,
    issued_on: date,
    disbursement_batch_id: uuid.UUID | None = None,
    request: Request | None = None,
) -> TravelAdvance:
    """Issue an advance against an approved trip, unless an old one is unsettled.

    The block is the control finance actually asks for. Somebody takes ₹40,000
    in March, never claims, and in June asks for another; issuing it puts the
    company ₹80,000 out to one person with no expenses against either amount.
    """
    stmt = tenant_scoped(
        select(TravelRequest).where(TravelRequest.id == travel_request_id),
        TravelRequest,
        user,
    )
    trip = (await session.execute(stmt)).scalar_one_or_none()
    if trip is None:
        raise _not_found("No such travel request in this tenant.")
    if trip.status is not TravelRequestStatusEnum.APPROVED:
        raise _bad_request(
            "An advance goes out only against an approved trip. Issuing one "
            f"against a {trip.status.value} request is money out with nothing "
            "authorising it."
        )

    settings = await _settings(session, user)
    facts = await _advance_facts(session, user, employee_id=trip.employee_id)
    decision = advance_engine.advance_block(
        facts,
        today=issued_on,
        block_after_days=settings.advance_block_after_days,
    )
    if not decision.permitted:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=decision.message
        )

    row = TravelAdvance(
        tenant_id=user.tenant_id,
        travel_request_id=travel_request_id,
        employee_id=trip.employee_id,
        amount=_d(amount),
        issued_on=issued_on,
        disbursement_batch_id=disbursement_batch_id,
        status=AdvanceStatusEnum.ISSUED,
        issued_by_user_id=user.id,
    )
    session.add(row)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="travel_advance",
        entity_id=row.id,
        actor=user,
        summary=f"Advance {amount} issued for trip {travel_request_id}.",
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def settle_advance(
    session: AsyncSession,
    user: User,
    *,
    advance_id: uuid.UUID,
    settled_on: date,
    request: Request | None = None,
) -> dict[str, Any]:
    """Offset the trip's claims against its advance.

    Claims are read from the advance's own trip. Nothing here can net across
    trips, because the claims that reach the engine are selected by
    `travel_request_id` — an under-spent March trip cannot quietly settle an
    over-spent June one.
    """
    stmt = tenant_scoped(
        select(TravelAdvance).where(TravelAdvance.id == advance_id),
        TravelAdvance,
        user,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise _not_found("No such advance in this tenant.")

    claim_stmt = tenant_scoped(
        select(ExpenseClaim)
        .where(ExpenseClaim.travel_request_id == row.travel_request_id)
        .where(
            ExpenseClaim.status.in_(
                [
                    ExpenseClaimStatusEnum.APPROVED,
                    ExpenseClaimStatusEnum.REIMBURSED,
                ]
            )
        ),
        ExpenseClaim,
        user,
    )
    claims = list((await session.execute(claim_stmt)).scalars())
    claims_total = policy_engine.money(
        sum((_d(c.amount) for c in claims), Decimal("0"))
    )

    facts = advance_engine.AdvanceFacts(
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
    try:
        result = advance_engine.settle(
            facts,
            claims_total=claims_total,
            trip_ids_of_claims=tuple(str(c.travel_request_id) for c in claims),
        )
    except advance_engine.AdvanceError as exc:
        raise _bad_request(str(exc)) from exc

    row.claimed_against = claims_total
    row.settled_on = settled_on
    row.status = AdvanceStatusEnum.SETTLED
    row.recovered_amount = result.recovery_amount
    row.refunded_amount = result.refund_amount

    session.add(
        AdvanceSettlement(
            tenant_id=user.tenant_id,
            advance_id=row.id,
            travel_request_id=row.travel_request_id,
            advance_amount=result.advance_amount,
            claims_total=result.claims_total,
            outcome=SettlementOutcomeEnum(result.outcome.value),
            recovery_amount=result.recovery_amount,
            refund_amount=result.refund_amount,
            # Null until payroll picks it up. Setting it here would record a
            # recovery that has not happened.
            payroll_recovery_ref=None,
            settled_on=settled_on,
            settled_by_user_id=user.id,
            message=result.message,
        )
    )
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="travel_advance",
        entity_id=row.id,
        actor=user,
        summary=f"Advance settled: {result.outcome.value}.",
        request=request,
    )
    await session.commit()

    payload = result.as_dict()
    payload["claim_count"] = len(claims)
    payload["payroll_recovery"] = (
        {
            "required": result.outcome.needs_payroll_recovery,
            "amount": str(result.recovery_amount),
            "reference": None,
            "note": (
                "Recorded for payroll to recover. Nothing in payroll picks this "
                "up automatically yet, so the balance stays visible and "
                "unrecovered rather than being quietly netted."
            ),
        }
        if result.outcome.needs_payroll_recovery
        else None
    )
    return payload


async def advance_ageing(
    session: AsyncSession,
    user: User,
    *,
    as_of: date,
    employee_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    stmt = tenant_scoped(
        select(TravelAdvance).where(TravelAdvance.status != AdvanceStatusEnum.CANCELLED),
        TravelAdvance,
        user,
    )
    if employee_id is not None:
        stmt = stmt.where(TravelAdvance.employee_id == employee_id)
    rows = list((await session.execute(stmt)).scalars())
    settings = await _settings(session, user)

    facts = [
        advance_engine.AdvanceFacts(
            advance_id=str(r.id),
            trip_id=str(r.travel_request_id),
            employee_id=str(r.employee_id),
            amount=_d(r.amount),
            issued_on=r.issued_on,
            claimed_against=_d(r.claimed_against),
            settled_on=r.settled_on,
            recovered=_d(r.recovered_amount),
            refunded=_d(r.refunded_amount),
        )
        for r in rows
    ]
    return advance_engine.ageing_report(
        facts, today=as_of, block_after_days=settings.advance_block_after_days
    )


def expense_posture() -> dict[str, Any]:
    """What this slice governs, and what it does not.

    Written here rather than in a document, because a document is not read
    during a procurement call and this is returned by an endpoint.
    """
    return {
        "enforced": [
            "Policy is evaluated at capture and the verdict is returned in the "
            "same response, not at approval three weeks later.",
            "BLOCK prevents submission; REQUIRE_JUSTIFICATION requires a "
            "written reason; FLAG submits with a flag the approver sees.",
            "Per-day, per-month and per-trip limits aggregate across claims.",
            "A per-diem claim never requires a receipt.",
            "An advance settles only against its own trip's claims.",
            "An advance unsettled past the tenant's threshold blocks a new one.",
            "Foreign claims store both amounts, the rate, its date, its source "
            "and the convention that chose the date.",
        ],
        "not_built": [
            "Nothing in payroll picks up a recovery from an under-spent "
            "advance. The settlement records it and the balance stays visible "
            "and unrecovered.",
            "No advance is placed into a disbursement batch automatically; the "
            "batch id is recorded if one is supplied.",
            "No exchange rate feed is connected. Rates are entered by an admin "
            "and a claim with no quote on or before its date is refused rather "
            "than converted at a guess.",
            "No receipt OCR or category extraction. A claim arrives with its "
            "category and amount already decided by the caller.",
            "No approval chain: a claim has one decision, not a routed "
            "multi-level workflow.",
            "No escalation notification for an ageing advance. The ageing "
            "report exists and nothing emails it.",
        ],
    }
