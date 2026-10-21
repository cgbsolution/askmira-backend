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

**How the money moves**, stated once here:

- A submitted claim goes to the employee's line manager first, then finance.
  Someone with no line manager who can decide it goes straight to finance.
- An approved claim is paid at cost in the next regular payroll run
  (`services/payroll/inputs.py`, `ExpenseClaimProvider`). The run's approval
  marks it REIMBURSED with the run's id, so no claim is paid twice.
- A claim on a trip with an open travel advance waits for the advance to be
  settled. Settlement offsets the trip's claims against the advance: a
  shortfall is recovered in payroll (`payroll_recovery_ref`), a balance owed to
  the employee is paid in payroll (`payroll_refund_ref`).
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
    ExpenseReceipt,
    ExpenseReport,
    ExpenseReportStatusEnum,
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
from app.services.approvals.email_actions import ApprovalRef
from app.services.attendance.service import local_today
from app.services.documents.indian_numbers import group_indian
from app.services.expenses import advances as advance_engine
from app.services.expenses import policy as policy_engine
from app.services.expenses import rates as rate_engine
from app.services.notifications.dispatch import notify, register_event

__all__ = [
    "advance_ageing",
    "capture_claim",
    "create_policy",
    "create_travel_request",
    "decide_claim",
    "decide_claim_as_manager",
    "decide_travel_request",
    "decide_travel_request_as_manager",
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


EVENT_CLAIM_AWAITING_MANAGER = "expenses.claim.awaiting_manager"
EVENT_CLAIM_MANAGER_DECIDED = "expenses.claim.manager_decided"
EVENT_CLAIM_DECIDED = "expenses.claim.decided"
EVENT_TRAVEL_AWAITING_MANAGER = "expenses.travel.awaiting_manager"
EVENT_TRAVEL_DECIDED = "expenses.travel.decided"

register_event(
    EVENT_CLAIM_AWAITING_MANAGER,
    "{employee_name}'s expense claim needs your approval",
    "{employee_name} claimed {amount} for {category} on {incurred_on}. Approve or reject it "
    "under Expenses, Team approvals.",
)
register_event(
    EVENT_CLAIM_MANAGER_DECIDED,
    "Your {category} claim was {decision} by your manager",
    "Your claim for {amount} ({category}, {incurred_on}) was {decision} by {decided_by}."
    "{next_line}{note_line}",
)
register_event(
    EVENT_CLAIM_DECIDED,
    "Your {category} claim was {decision}",
    "Finance {decision} your claim for {amount} ({category}, {incurred_on}).{next_line}{note_line}",
)
register_event(
    EVENT_TRAVEL_AWAITING_MANAGER,
    "{employee_name}'s travel request needs your approval",
    "{employee_name} plans to travel from {from_date} to {to_date}: {purpose}. Approve or "
    "reject it under Expenses, Team approvals.",
)
register_event(
    EVENT_TRAVEL_DECIDED,
    "Your travel request was {decision}",
    "Your trip from {from_date} to {to_date} was {decision} by {decided_by}.{next_line}{note_line}",
)


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _rupees(value: Any) -> str:
    return f"₹{group_indian(_d(value).quantize(Decimal('0.01')))}"


def _day(value: date) -> str:
    return f"{value:%d %b %Y}"


def _category(key: str) -> str:
    return key.replace("_", " ").strip().lower() or "expense"


async def _login_ids(
    session: AsyncSession, user: User, employee_ids: set[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID]:
    """The active login of each employee that has one."""
    wanted = {eid for eid in employee_ids if eid is not None}
    if not wanted:
        return {}
    rows = (
        await session.execute(
            tenant_scoped(
                select(User.employee_id, User.id).where(
                    User.employee_id.in_(wanted), User.is_active.is_(True)
                ),
                User,
                user,
            )
        )
    ).all()
    return {employee_id: user_id for employee_id, user_id in rows}


async def line_manager_login(
    session: AsyncSession, user: User, employee_id: uuid.UUID
) -> tuple[Employee | None, User | None]:
    """The employee, and the login of the line manager who decides their claims.

    `None` for the login when there is nobody who could: no manager on the
    record, or a manager with no active login. Such a request goes straight to
    finance rather than waiting on someone who will never see it.
    """
    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
    )
    if employee is None or employee.manager_id is None or employee.manager_id == employee.id:
        return employee, None
    manager_login = await session.scalar(
        tenant_scoped(
            # They manage this employee, so they can take a manager's decision
            # whatever their login role.
            select(User).where(User.employee_id == employee.manager_id, User.is_active.is_(True)),
            User,
            user,
        )
    )
    return employee, manager_login


def first_stage(has_line_manager: bool) -> tuple[ExpenseClaimStatusEnum, str]:
    """Where a claim goes on submission, and how to say so. Pure."""
    if has_line_manager:
        return ExpenseClaimStatusEnum.PENDING_MANAGER, "sent to the line manager"
    return ExpenseClaimStatusEnum.SUBMITTED, "sent to finance (no line manager to decide it)"


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
    await session.flush()
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
    await session.flush()
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
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.RULE_CHANGE,
        entity_type="exchange_rate_quote",
        entity_id=row.id,
        actor=user,
        summary=(
            f"Exchange rate {from_currency.upper()}/{to_currency.upper()} = {rate} on "
            f"{quote_date.isoformat()} ({source})."
        ),
        request=request,
    )
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

    convention = rate_engine.ConversionConvention(settings.conversion_convention.value)
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
    report_id: uuid.UUID | None = None,
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
    emp_stmt = tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
    employee = (await session.execute(emp_stmt)).scalar_one_or_none()
    if employee is None:
        raise _not_found("No such employee in this tenant.")

    # Judged against the workspace's date, not the server's UTC one: at 01:00
    # IST an expense from "today" would otherwise read as tomorrow's.
    today = await local_today(session, user)
    if incurred_on > today:
        raise _bad_request(
            f"An expense cannot be claimed before it is incurred; {_day(incurred_on)} is "
            "in the future."
        )

    if report_id is not None:
        report = (
            await session.execute(
                tenant_scoped(
                    select(ExpenseReport).where(ExpenseReport.id == report_id),
                    ExpenseReport,
                    user,
                )
            )
        ).scalar_one_or_none()
        if report is None:
            raise _not_found("No such expense report in this tenant.")
        if report.employee_id != employee_id:
            raise _bad_request("That report belongs to a different employee.")
        if report.status is not ExpenseReportStatusEnum.DRAFT:
            raise _bad_request(
                f"That report is already {report.status.value}. Lines are added only "
                "to a draft report."
            )
        if travel_request_id is None:
            travel_request_id = report.travel_request_id

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

    if receipt_document_id is not None:
        receipt_owner = (
            await session.execute(
                tenant_scoped(
                    select(ExpenseReceipt.employee_id).where(
                        ExpenseReceipt.id == receipt_document_id
                    ),
                    ExpenseReceipt,
                    user,
                )
            )
        ).scalar_one_or_none()
        if receipt_owner is None:
            raise _not_found("No such receipt in this tenant.")
        if receipt_owner != employee_id:
            raise _bad_request("That receipt was uploaded for a different employee.")
        attached = (
            await session.execute(
                tenant_scoped(
                    select(ExpenseClaim.id)
                    .where(ExpenseClaim.receipt_document_id == receipt_document_id)
                    .where(ExpenseClaim.status != ExpenseClaimStatusEnum.CANCELLED),
                    ExpenseClaim,
                    user,
                )
            )
        ).first()
        if attached is not None:
            raise _bad_request(
                "That receipt is already attached to another claim. One bill, one claim."
            )
        #: The file is the evidence; nobody's say-so is.
        has_receipt = True

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
            (await session.execute(tenant_scoped(select(MileageRate), MileageRate, user))).scalars()
        )
        try:
            rate_row = rate_engine.rate_on(rate_rows, incurred_on, vehicle_type=vehicle_type)
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
            (await session.execute(tenant_scoped(select(PerDiemRate), PerDiemRate, user))).scalars()
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

    _, rule_rows = await _applicable_rules(session, user, employee=employee, as_of=incurred_on)
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
    priors = await _prior_claims(session, user, employee_id=employee_id, category_key=category_key)
    verdict = policy_engine.evaluate(claim_facts, rules=engine_rules, priors=priors)

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
        report_id=report_id,
        category_key=category_key,
        city_tier=city_tier,
        kind=kind,
        amount=reported,
        original_amount=converted.original_amount if converted else reported,
        original_currency=(converted.original_currency if converted else currency.upper()),
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
        summary=(f"Claim captured: {category_key} {reported} — {verdict.kind.value}."),
        request=request,
    )
    await session.commit()
    await session.refresh(claim)

    return {
        "claim_id": str(claim.id),
        "status": claim.status.value,
        "report_id": str(claim.report_id) if claim.report_id else None,
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
        raise _bad_request(f"This claim cannot be submitted. {claim.verdict_message}")

    from app.models.approval_rules import ApprovalProcess
    from app.services.approvals import rules

    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == claim.employee_id), Employee, user)
    )
    steps, rule_name = (
        await rules.resolve_steps(
            session,
            user,
            employee,
            rules.Facts(
                process=ApprovalProcess.EXPENSE,
                department_id=employee.department_id,
                location_id=employee.location_id,
                amount=Decimal(str(claim.amount)),
                category=claim.category_key,
            ),
        )
        if employee is not None
        else ([], None)
    )
    claim.approval_chain = rules.chain_from(steps, rule_name)
    claim.status, routed = first_stage(bool(steps))
    if steps:
        routed = f"sent to {steps[0].name}" + (
            f" (step 1 of {len(steps)}, then finance)" if len(steps) > 1 else ""
        )
    first_approver = steps[0].user_id if steps else None
    claim.submitted_at = datetime.now(UTC)

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="expense_claim",
        entity_id=claim.id,
        actor=user,
        changes={"status": claim.status.value},
        summary=(
            f"Claim submitted ({claim.policy_verdict.value}) and {routed}"
            + (" — flagged for the approver." if claim.flagged_for_approver else ".")
        ),
        request=request,
    )
    if first_approver is not None:
        await notify(
            session,
            tenant_id=user.tenant_id,
            event_key=EVENT_CLAIM_AWAITING_MANAGER,
            recipient_user_ids=[first_approver],
            payload={
                "employee_name": employee.full_name if employee else "A team member",
                "amount": _rupees(claim.amount),
                "category": _category(claim.category_key),
                "incurred_on": _day(claim.incurred_on),
                "claim_id": str(claim.id),
            },
            commit=False,
            approval=ApprovalRef("expense_claim_manager", claim.id),
        )
    await session.commit()
    await session.refresh(claim)
    return claim


