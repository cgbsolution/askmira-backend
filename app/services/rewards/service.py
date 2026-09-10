"""The DB-facing half of the rewards programme.

The valuation resolution, the FIFO liability, the movement arithmetic, the
budget check, the perquisite treatment and the reconciliation all live in the
pure modules beside this. An invariant checks the isolation.

═══════════════════════════════════════════════════════════════════════════════
THIS MODULE NEVER UPDATES OR DELETES A LEDGER ROW
═══════════════════════════════════════════════════════════════════════════════

Every correction is an insert: `REVERSED` for a mistaken award, `ADJUSTED` for a
signed correction, `EXPIRED` for points that timed out. An invariant fails the
build if an assignment to a ledger row or a delete against the table appears
here.

The reason is not purity. A row that disappears leaves an employee who had 500
points and now has none, with nothing on their statement to explain it, and a
support conversation nobody can resolve because there is no record of what
happened.

**What is and is not wired**, stated once here:

- `payroll_perquisites` produces the figure and `reward_events.payroll_reference`
  stays null. **Nothing in payroll consumes it**, so a crossed threshold is
  visible and unprocessed rather than silently applied.
- Fulfilment and invoice rows are **imported by a caller**. There is no
  connection to a merchandise partner, so a reconciliation runs against
  whatever has been loaded.
- Expiry is computed and returned. **Nothing schedules it**, and nothing sends
  the warning; the notification engine is not called from here.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.rewards import (
    BudgetScopeEnum,
    FulfilmentRecord,
    PointsBudget,
    PointsExpiryPolicy,
    PointsLedgerEntry,
    PointsRedemption,
    PointsTransactionTypeEnum,
    PointsValuation,
    ReconciliationRun,
    ReconciliationStatusEnum,
    RedemptionStatusEnum,
    RewardEvent,
    RewardInvoiceLine,
    RewardTaxRule,
    TaxTreatmentEnum,
)
from app.models.user import User
from app.services import audit
from app.services.rewards import budgets as budget_engine
from app.services.rewards import ledger as ledger_engine
from app.services.rewards import reconciliation as recon_engine
from app.services.rewards import tax as tax_engine
from app.services.rewards import valuation as valuation_engine

__all__ = [
    "award_points",
    "budget_utilisation",
    "employee_statement",
    "expire_points",
    "liability_report",
    "movement",
    "perquisites",
    "redeem_points",
    "reverse_entry",
    "rewards_posture",
    "run_reconciliation",
    "set_expiry_policy",
    "set_tax_rule",
    "set_valuation",
]


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _d(value: Any) -> Decimal:
    return Decimal(str(value if value is not None else 0))


# ══════════════════════════════════════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════════════════════════════════════


async def set_valuation(
    session: AsyncSession,
    user: User,
    *,
    points_per_rupee: Decimal,
    effective_from: date,
    note: str | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """Add a valuation from a date. It never edits an existing one.

    The response says what the change does *not* do, because the intuition is
    wrong: people expect a revaluation to move the liability and it does not
    touch a point already issued.
    """
    try:
        valuation_engine.PointsValuation(
            points_per_rupee=_d(points_per_rupee), effective_from=effective_from
        )
    except valuation_engine.ValuationError as exc:
        raise _bad_request(str(exc)) from exc

    row = PointsValuation(
        tenant_id=user.tenant_id,
        points_per_rupee=_d(points_per_rupee),
        effective_from=effective_from,
        note=note,
    )
    session.add(row)

    entries = await _ledger_entries(session, user)
    current = ledger_engine.liability(entries)
    impact = valuation_engine.revaluation_impact(
        outstanding_points=current.balance_points,
        current_liability=current.rupees,
        new_points_per_rupee=_d(points_per_rupee),
    )

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.RULE_CHANGE,
        entity_type="points_valuation",
        entity_id=row.id,
        actor=user,
        summary=(
            f"Points valuation set to {points_per_rupee} per rupee from "
            f"{effective_from.isoformat()}. Issued points keep their rate."
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return {"valuation_id": str(row.id), "impact": impact}


async def _valuations(
    session: AsyncSession, user: User
) -> list[valuation_engine.PointsValuation]:
    stmt = tenant_scoped(select(PointsValuation), PointsValuation, user)
    return [
        valuation_engine.PointsValuation(
            points_per_rupee=_d(row.points_per_rupee),
            effective_from=row.effective_from,
            note=row.note or "",
        )
        for row in (await session.execute(stmt)).scalars()
    ]


async def set_expiry_policy(
    session: AsyncSession,
    user: User,
    *,
    enabled: bool,
    expire_after_days: int,
    notify_days_before: int,
    disclosure: str,
    request: Request | None = None,
) -> PointsExpiryPolicy:
    try:
        ledger_engine.ExpiryPolicy(
            expire_after_days=expire_after_days,
            notify_days_before=notify_days_before,
            disclosure=disclosure,
            enabled=enabled,
        )
    except ledger_engine.LedgerError as exc:
        raise _bad_request(str(exc)) from exc

    stmt = tenant_scoped(select(PointsExpiryPolicy), PointsExpiryPolicy, user)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        row = PointsExpiryPolicy(tenant_id=user.tenant_id, disclosure=disclosure)
        session.add(row)
    row.enabled = enabled
    row.expire_after_days = expire_after_days
    row.notify_days_before = notify_days_before
    row.disclosure = disclosure

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.RULE_CHANGE,
        entity_type="points_expiry_policy",
        entity_id=row.id,
        actor=user,
        summary=(
            f"Points expire after {expire_after_days} days"
            if enabled
            else "Points expiry disabled."
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def _expiry_policy(
    session: AsyncSession, user: User
) -> ledger_engine.ExpiryPolicy | None:
    stmt = tenant_scoped(select(PointsExpiryPolicy), PointsExpiryPolicy, user)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        return None
    return ledger_engine.ExpiryPolicy(
        expire_after_days=row.expire_after_days,
        notify_days_before=row.notify_days_before,
        disclosure=row.disclosure,
        enabled=row.enabled,
    )


async def set_tax_rule(
    session: AsyncSession,
    user: User,
    *,
    reward_type: str,
    treatment: TaxTreatmentEnum,
    exemption_threshold_per_year: Decimal,
    effective_from: date,
    basis: str | None = None,
    reviewed: bool = False,
    reviewed_by: str | None = None,
    reviewed_on: date | None = None,
    request: Request | None = None,
) -> RewardTaxRule:
    """Record a tax treatment rule. Unreviewed unless a person is named.

    `reviewed` without `reviewed_by` is refused by the engine, the service and a
    check constraint. "Reviewed" with nobody attached is exactly the state the
    flag exists to prevent.
    """
    try:
        tax_engine.TaxTreatmentRule(
            reward_type=reward_type,
            treatment=tax_engine.TaxTreatment(treatment.value),
            exemption_threshold_per_year=_d(exemption_threshold_per_year),
            effective_from=effective_from,
            basis=basis or "",
            reviewed=reviewed,
            reviewed_by=reviewed_by or "",
            reviewed_on=reviewed_on,
        )
    except tax_engine.TaxError as exc:
        raise _bad_request(str(exc)) from exc

    row = RewardTaxRule(
        tenant_id=user.tenant_id,
        reward_type=reward_type,
        treatment=treatment,
        exemption_threshold_per_year=_d(exemption_threshold_per_year),
        effective_from=effective_from,
        basis=basis,
        reviewed=reviewed,
        reviewed_by=reviewed_by,
        reviewed_on=reviewed_on,
    )
    session.add(row)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.RULE_CHANGE,
        entity_type="reward_tax_rule",
        entity_id=row.id,
        actor=user,
        summary=(
            f"Tax rule for {reward_type}: {treatment.value}, threshold "
            f"{exemption_threshold_per_year} from {effective_from.isoformat()}"
            + (f", reviewed by {reviewed_by}." if reviewed else ", NOT reviewed.")
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def _tax_rules(
    session: AsyncSession, user: User
) -> list[tax_engine.TaxTreatmentRule]:
    """Configured rules, falling back to the unreviewed defaults.

    The fallback is deliberate and safe: every default carries `reviewed=False`,
    so payroll refuses them. A tenant that has configured nothing gets a
    provisional figure that says so, rather than an exception nobody can act on
    or - far worse - an assumption of exemption.
    """
    stmt = tenant_scoped(select(RewardTaxRule), RewardTaxRule, user)
    rows = list((await session.execute(stmt)).scalars())
    configured = [
        tax_engine.TaxTreatmentRule(
            reward_type=row.reward_type,
            treatment=tax_engine.TaxTreatment(row.treatment.value),
            exemption_threshold_per_year=_d(row.exemption_threshold_per_year),
            effective_from=row.effective_from,
            basis=row.basis or "",
            reviewed=row.reviewed,
            reviewed_by=row.reviewed_by or "",
            reviewed_on=row.reviewed_on,
        )
        for row in rows
    ]
    covered = {r.reward_type for r in configured}
    return configured + [
        r for r in tax_engine.DEFAULT_RULES if r.reward_type not in covered
    ]


# ══════════════════════════════════════════════════════════════════════════════
# The ledger
# ══════════════════════════════════════════════════════════════════════════════


async def _ledger_entries(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID | None = None,
) -> list[ledger_engine.LedgerEntry]:
    stmt = tenant_scoped(select(PointsLedgerEntry), PointsLedgerEntry, user)
    if employee_id is not None:
        stmt = stmt.where(PointsLedgerEntry.employee_id == employee_id)
    return [
        ledger_engine.LedgerEntry(
            entry_id=str(row.id),
            employee_id=str(row.employee_id),
            transaction_type=ledger_engine.TransactionType(
                row.transaction_type.value
            ),
            points=row.points,
            monetary_value=_d(row.monetary_value),
            occurred_at=row.occurred_at,
            source_type=row.source_type or "",
            source_id=str(row.source_id) if row.source_id else "",
            balance_after=row.balance_after,
            department_key=row.department_key or "",
            cost_centre=row.cost_centre or "",
            rate_per_point=_d(row.rate_per_point),
        )
        for row in (await session.execute(stmt)).scalars()
    ]


async def _insert_entry(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    transaction_type: PointsTransactionTypeEnum,
    points: int,
    rate_per_point: Decimal,
    occurred_at: datetime,
    source_type: str | None = None,
    source_id: uuid.UUID | None = None,
    department_key: str | None = None,
    cost_centre: str | None = None,
    reverses_entry_id: uuid.UUID | None = None,
    reason: str | None = None,
) -> PointsLedgerEntry:
    """Append one row. There is no counterpart that changes or removes one."""
    existing = await _ledger_entries(session, user, employee_id=employee_id)
    signed = points if transaction_type is PointsTransactionTypeEnum.ADJUSTED else (
        points * ledger_engine.TransactionType(transaction_type.value).sign
    )
    row = PointsLedgerEntry(
        tenant_id=user.tenant_id,
        employee_id=employee_id,
        transaction_type=transaction_type,
        points=points,
        monetary_value=ledger_engine.money(Decimal(abs(points)) * rate_per_point),
        rate_per_point=rate_per_point,
        source_type=source_type,
        source_id=source_id,
        balance_after=ledger_engine.derive_balance(existing) + signed,
        occurred_at=occurred_at,
        department_key=department_key,
        cost_centre=cost_centre,
        reverses_entry_id=reverses_entry_id,
        reason=reason,
        created_by_user_id=user.id,
    )
    session.add(row)
    return row


async def award_points(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    points: int,
    awarded_on: date,
    source_type: str = "recognition",
    source_id: uuid.UUID | None = None,
    manager_key: str | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """Award points, valued at today's rate and checked against every budget.

    The valuation is resolved once, here, and stamped onto the row. Nothing
    looks it up again, which is what makes a later revaluation unable to
    restate this award.
    """
    if points <= 0:
        raise _bad_request("An award of zero or fewer points is not an award.")

    employee = (
        await session.execute(
            tenant_scoped(
                select(Employee).where(Employee.id == employee_id), Employee, user
            )
        )
    ).scalar_one_or_none()
    if employee is None:
        raise _not_found("No such employee in this tenant.")

    try:
        valued = valuation_engine.value_award(
            await _valuations(session, user), points=points, awarded_on=awarded_on
        )
    except valuation_engine.ValuationError as exc:
        raise _bad_request(str(exc)) from exc

    department_key = str(employee.department_id) if employee.department_id else None
    scope_keys = {
        BudgetScopeEnum.COMPANY: str(user.tenant_id),
        BudgetScopeEnum.DEPARTMENT: department_key or "",
        BudgetScopeEnum.MANAGER: manager_key or str(user.id),
    }
    budget_rows = list(
        (
            await session.execute(
                tenant_scoped(select(PointsBudget), PointsBudget, user)
            )
        ).scalars()
    )
    engine_budgets = [
        budget_engine.Budget(
            budget_id=str(b.id),
            scope=budget_engine.BudgetScope(b.scope.value),
            scope_key=b.scope_key,
            period_start=b.period_start,
            period_end=b.period_end,
            allocated_points=b.allocated_points,
            consumed_points=b.consumed_points,
            enforcement=budget_engine.EnforcementMode(b.enforcement.value),
        )
        for b in budget_rows
    ]
    decision = budget_engine.check(
        engine_budgets,
        points=points,
        scope_keys={
            budget_engine.BudgetScope(k.value): v for k, v in scope_keys.items()
        },
        as_of=awarded_on,
    )
    if not decision.permitted:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=decision.message
        )

    row = await _insert_entry(
        session,
        user,
        employee_id=employee_id,
        transaction_type=PointsTransactionTypeEnum.AWARDED,
        points=points,
        rate_per_point=Decimal(valued["rate_per_point"]),
        occurred_at=datetime(
            awarded_on.year, awarded_on.month, awarded_on.day, tzinfo=UTC
        ),
        source_type=source_type,
        source_id=source_id,
        department_key=department_key,
    )

    consumed_ids = {
        b.budget_id
        for b in engine_budgets
        if b.covers(awarded_on)
        and scope_keys.get(BudgetScopeEnum(b.scope.value)) == b.scope_key
    }
    for budget in budget_rows:
        if str(budget.id) in consumed_ids:
            budget.consumed_points += points
            overspend = next(
                (w for w in decision.warnings if w["budget_id"] == str(budget.id)),
                None,
            )
            if overspend:
                budget.overspend_points += overspend["overspend"]

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="points_ledger",
        entity_id=row.id,
        actor=user,
        summary=(
            f"{points} points awarded at {valued['rate_per_point']} per point "
            f"({valued['monetary_value']})."
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(row)

    policy = await _expiry_policy(session, user)
    return {
        "entry_id": str(row.id),
        "points": points,
        "monetary_value": valued["monetary_value"],
        "rate_per_point": valued["rate_per_point"],
        "balance_after": row.balance_after,
        "budget": decision.as_dict(),
        # Shown at the point of award, not buried in a policy document.
        "expiry_disclosure": (
            policy.disclosure if policy and policy.enabled else "Points do not expire."
        ),
    }


async def redeem_points(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    points: int,
    requested_on: date,
    item_sku: str | None = None,
    item_description: str | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """Spend points. Valued at the rate of the lots being consumed.

    The rupee value is the FIFO value of what is actually being spent, not the
    balance times today's rate — otherwise a redemption at a revalued rate
    removes more liability than it should and the provision drifts.
    """
    if points <= 0:
        raise _bad_request("A redemption of zero or fewer points is not a redemption.")

    entries = await _ledger_entries(session, user, employee_id=employee_id)
    before = ledger_engine.liability(entries)
    if before.balance_points < points:
        raise _bad_request(
            f"{before.balance_points} points available and {points} requested. "
            "Allowing an overdraft would create a negative balance nobody can "
            "explain on a statement."
        )

    lot_value = Decimal("0")
    remaining = points
    for lot in before.lots:
        if remaining <= 0:
            break
        take = min(lot["remaining_points"], remaining)
        lot_value += Decimal(take) * Decimal(lot["rate_per_point"])
        remaining -= take
    rupee_value = ledger_engine.money(lot_value)
    effective_rate = (
        (rupee_value / Decimal(points)).quantize(Decimal("0.000001"))
        if points
        else Decimal("0")
    )

    redemption = PointsRedemption(
        tenant_id=user.tenant_id,
        employee_id=employee_id,
        points=points,
        rupee_value=rupee_value,
        item_sku=item_sku,
        item_description=item_description,
        status=RedemptionStatusEnum.REQUESTED,
        requested_on=requested_on,
    )
    session.add(redemption)
    await session.flush()

    row = await _insert_entry(
        session,
        user,
        transaction_type=PointsTransactionTypeEnum.REDEEMED,
        employee_id=employee_id,
        points=points,
        rate_per_point=effective_rate,
        occurred_at=datetime(
            requested_on.year, requested_on.month, requested_on.day, tzinfo=UTC
        ),
        source_type="redemption",
        source_id=redemption.id,
    )
    await session.flush()
    redemption.ledger_entry_id = row.id

    # The reward, not the points, is what a perquisite is computed on.
    session.add(
        RewardEvent(
            tenant_id=user.tenant_id,
            employee_id=employee_id,
            reward_type="merchandise",
            value=rupee_value,
            given_on=requested_on,
            financial_year=tax_engine.financial_year(requested_on),
            description=item_description,
            source_id=redemption.id,
            payroll_reference=None,
        )
    )

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="points_redemption",
        entity_id=redemption.id,
        actor=user,
        summary=f"{points} points redeemed for {rupee_value}.",
        request=request,
    )
    await session.commit()
    await session.refresh(redemption)

    return {
        "redemption_id": str(redemption.id),
        "points": points,
        "rupee_value": str(rupee_value),
        "balance_after": row.balance_after,
        "note": (
            "Valued at the rate of the points actually spent, oldest first - "
            "not at today's rate, which would remove more liability than was "
            "created."
        ),
    }


async def reverse_entry(
    session: AsyncSession,
    user: User,
    *,
    entry_id: uuid.UUID,
    reason: str,
    occurred_on: date,
    request: Request | None = None,
) -> dict[str, Any]:
    """Undo an award with a new row. Nothing is edited and nothing is deleted.

    A row that disappears leaves somebody who had 500 points and now has none,
    with nothing on their statement to explain it.
    """
    if not reason.strip():
        raise _bad_request(
            "A reversal needs a reason. It appears on the employee's statement, "
            "and a points balance that dropped for no stated reason is a "
            "support conversation nobody can resolve."
        )

    original = (
        await session.execute(
            tenant_scoped(
                select(PointsLedgerEntry).where(PointsLedgerEntry.id == entry_id),
                PointsLedgerEntry,
                user,
            )
        )
    ).scalar_one_or_none()
    if original is None:
        raise _not_found("No such ledger entry in this tenant.")
    if original.transaction_type is not PointsTransactionTypeEnum.AWARDED:
        raise _bad_request(
            f"Only an award can be reversed; this is a "
            f"{original.transaction_type.value} row. A redemption is undone by "
            "cancelling the order, not by rewriting the ledger."
        )

    already = (
        await session.execute(
            tenant_scoped(
                select(PointsLedgerEntry).where(
                    PointsLedgerEntry.reverses_entry_id == entry_id
                ),
                PointsLedgerEntry,
                user,
            )
        )
    ).scalar_one_or_none()
    if already is not None:
        raise _bad_request(
            "That award has already been reversed. Reversing it twice would "
            "take points the employee never received."
        )

    row = await _insert_entry(
        session,
        user,
        employee_id=original.employee_id,
        transaction_type=PointsTransactionTypeEnum.REVERSED,
        points=original.points,
        rate_per_point=_d(original.rate_per_point),
        occurred_at=datetime(
            occurred_on.year, occurred_on.month, occurred_on.day, tzinfo=UTC
        ),
        source_type="reversal",
        source_id=entry_id,
        department_key=original.department_key,
        reverses_entry_id=entry_id,
        reason=reason,
    )
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="points_ledger",
        entity_id=row.id,
        actor=user,
        summary=f"Award of {original.points} points reversed: {reason}",
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return {
        "entry_id": str(row.id),
        "reverses": str(entry_id),
        "balance_after": row.balance_after,
    }


async def expire_points(
    session: AsyncSession,
    user: User,
    *,
    as_of: date,
    request: Request | None = None,
) -> dict[str, Any]:
    """Write EXPIRED rows for lots past the policy. Nothing is deleted.

    An expired point that vanished from the ledger is a liability reduction
    nobody can audit.
    """
    policy = await _expiry_policy(session, user)
    if policy is None or not policy.enabled:
        return {
            "expired_entries": 0,
            "points_expired": 0,
            "note": "No expiry policy is enabled for this tenant.",
        }

    employees = list(
        (
            await session.execute(
                tenant_scoped(select(Employee), Employee, user)
            )
        ).scalars()
    )
    total_points = 0
    written = 0

    for employee in employees:
        entries = await _ledger_entries(session, user, employee_id=employee.id)
        for lot in ledger_engine.expirable(entries, policy=policy, as_of=as_of):
            await _insert_entry(
                session,
                user,
                employee_id=employee.id,
                transaction_type=PointsTransactionTypeEnum.EXPIRED,
                points=lot["points"],
                rate_per_point=Decimal(lot["rupees"]) / Decimal(lot["points"]),
                occurred_at=datetime(as_of.year, as_of.month, as_of.day, tzinfo=UTC),
                source_type="expiry",
                reason=(
                    f"Expired {lot['age_days']} days after award, per the "
                    f"{policy.expire_after_days}-day policy."
                ),
            )
            total_points += lot["points"]
            written += 1

    if written:
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.UPDATE,
            entity_type="points_ledger",
            entity_id=user.tenant_id,
            actor=user,
            summary=f"{total_points} points expired across {written} lot(s).",
            request=request,
        )
    await session.commit()
    return {
        "expired_entries": written,
        "points_expired": total_points,
        "note": (
            "Written as EXPIRED rows. Nothing was deleted - an expired point "
            "that vanished is a liability reduction nobody can audit."
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Reporting
# ══════════════════════════════════════════════════════════════════════════════


async def liability_report(
    session: AsyncSession,
    user: User,
    *,
    as_of: date,
    group_by: str = "department_key",
) -> dict[str, Any]:
    entries = await _ledger_entries(session, user)
    result = ledger_engine.liability(entries)
    try:
        grouped = ledger_engine.by_group(entries, key=group_by)
    except ledger_engine.LedgerError as exc:
        raise _bad_request(str(exc)) from exc

    return {
        "as_of": as_of.isoformat(),
        "liability": result.as_dict(),
        "ageing": ledger_engine.ageing(entries, as_of=as_of),
        "by_group": grouped,
        "expected_redemption": ledger_engine.expected_redemption_rate(entries),
        "chain": ledger_engine.verify_chain(entries),
    }


async def movement(
    session: AsyncSession,
    user: User,
    *,
    period_start: date,
    period_end: date,
) -> dict[str, Any]:
    entries = await _ledger_entries(session, user)
    try:
        return ledger_engine.movement_report(
            entries, period_start=period_start, period_end=period_end
        )
    except ledger_engine.LedgerError as exc:
        raise _bad_request(str(exc)) from exc


async def budget_utilisation(
    session: AsyncSession, user: User, *, as_of: date
) -> dict[str, Any]:
    rows = list(
        (
            await session.execute(tenant_scoped(select(PointsBudget), PointsBudget, user))
        ).scalars()
    )
    try:
        current = valuation_engine.rate_on(
            await _valuations(session, user), as_of=as_of
        )
    except valuation_engine.ValuationError as exc:
        raise _bad_request(str(exc)) from exc

    return budget_engine.utilisation(
        [
            budget_engine.Budget(
                budget_id=str(b.id),
                scope=budget_engine.BudgetScope(b.scope.value),
                scope_key=b.scope_key,
                period_start=b.period_start,
                period_end=b.period_end,
                allocated_points=b.allocated_points,
                consumed_points=b.consumed_points,
                enforcement=budget_engine.EnforcementMode(b.enforcement.value),
            )
            for b in rows
        ],
        as_of=as_of,
        rate_per_point=current.rupees_per_point,
    )


async def _reward_events(
    session: AsyncSession, user: User, *, employee_id: uuid.UUID | None = None
) -> list[tax_engine.RewardEvent]:
    stmt = tenant_scoped(select(RewardEvent), RewardEvent, user)
    if employee_id is not None:
        stmt = stmt.where(RewardEvent.employee_id == employee_id)
    return [
        tax_engine.RewardEvent(
            employee_id=str(row.employee_id),
            reward_type=row.reward_type,
            value=_d(row.value),
            given_on=row.given_on,
            source_id=str(row.source_id) if row.source_id else "",
            description=row.description or "",
        )
        for row in (await session.execute(stmt)).scalars()
    ]


async def perquisites(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    financial_year: str,
    as_of: date,
    allow_provisional: bool = False,
) -> dict[str, Any]:
    """What should flow into payroll and TDS for one person.

    Refuses a taxable figure from an unreviewed rule unless the caller asks for
    a provisional one. **Nothing in payroll consumes this yet**, so a crossed
    threshold is visible and unprocessed rather than silently applied.
    """
    events = await _reward_events(session, user, employee_id=employee_id)
    rules = await _tax_rules(session, user)
    try:
        assessments = tax_engine.payroll_perquisites(
            events,
            employee_id=str(employee_id),
            fy=financial_year,
            rules=rules,
            as_of=as_of,
            allow_provisional=allow_provisional,
        )
    except tax_engine.TaxError as exc:
        raise _bad_request(str(exc)) from exc

    return {
        "employee_id": str(employee_id),
        "financial_year": financial_year,
        "assessments": [a.as_dict() for a in assessments],
        "payroll": {
            "handed_over": False,
            "note": (
                "Nothing in payroll consumes this yet. A crossed threshold is "
                "visible and unprocessed rather than silently applied to a "
                "payslip."
            ),
        },
    }


async def employee_statement(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    financial_year: str,
    as_of: date,
) -> dict[str, Any]:
    """What one employee sees: balance, rows, expiry and tax position.

    Shown before the payslip rather than in it. A recognition programme that
    produces an unexplained deduction has spent money to make somebody feel
    worse.
    """
    entries = await _ledger_entries(session, user, employee_id=employee_id)
    result = ledger_engine.liability(entries)
    policy = await _expiry_policy(session, user)

    return {
        "employee_id": str(employee_id),
        "balance_points": result.balance_points,
        "balance_rupees": str(result.rupees),
        "rows": [
            {
                "entry_id": e.entry_id,
                "type": e.transaction_type.value,
                "points": e.signed_points,
                "value": str(e.monetary_value),
                "occurred_at": e.occurred_at.isoformat(),
                "balance_after": e.balance_after,
            }
            for e in sorted(entries, key=lambda e: e.occurred_at, reverse=True)
        ],
        "expiring": (
            ledger_engine.expiring_soon(entries, policy=policy, as_of=as_of)
            if policy
            else []
        ),
        "expiry_disclosure": (
            policy.disclosure if policy and policy.enabled else "Points do not expire."
        ),
        "tax": tax_engine.reward_statement(
            await _reward_events(session, user, employee_id=employee_id),
            employee_id=str(employee_id),
            fy=financial_year,
            rules=await _tax_rules(session, user),
            as_of=as_of,
        ),
    }


async def run_reconciliation(
    session: AsyncSession,
    user: User,
    *,
    period_start: date,
    period_end: date,
    period_label: str,
    request: Request | None = None,
) -> dict[str, Any]:
    """Match redemptions, fulfilments and invoices for a month.

    The run is recorded even when clean: a clean month with no record is
    indistinguishable from a month nobody reconciled, and the question an
    auditor asks is whether it runs every month.
    """
    redemptions = list(
        (
            await session.execute(
                tenant_scoped(
                    select(PointsRedemption)
                    .where(PointsRedemption.requested_on >= period_start)
                    .where(PointsRedemption.requested_on <= period_end),
                    PointsRedemption,
                    user,
                )
            )
        ).scalars()
    )
    fulfilments = list(
        (
            await session.execute(
                tenant_scoped(select(FulfilmentRecord), FulfilmentRecord, user)
            )
        ).scalars()
    )
    invoices = list(
        (
            await session.execute(
                tenant_scoped(
                    select(RewardInvoiceLine)
                    .where(RewardInvoiceLine.invoiced_on >= period_start)
                    .where(RewardInvoiceLine.invoiced_on <= period_end),
                    RewardInvoiceLine,
                    user,
                )
            )
        ).scalars()
    )

    report = recon_engine.reconcile(
        redemptions=[
            recon_engine.RedemptionRecord(
                redemption_id=str(r.id),
                employee_id=str(r.employee_id),
                points=r.points,
                rupee_value=_d(r.rupee_value),
                item_sku=r.item_sku or "",
            )
            for r in redemptions
        ],
        fulfilments=[
            recon_engine.FulfilmentRecord(
                redemption_id=(
                    str(f.redemption_id)
                    if f.redemption_id
                    else (f.external_redemption_ref or f.order_id)
                ),
                order_id=f.order_id,
                item_sku=f.item_sku or "",
                status=f.status,
            )
            for f in fulfilments
        ],
        invoices=[
            recon_engine.InvoiceLine(
                order_id=i.order_id, amount=_d(i.amount), item_sku=i.item_sku or ""
            )
            for i in invoices
        ],
        period_label=period_label,
    )

    existing = (
        await session.execute(
            tenant_scoped(
                select(ReconciliationRun).where(
                    ReconciliationRun.period_label == period_label
                ),
                ReconciliationRun,
                user,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise _bad_request(
            f"{period_label} has already been reconciled. Running it again "
            "would overwrite the record of what was found and acted on - close "
            "the gaps rather than re-running the report."
        )

    session.add(
        ReconciliationRun(
            tenant_id=user.tenant_id,
            period_label=period_label,
            status=(
                ReconciliationStatusEnum.CLEAN
                if report["clean"]
                else ReconciliationStatusEnum.GAPS_FOUND
            ),
            redemption_count=report["redemptions"],
            fulfilment_count=report["fulfilments"],
            invoice_line_count=report["invoice_lines"],
            redeemed_value=Decimal(report["redeemed_value"]),
            invoiced_value=Decimal(report["invoiced_value"]),
            gaps=report["gaps"],
            gap_count=report["gap_count"],
            run_on=period_end,
            run_by_user_id=user.id,
        )
    )
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="reward_reconciliation",
        entity_id=user.tenant_id,
        actor=user,
        summary=(
            f"Reconciliation {period_label}: "
            + ("clean." if report["clean"] else f"{report['gap_count']} gap(s).")
        ),
        request=request,
    )
    await session.commit()
    return report


def rewards_posture() -> dict[str, Any]:
    """What this slice enforces, and what it does not."""
    return {
        "enforced": [
            "The points ledger is insert-only. A correction is a REVERSED or "
            "ADJUSTED row; nothing updates or deletes one.",
            "The balance is derived from the transactions; the stored "
            "balance_after is a checkpoint and a divergence is reported.",
            "Liability is valued at the rate each award was issued at, oldest "
            "consumed first. A revaluation does not restate issued points.",
            "The movement report's arithmetic is checked against the ledger "
            "rather than asserted.",
            "Expiry writes EXPIRED rows and deletes nothing, and an expiry "
            "policy needs a disclosure shown at the point of award.",
            "Budgets are checked across every applicable scope; a hard stop "
            "refuses and a soft warning records the overspend.",
            "A perquisite from an unreviewed tax rule is refused for payroll.",
            "Reconciliation reports each kind of gap separately rather than "
            "netting them.",
        ],
        "not_built": [
            "**No tax advisor has reviewed the thresholds or treatments.** "
            "Every shipped rule is marked unreviewed and produces a provisional "
            "figure that says so. This is the gate on the whole tax section.",
            "Nothing in payroll consumes a perquisite. The figure is produced "
            "and reward_events.payroll_reference stays null, so a crossed "
            "threshold is visible and unprocessed.",
            "No connection to the merchandise partner. Fulfilment and invoice "
            "rows are imported by a caller and a reconciliation runs against "
            "whatever has been loaded.",
            "Nothing schedules expiry and nothing sends the warning. The "
            "notification engine is not called from here.",
            "No provisioning entry is posted to any accounting system. The "
            "liability and the expected redemption rate are reported; the "
            "journal is somebody else's.",
        ],
    }