async def _claim_for_decision(
    session: AsyncSession, user: User, claim_id: uuid.UUID
) -> ExpenseClaim:
    stmt = tenant_scoped(
        select(ExpenseClaim).where(ExpenseClaim.id == claim_id), ExpenseClaim, user
    )
    claim = (await session.execute(stmt)).scalar_one_or_none()
    if claim is None:
        raise _not_found("No such claim in this tenant.")
    if claim.employee_id == getattr(user, "employee_id", None):
        raise permissions.forbidden(
            "A claim cannot be decided by the person who made it. It goes to somebody else."
        )
    return claim


async def _open_advance_on_trip(
    session: AsyncSession, user: User, trip_id: uuid.UUID | None
) -> bool:
    if trip_id is None:
        return False
    return (
        await session.scalar(
            tenant_scoped(
                select(TravelAdvance.id).where(
                    TravelAdvance.travel_request_id == trip_id,
                    TravelAdvance.status == AdvanceStatusEnum.ISSUED,
                ),
                TravelAdvance,
                user,
            )
        )
    ) is not None


async def decide_claim_as_manager(
    session: AsyncSession,
    user: User,
    *,
    claim_id: uuid.UUID,
    approve: bool,
    note: str | None = None,
    request: Request | None = None,
) -> ExpenseClaim:
    """The line manager's stage. An approval hands the claim to finance.

    The line manager decides — or HR, standing in for a manager who is away.
    Nobody decides their own claim.
    """
    from app.services.approvals import rules
    from app.services.leave import engine as chain_engine

    claim = await _claim_for_decision(session, user, claim_id)
    if claim.status is not ExpenseClaimStatusEnum.PENDING_MANAGER:
        raise _bad_request(
            f"This claim is not waiting for a manager's decision; it is "
            f"{claim.status.value.replace('_', ' ')}."
        )
    chain = list(claim.approval_chain or [])
    outcome = "approved" if approve else "rejected"
    now = datetime.now(UTC)
    if chain:
        index = chain_engine.current_step(chain)
        step = chain[index] if index is not None else {}
        acting = {str(a) for a in await rules.acting_for(session, user)}
        if not (permissions.is_hr(user) or step.get("approver_user_id") in acting):
            raise permissions.forbidden(
                f"This claim is waiting on {step.get('approver_name', 'another approver')}. "
                "Only they, somebody standing in for them, or HR can decide it."
            )
        chain, outcome = chain_engine.apply_decision(
            chain,
            approve=approve,
            actor_user_id=user.id,
            actor_name=user.full_name,
            note=note,
            at=now,
        )
        claim.approval_chain = chain
    else:
        await permissions.require_manager_or_hr(
            session,
            user,
            claim.employee_id,
            detail="Only the claimant's line manager, or HR, decides this claim.",
        )

    claim.manager_decided_by_user_id = user.id
    claim.manager_decided_at = now
    claim.manager_decision_note = note
    if outcome == "advanced":
        pass  # still with the approvers; the next one is told below
    elif approve:
        claim.status = ExpenseClaimStatusEnum.SUBMITTED
    else:
        # A manager's rejection is the claim's final decision.
        claim.status = ExpenseClaimStatusEnum.REJECTED
        claim.decided_by_user_id = user.id
        claim.decided_at = now
        claim.decision_note = note
    if claim.report_id is not None:
        await _refresh_report_status(session, user, claim.report_id)

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.APPROVE if approve else AuditAction.REJECT,
        entity_type="expense_claim",
        entity_id=claim.id,
        actor=user,
        changes={
            "stage": "manager",
            "status": claim.status.value,
            "note": note,
            "outcome": outcome,
        },
        summary=(
            f"Approver {'approved' if approve else 'rejected'} the claim"
            + (
                " and passed it to the next approver"
                if outcome == "advanced"
                else " and sent it to finance"
                if approve
                else ""
            )
            + (f": {note}" if note else ".")
        ),
        request=request,
    )
    if outcome == "advanced":
        index = chain_engine.current_step(chain)
        next_approver = chain[index].get("approver_user_id") if index is not None else None
        if next_approver:
            employee = await session.scalar(
                tenant_scoped(
                    select(Employee).where(Employee.id == claim.employee_id), Employee, user
                )
            )
            await notify(
                session,
                tenant_id=user.tenant_id,
                event_key=EVENT_CLAIM_AWAITING_MANAGER,
                recipient_user_ids=[uuid.UUID(next_approver)],
                payload={
                    "employee_name": employee.full_name if employee else "A team member",
                    "amount": _rupees(claim.amount),
                    "category": _category(claim.category_key),
                    "incurred_on": _day(claim.incurred_on),
                    "claim_id": str(claim.id),
                },
                commit=False,
                approval=ApprovalRef("expense_claim_manager", claim.id),
            )
        await session.commit()
        await session.refresh(claim)
        return claim
    recipients = await _login_ids(session, user, {claim.employee_id})
    if claim.employee_id in recipients:
        await notify(
            session,
            tenant_id=user.tenant_id,
            event_key=EVENT_CLAIM_MANAGER_DECIDED,
            recipient_user_ids=[recipients[claim.employee_id]],
            payload={
                "decision": "approved" if approve else "rejected",
                "amount": _rupees(claim.amount),
                "category": _category(claim.category_key),
                "incurred_on": _day(claim.incurred_on),
                "decided_by": user.full_name,
                "next_line": " It is now with finance." if approve else "",
                "note_line": f" Note: {note}" if note else "",
                "claim_id": str(claim.id),
            },
            commit=False,
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
    """Finance's decision, the last stage. An approval is paid in the next payroll."""
    claim = await _claim_for_decision(session, user, claim_id)
    if claim.status is ExpenseClaimStatusEnum.PENDING_MANAGER:
        raise _bad_request(
            "This claim is waiting for the employee's line manager. It reaches finance once "
            "they approve it."
        )
    if claim.status is not ExpenseClaimStatusEnum.SUBMITTED:
        raise _bad_request(
            f"Only a claim with finance can be decided here; this one is {claim.status.value}."
        )

    claim.status = ExpenseClaimStatusEnum.APPROVED if approve else ExpenseClaimStatusEnum.REJECTED
    claim.decided_by_user_id = user.id
    claim.decided_at = datetime.now(UTC)
    claim.decision_note = note
    if claim.report_id is not None:
        await _refresh_report_status(session, user, claim.report_id)

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.APPROVE if approve else AuditAction.REJECT,
        entity_type="expense_claim",
        entity_id=claim.id,
        actor=user,
        changes={"stage": "finance", "status": claim.status.value, "note": note},
        summary=f"Claim {'approved' if approve else 'rejected'}: {note or ''}".strip(),
        request=request,
    )
    next_line = ""
    if approve:
        next_line = (
            " It is settled against your travel advance for that trip."
            if await _open_advance_on_trip(session, user, claim.travel_request_id)
            else " It will be paid in your next payroll."
        )
    recipients = await _login_ids(session, user, {claim.employee_id})
    if claim.employee_id in recipients:
        await notify(
            session,
            tenant_id=user.tenant_id,
            event_key=EVENT_CLAIM_DECIDED,
            recipient_user_ids=[recipients[claim.employee_id]],
            payload={
                "decision": "approved" if approve else "rejected",
                "amount": _rupees(claim.amount),
                "category": _category(claim.category_key),
                "incurred_on": _day(claim.incurred_on),
                "next_line": next_line,
                "note_line": f" Note: {note}" if note else "",
                "claim_id": str(claim.id),
            },
            commit=False,
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

    total_stmt = tenant_scoped(select(column, func.count()).group_by(column), ExpenseClaim, user)
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
    peer_rate = overall_violations / overall_total if overall_total >= 10 else None

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
    employee, manager_login = await line_manager_login(session, user, employee_id)
    stage = (
        TravelRequestStatusEnum.PENDING_MANAGER
        if manager_login is not None
        else TravelRequestStatusEnum.SUBMITTED
    )
    row = TravelRequest(
        tenant_id=user.tenant_id,
        employee_id=employee_id,
        purpose=purpose,
        from_date=from_date,
        to_date=to_date,
        destinations=list(destinations or []),
        estimated_cost=_d(estimated_cost),
        advance_requested=_d(advance_requested),
        status=stage,
        approvals=[],
    )
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="travel_request",
        entity_id=row.id,
        actor=user,
        changes={"status": stage.value},
        summary=(
            f"Travel request {from_date.isoformat()} to {to_date.isoformat()}, "
            f"advance requested {advance_requested}; sent to "
            + ("the line manager." if manager_login is not None else "finance.")
        ),
        request=request,
    )
    if manager_login is not None:
        await notify(
            session,
            tenant_id=user.tenant_id,
            event_key=EVENT_TRAVEL_AWAITING_MANAGER,
            recipient_user_ids=[manager_login.id],
            payload={
                "employee_name": employee.full_name if employee else "A team member",
                "from_date": _day(from_date),
                "to_date": _day(to_date),
                "purpose": purpose[:200],
                "travel_request_id": str(row.id),
            },
            commit=False,
            approval=ApprovalRef("travel_request_manager", row.id),
        )
    await session.commit()
    await session.refresh(row)
    return row


async def _trip_for_decision(
    session: AsyncSession, user: User, travel_request_id: uuid.UUID
) -> TravelRequest:
    stmt = tenant_scoped(
        select(TravelRequest).where(TravelRequest.id == travel_request_id),
        TravelRequest,
        user,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise _not_found("No such travel request in this tenant.")
    if row.employee_id == getattr(user, "employee_id", None):
        raise permissions.forbidden(
            "You cannot decide your own travel request. It goes to somebody else."
        )
    return row


async def _record_trip_decision(
    session: AsyncSession,
    user: User,
    row: TravelRequest,
    *,
    stage: str,
    approve: bool,
    note: str | None,
    request: Request | None,
) -> None:
    now = datetime.now(UTC)
    # Appended, never rewritten: the approval trail is evidence for a
    # disbursement and a rewritten one is not evidence.
    row.approvals = [
        *list(row.approvals or []),
        {
            "user_id": str(user.id),
            "name": user.full_name,
            "stage": stage,
            "decision": "approved" if approve else "rejected",
            "at": now.isoformat(),
            "note": note or "",
        },
    ]
    final = stage == "finance" or not approve
    if final:
        row.decided_by_user_id = user.id
        row.decided_at = now

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.APPROVE if approve else AuditAction.REJECT,
        entity_type="travel_request",
        entity_id=row.id,
        actor=user,
        changes={"stage": stage, "status": row.status.value, "note": note},
        summary=(
            f"Travel request {'approved' if approve else 'rejected'} by "
            f"{'the line manager' if stage == 'manager' else 'finance'}"
            + (f": {note}" if note else ".")
        ),
        request=request,
    )
    recipients = await _login_ids(session, user, {row.employee_id})
    if row.employee_id in recipients:
        next_line = ""
        if approve and stage == "manager":
            next_line = " It is now with finance."
        elif approve and _d(row.advance_requested) > 0:
            next_line = " Finance will issue the advance you asked for."
        await notify(
            session,
            tenant_id=user.tenant_id,
            event_key=EVENT_TRAVEL_DECIDED,
            recipient_user_ids=[recipients[row.employee_id]],
            payload={
                "decision": "approved" if approve else "rejected",
                "from_date": _day(row.from_date),
                "to_date": _day(row.to_date),
                "decided_by": (
                    f"your manager, {user.full_name}" if stage == "manager" else "finance"
                ),
                "next_line": next_line,
                "note_line": f" Note: {note}" if note else "",
                "travel_request_id": str(row.id),
            },
            commit=False,
        )


async def decide_travel_request_as_manager(
    session: AsyncSession,
    user: User,
    *,
    travel_request_id: uuid.UUID,
    approve: bool,
    note: str | None = None,
    request: Request | None = None,
) -> TravelRequest:
    """The line manager's stage. An approval hands the trip to finance."""
    row = await _trip_for_decision(session, user, travel_request_id)
    if row.status is not TravelRequestStatusEnum.PENDING_MANAGER:
        raise _bad_request(
            f"This travel request is not waiting for a manager's decision; it is "
            f"{row.status.value.replace('_', ' ')}."
        )
    await permissions.require_manager_or_hr(
        session,
        user,
        row.employee_id,
        detail="Only the traveller's line manager, or HR, decides this request.",
    )
    row.status = TravelRequestStatusEnum.SUBMITTED if approve else TravelRequestStatusEnum.REJECTED
    await _record_trip_decision(
        session, user, row, stage="manager", approve=approve, note=note, request=request
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
    """Finance's decision, the last stage."""
    row = await _trip_for_decision(session, user, travel_request_id)
    if row.status is TravelRequestStatusEnum.PENDING_MANAGER:
        raise _bad_request(
            "This trip is waiting for the traveller's line manager. It reaches finance once "
            "they approve it."
        )
    if row.status is not TravelRequestStatusEnum.SUBMITTED:
        raise _bad_request(
            f"Only a trip with finance can be decided here; this one is {row.status.value}."
        )

    row.status = TravelRequestStatusEnum.APPROVED if approve else TravelRequestStatusEnum.REJECTED
    await _record_trip_decision(
        session, user, row, stage="finance", approve=approve, note=note, request=request
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

    if trip.employee_id == getattr(user, "employee_id", None):
        raise permissions.forbidden(
            "You cannot issue an advance to yourself. It goes to somebody else in finance."
        )

    settings = await _settings(session, user)
    facts = await _advance_facts(session, user, employee_id=trip.employee_id)
    decision = advance_engine.advance_block(
        facts,
        today=issued_on,
        block_after_days=settings.advance_block_after_days,
    )
    if not decision.permitted:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=decision.message)

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
    await session.flush()
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
    if row.employee_id == getattr(user, "employee_id", None):
        raise permissions.forbidden(
            "You cannot settle your own advance. It goes to somebody else in finance."
        )

    # Approved claims on this trip that nothing has paid yet. One already paid
    # in a payroll run, or already offset against another advance, has reached
    # the employee once; counting it here would pay it again.
    claim_stmt = tenant_scoped(
        select(ExpenseClaim)
        .where(ExpenseClaim.travel_request_id == row.travel_request_id)
        .where(ExpenseClaim.status == ExpenseClaimStatusEnum.APPROVED)
        .where(ExpenseClaim.reimbursed_in_run_id.is_(None))
        .where(ExpenseClaim.advance_settlement_id.is_(None)),
        ExpenseClaim,
        user,
    )
    claims = list((await session.execute(claim_stmt)).scalars())
    claims_total = policy_engine.money(sum((_d(c.amount) for c in claims), Decimal("0")))

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

    if settled_on > await local_today(session, user):
        raise _bad_request("An advance cannot be settled on a date in the future.")

    row.claimed_against = claims_total
    row.settled_on = settled_on
    row.status = AdvanceStatusEnum.SETTLED
    row.recovered_amount = result.recovery_amount
    row.refunded_amount = result.refund_amount

    settlement = AdvanceSettlement(
        tenant_id=user.tenant_id,
        advance_id=row.id,
        travel_request_id=row.travel_request_id,
        advance_amount=result.advance_amount,
        claims_total=result.claims_total,
        outcome=SettlementOutcomeEnum(result.outcome.value),
        recovery_amount=result.recovery_amount,
        refund_amount=result.refund_amount,
        # Null until a payroll run's approval takes the recovery or pays the
        # balance. Setting either here would record money that has not moved.
        payroll_recovery_ref=None,
        payroll_refund_ref=None,
        settled_on=settled_on,
        settled_by_user_id=user.id,
        message=result.message,
    )
    session.add(settlement)
    await session.flush()

    # The claims this advance covered are now accounted for by it. Where the
    # advance covered them in full they are reimbursed — out of the advance.
    # Where the trip cost more, they are reimbursed when payroll pays the
    # balance (`AdvanceRefundProvider` marks them then).
    paid_by_advance = result.outcome is not advance_engine.SettlementOutcome.REFUND_DUE_TO_EMPLOYEE
    now = datetime.now(UTC)
    for claim in claims:
        claim.advance_settlement_id = settlement.id
        if paid_by_advance:
            claim.status = ExpenseClaimStatusEnum.REIMBURSED
            claim.reimbursed_at = now
    for report_id in {claim.report_id for claim in claims if claim.report_id}:
        await _refresh_report_status(session, user, report_id)

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="travel_advance",
        entity_id=row.id,
        actor=user,
        changes={
            "outcome": result.outcome.value,
            "claims": [str(claim.id) for claim in claims],
            "recovery_amount": str(result.recovery_amount),
            "refund_amount": str(result.refund_amount),
        },
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
                "Recovered in the next payroll run, when it fits inside the employee's "
                "net pay. Until then the balance stays visible as unrecovered."
            ),
        }
        if result.outcome.needs_payroll_recovery
        else None
    )
    payload["payroll_refund"] = (
        {
            "amount": str(result.refund_amount),
            "reference": None,
            "note": "Paid to the employee in the next payroll run, as a non-taxable line.",
        }
        if result.outcome is advance_engine.SettlementOutcome.REFUND_DUE_TO_EMPLOYEE
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


# ══════════════════════════════════════════════════════════════════════════════
# Reports (claims with line items), withdrawals and policy retirement
# ══════════════════════════════════════════════════════════════════════════════


async def settings_row(session: AsyncSession, user: User) -> TenantExpenseSettings:
    """The tenant's expense settings, created with defaults on first read."""
    return await _settings(session, user)


def derive_report_status(
    statuses: list[ExpenseClaimStatusEnum],
) -> ExpenseReportStatusEnum | None:
    """A submitted report's status from its lines. Pure. `None` means no lines."""
    live = [s for s in statuses if s is not ExpenseClaimStatusEnum.CANCELLED]
    if not statuses:
        return None
    if not live:
        return ExpenseReportStatusEnum.CANCELLED
    if any(s is ExpenseClaimStatusEnum.DRAFT for s in live):
        return ExpenseReportStatusEnum.DRAFT
    pending = {ExpenseClaimStatusEnum.PENDING_MANAGER, ExpenseClaimStatusEnum.SUBMITTED}
    if any(s in pending for s in live):
        return ExpenseReportStatusEnum.SUBMITTED
    paid = {ExpenseClaimStatusEnum.APPROVED, ExpenseClaimStatusEnum.REIMBURSED}
    if all(s in paid for s in live):
        return ExpenseReportStatusEnum.APPROVED
    if all(s is ExpenseClaimStatusEnum.REJECTED for s in live):
        return ExpenseReportStatusEnum.REJECTED
    return ExpenseReportStatusEnum.PARTIALLY_APPROVED


async def _refresh_report_status(session: AsyncSession, user: User, report_id: uuid.UUID) -> None:
    report = (
        await session.execute(
            tenant_scoped(
                select(ExpenseReport).where(ExpenseReport.id == report_id), ExpenseReport, user
            )
        )
    ).scalar_one_or_none()
    if report is None or report.status is ExpenseReportStatusEnum.DRAFT:
        return
    statuses = list(
        (
            await session.execute(
                tenant_scoped(
                    select(ExpenseClaim.status).where(ExpenseClaim.report_id == report_id),
                    ExpenseClaim,
                    user,
                )
            )
        ).scalars()
    )
    derived = derive_report_status(statuses)
    if derived is None or derived is report.status:
        return
    report.status = derived
    if derived in {
        ExpenseReportStatusEnum.APPROVED,
        ExpenseReportStatusEnum.REJECTED,
        ExpenseReportStatusEnum.PARTIALLY_APPROVED,
    }:
        report.decided_at = datetime.now(UTC)


async def create_report(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    title: str,
    travel_request_id: uuid.UUID | None = None,
    request: Request | None = None,
) -> ExpenseReport:
    employee = (
        await session.execute(
            tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
        )
    ).scalar_one_or_none()
    if employee is None:
        raise _not_found("No such employee in this tenant.")
    if travel_request_id is not None:
        trip = (
            await session.execute(
                tenant_scoped(
                    select(TravelRequest).where(TravelRequest.id == travel_request_id),
                    TravelRequest,
                    user,
                )
            )
        ).scalar_one_or_none()
        if trip is None:
            raise _not_found("No such travel request in this tenant.")
        if trip.employee_id != employee_id:
            raise _bad_request("That trip belongs to a different employee.")

    row = ExpenseReport(
        tenant_id=user.tenant_id,
        employee_id=employee_id,
        title=title,
        travel_request_id=travel_request_id,
        status=ExpenseReportStatusEnum.DRAFT,
        created_by_user_id=user.id,
    )
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="expense_report",
        entity_id=row.id,
        actor=user,
        summary=f"Expense report '{title}' started.",
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def submit_report(
    session: AsyncSession,
    user: User,
    *,
    report_id: uuid.UUID,
    justifications: dict[uuid.UUID, str] | None = None,
    request: Request | None = None,
) -> ExpenseReport:
    """Submit every draft line together, each on the verdict it was given at capture.

    All or nothing: a blocked line, or one that needs a reason and has none,
    refuses the whole report and names the line. Letting the rest through would
    split one trip across two approvals and hide the line that mattered.
    """
    report = (
        await session.execute(
            tenant_scoped(
                select(ExpenseReport).where(ExpenseReport.id == report_id), ExpenseReport, user
            )
        )
    ).scalar_one_or_none()
    if report is None:
        raise _not_found("No such expense report in this tenant.")
    permissions.require_self_or_hr(
        user, report.employee_id, detail="You can only submit your own expense reports."
    )
    if report.status is not ExpenseReportStatusEnum.DRAFT:
        raise _bad_request(f"This report is already {report.status.value}.")

    lines = list(
        (
            await session.execute(
                tenant_scoped(
                    select(ExpenseClaim)
                    .where(ExpenseClaim.report_id == report_id)
                    .where(ExpenseClaim.status == ExpenseClaimStatusEnum.DRAFT)
                    .order_by(ExpenseClaim.incurred_on),
                    ExpenseClaim,
                    user,
                )
            )
        ).scalars()
    )
    if not lines:
        raise _bad_request("This report has no draft lines to submit. Add a claim to it first.")

    supplied = {k: (v or "").strip() for k, v in (justifications or {}).items()}
    problems: list[str] = []
    for line in lines:
        verdict = policy_engine.VerdictKind(line.policy_verdict.value)
        reason = supplied.get(line.id) or (line.justification or "").strip()
        label = f"{line.category_key} on {line.incurred_on.isoformat()}"
        if verdict is policy_engine.VerdictKind.NEEDS_JUSTIFICATION:
            if not reason:
                problems.append(f"{label} needs a written reason. {line.verdict_message}")
        elif not verdict.permits_submission:
            problems.append(f"{label} cannot be submitted. {line.verdict_message}")
    if problems:
        raise _bad_request("The report was not submitted. " + " | ".join(problems))

    employee, manager_login = await line_manager_login(session, user, report.employee_id)
    stage, routed = first_stage(manager_login is not None)
    now = datetime.now(UTC)
    for line in lines:
        if supplied.get(line.id) and not (line.justification or "").strip():
            line.justification = supplied[line.id]
        if line.policy_verdict is PolicyVerdictEnum.NEEDS_JUSTIFICATION:
            line.flagged_for_approver = True
        line.status = stage
        line.submitted_at = now
    report.status = ExpenseReportStatusEnum.SUBMITTED
    report.submitted_at = now

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="expense_report",
        entity_id=report.id,
        actor=user,
        changes={"lines": [str(line.id) for line in lines], "status": stage.value},
        summary=f"Expense report submitted with {len(lines)} line(s) and {routed}.",
        request=request,
    )
    if manager_login is not None:
        total = sum((_d(line.amount) for line in lines), Decimal("0"))
        await notify(
            session,
            tenant_id=user.tenant_id,
            event_key=EVENT_CLAIM_AWAITING_MANAGER,
            recipient_user_ids=[manager_login.id],
            payload={
                "employee_name": employee.full_name if employee else "A team member",
                "amount": _rupees(total),
                "category": f"'{report.title}' ({len(lines)} line(s))",
                "incurred_on": _day(min(line.incurred_on for line in lines)),
                "report_id": str(report.id),
            },
            commit=False,
        )
    await session.commit()
    await session.refresh(report)
    return report


async def cancel_claim(
    session: AsyncSession,
    user: User,
    *,
    claim_id: uuid.UUID,
    request: Request | None = None,
) -> ExpenseClaim:
    """Withdraw a draft. Its violation record stays — a rate that improves by
    withdrawing claims is a metric optimising itself."""
    claim = (
        await session.execute(
            tenant_scoped(
                select(ExpenseClaim).where(ExpenseClaim.id == claim_id), ExpenseClaim, user
            )
        )
    ).scalar_one_or_none()
    if claim is None:
        raise _not_found("No such claim in this tenant.")
    permissions.require_self_or_hr(
        user, claim.employee_id, detail="You can only withdraw your own claims."
    )
    if claim.status is not ExpenseClaimStatusEnum.DRAFT:
        raise _bad_request(
            f"Only a draft claim can be withdrawn; this one is {claim.status.value}."
        )
    claim.status = ExpenseClaimStatusEnum.CANCELLED
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="expense_claim",
        entity_id=claim.id,
        actor=user,
        summary="Draft claim withdrawn.",
        request=request,
    )
    await session.commit()
    await session.refresh(claim)
    return claim


async def cancel_travel_request(
    session: AsyncSession,
    user: User,
    *,
    travel_request_id: uuid.UUID,
    request: Request | None = None,
) -> TravelRequest:
    row = (
        await session.execute(
            tenant_scoped(
                select(TravelRequest).where(TravelRequest.id == travel_request_id),
                TravelRequest,
                user,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise _not_found("No such travel request in this tenant.")
    permissions.require_self_or_hr(
        user, row.employee_id, detail="You can only cancel your own travel requests."
    )
    if row.status not in (
        TravelRequestStatusEnum.PENDING_MANAGER,
        TravelRequestStatusEnum.SUBMITTED,
    ):
        raise _bad_request(
            f"Only a trip awaiting a decision can be cancelled; this one is {row.status.value}."
        )
    row.status = TravelRequestStatusEnum.CANCELLED
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="travel_request",
        entity_id=row.id,
        actor=user,
        summary="Travel request cancelled before a decision.",
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def deactivate_policy(
    session: AsyncSession,
    user: User,
    *,
    policy_id: uuid.UUID,
    request: Request | None = None,
) -> ExpensePolicy:
    """Retire a policy. It is kept: claims judged under it still point at its rules."""
    row = (
        await session.execute(
            tenant_scoped(
                select(ExpensePolicy).where(ExpensePolicy.id == policy_id), ExpensePolicy, user
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise _not_found("No such expense policy in this tenant.")
    if not row.is_active:
        raise _bad_request("This policy is already inactive.")
    row.is_active = False
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.RULE_CHANGE,
        entity_type="expense_policy",
        entity_id=row.id,
        actor=user,
        summary=f"Expense policy '{row.name}' v{row.effective_from.isoformat()} retired.",
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


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
            "A submitted claim or trip goes to the employee's line manager, then "
            "to finance. Someone with no line manager who can decide goes "
            "straight to finance. Nobody decides their own.",
            "An approved claim is paid at cost, untaxed, in the next regular "
            "payroll run, and marked reimbursed with that run when the run is "
            "approved. A claim is never paid by two runs.",
            "Claims on a trip with an open advance wait for its settlement: an "
            "under-spent advance is recovered in payroll within net pay, and a "
            "trip that cost more than its advance has the balance paid in payroll.",
        ],
        "not_built": [
            "No advance is placed into a disbursement batch automatically; the "
            "batch id is recorded if one is supplied.",
            "No exchange rate feed is connected. Rates are entered by an admin "
            "and a claim with no quote on or before its date is refused rather "
            "than converted at a guess.",
            "No receipt OCR or category extraction. A claim arrives with its "
            "category and amount already decided by the caller.",
            "Approval is two stages, line manager then finance. There are no "
            "amount-based extra levels or delegation rules.",
            "No escalation notification for an ageing advance. The ageing "
            "report exists and nothing emails it.",
            "An employee who has left is not in a payroll run, so a claim of "
            "theirs approved after they leave is not paid automatically; add it "
            "to their full and final settlement.",
            "Receipts are stored in the database, validated by their bytes, until "
            "object storage exists.",
        ],
    }
