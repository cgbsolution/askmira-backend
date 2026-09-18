"""Rewards as people use them, and the scheduled half of the programme.

Nothing here updates or deletes a points ledger row; every ledger change still
goes through `service.py`, where the invariants watch for it.

Scheduler-callable (no scheduler exists in this deployment - HR runs them from
the admin screen, or an external cron calls the endpoints):

- `run_expiry(session, tenant_id)` writes EXPIRED rows for lots past the policy.
- `send_expiry_warnings(session, tenant_id)` notifies each person whose points
  fall inside the policy's warning window, once per expiry date.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.notifications import Channel, Notification, NotificationTemplate
from app.models.org import Department
from app.models.rewards import (
    BudgetScopeEnum,
    FulfilmentRecord,
    PointsBudget,
    PointsExpiryPolicy,
    PointsLedgerEntry,
    PointsRedemption,
    PointsValuation,
    ReconciliationRun,
    RedemptionStatusEnum,
    RewardInvoiceLine,
    RewardTaxRule,
)
from app.models.user import User
from app.schemas.rewards import (
    BudgetCreate,
    ExpiryPolicyRead,
    ExpiryWarningRunRead,
    ReconciliationRunRead,
    RedemptionRead,
    RewardsImportCreate,
    RewardsImportRead,
    TaxRuleRead,
    ValuationRead,
)
from app.services import audit
from app.services.notifications import dispatch
from app.services.rewards import ledger as ledger_engine
from app.services.rewards import service as rewards
from app.services.rewards import tax as tax_engine

__all__ = [
    "EXPIRY_EVENT",
    "budget_report",
    "create_budget",
    "get_expiry_policy",
    "import_partner_records",
    "list_reconciliations",
    "list_redemptions",
    "list_tax_rules",
    "list_valuations",
    "my_statement",
    "run_expiry",
    "send_expiry_warnings",
]

EXPIRY_EVENT = "rewards.points.expiring"

#: Installed for the tenant the first time a warning is sent, if the tenant has
#: no template of its own for this event. The notification engine sends nothing
#: without a template, and a warning that silently never arrives is the failure
#: the whole expiry rule exists to prevent.
_EXPIRY_TEMPLATES: dict[Channel, tuple[str, str]] = {
    Channel.IN_APP: (
        "{points} points expire on {expires_on}",
        "{points} of your recognition points expire on {expires_on}. Redeem them "
        "before then in AskMira. {disclosure}",
    ),
    Channel.EMAIL: (
        "Your AskMira points expire on {expires_on}",
        "{points} of your recognition points expire on {expires_on}. You can redeem "
        "them in AskMira until then. {disclosure}",
    ),
}


@dataclass(frozen=True, slots=True)
class SystemActor:
    """The caller for a scheduled run: one tenant, and nobody in it."""

    tenant_id: uuid.UUID
    id: None = None
    employee_id: None = None
    role: None = None


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


# ── My points ───────────────────────────────────────────────────────────────


async def my_statement(
    session: AsyncSession, user: User, *, as_of: date | None = None
) -> dict[str, Any]:
    """The caller's own balance, rows, expiry and tax position this year."""
    me = permissions.own_employee_id(user)
    today = as_of or date.today()
    statement = await rewards.employee_statement(
        session,
        user,
        employee_id=me,
        financial_year=tax_engine.financial_year(today),
        as_of=today,
    )
    statement["financial_year"] = tax_engine.financial_year(today)
    return statement


# ── Configuration reads ─────────────────────────────────────────────────────


async def list_valuations(session: AsyncSession, user: User) -> list[ValuationRead]:
    rows = (
        await session.scalars(
            tenant_scoped(
                select(PointsValuation).order_by(PointsValuation.effective_from.desc()),
                PointsValuation,
                user,
            )
        )
    ).all()
    return [ValuationRead.model_validate(row) for row in rows]


async def get_expiry_policy(session: AsyncSession, user: User) -> ExpiryPolicyRead:
    row = await session.scalar(
        tenant_scoped(select(PointsExpiryPolicy), PointsExpiryPolicy, user)
    )
    if row is None:
        return ExpiryPolicyRead(
            configured=False,
            enabled=False,
            expire_after_days=None,
            notify_days_before=None,
            disclosure="Points do not expire.",
        )
    return ExpiryPolicyRead(
        configured=True,
        enabled=row.enabled,
        expire_after_days=row.expire_after_days,
        notify_days_before=row.notify_days_before,
        disclosure=row.disclosure,
    )


async def list_tax_rules(session: AsyncSession, user: User) -> list[TaxRuleRead]:
    rows = (
        await session.scalars(
            tenant_scoped(
                select(RewardTaxRule).order_by(
                    RewardTaxRule.reward_type, RewardTaxRule.effective_from.desc()
                ),
                RewardTaxRule,
                user,
            )
        )
    ).all()
    return [TaxRuleRead.model_validate(row) for row in rows]


# ── Budgets ─────────────────────────────────────────────────────────────────


async def create_budget(
    session: AsyncSession, user: User, payload: BudgetCreate, *, request: Request | None = None
) -> dict[str, Any]:
    """Allocate points to a scope for a period.

    A manager budget is keyed to the manager's login, which is the key an award
    is charged against. A department must exist in this tenant.
    """
    permissions.require_hr(user, detail="Points budgets are set by HR.")
    if payload.scope is BudgetScopeEnum.COMPANY:
        scope_key = str(user.tenant_id)
    elif payload.scope is BudgetScopeEnum.DEPARTMENT:
        department = await session.scalar(
            tenant_scoped(
                select(Department.id).where(Department.id == payload.department_id),
                Department,
                user,
            )
        )
        if department is None:
            raise _not_found("No such department.")
        scope_key = str(department)
    else:
        login = await session.scalar(
            tenant_scoped(
                select(User.id).where(
                    User.employee_id == payload.manager_employee_id,
                    User.is_active.is_(True),
                ),
                User,
                user,
            )
        )
        if login is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="That person has no active login, so no award can be charged to them.",
            )
        scope_key = str(login)

    clash = await session.scalar(
        tenant_scoped(
            select(PointsBudget.id).where(
                PointsBudget.scope == payload.scope,
                PointsBudget.scope_key == scope_key,
                PointsBudget.period_start == payload.period_start,
            ),
            PointsBudget,
            user,
        )
    )
    if clash is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A budget for this scope already starts on that date.",
        )

    row = PointsBudget(
        tenant_id=user.tenant_id,
        scope=payload.scope,
        scope_key=scope_key,
        period_start=payload.period_start,
        period_end=payload.period_end,
        allocated_points=payload.allocated_points,
        consumed_points=0,
        enforcement=payload.enforcement,
    )
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.RULE_CHANGE,
        entity_type="points_budget",
        entity_id=row.id,
        actor=user,
        summary=(
            f"{payload.scope.value.title()} points budget of {payload.allocated_points} "
            f"for {payload.period_start.isoformat()} to {payload.period_end.isoformat()}, "
            f"{payload.enforcement.value.replace('_', ' ')}."
        ),
        request=request,
    )
    await session.commit()
    return {
        "budget_id": str(row.id),
        "scope": row.scope.value,
        "scope_key": row.scope_key,
        "allocated_points": row.allocated_points,
        "period_start": row.period_start.isoformat(),
        "period_end": row.period_end.isoformat(),
        "enforcement": row.enforcement.value,
    }


async def budget_report(
    session: AsyncSession, user: User, *, as_of: date
) -> dict[str, Any]:
    """Utilisation per budget, with each scope named."""
    report = await rewards.budget_utilisation(session, user, as_of=as_of)
    keys: set[uuid.UUID] = set()
    for budget in report["budgets"]:
        try:
            keys.add(uuid.UUID(budget["scope_key"]))
        except ValueError:
            continue
    departments: dict[uuid.UUID, str] = {}
    managers: dict[uuid.UUID, str] = {}
    if keys:
        departments = {
            dept_id: name
            for dept_id, name in (
                await session.execute(
                    tenant_scoped(
                        select(Department.id, Department.name).where(Department.id.in_(keys)),
                        Department,
                        user,
                    )
                )
            ).all()
        }
        managers = {
            user_id: name
            for user_id, name in (
                await session.execute(
                    tenant_scoped(
                        select(User.id, User.full_name).where(User.id.in_(keys)), User, user
                    )
                )
            ).all()
        }
    for budget in report["budgets"]:
        try:
            key = uuid.UUID(budget["scope_key"])
        except ValueError:
            key = None
        if budget["scope"] == BudgetScopeEnum.COMPANY.value:
            budget["scope_label"] = "Whole company"
        elif budget["scope"] == BudgetScopeEnum.DEPARTMENT.value:
            budget["scope_label"] = departments.get(key, "Unknown department")
        else:
            budget["scope_label"] = managers.get(key, "Unknown manager")
    return report


# ── Redemptions and the merchandise partner ─────────────────────────────────


async def list_redemptions(
    session: AsyncSession,
    user: User,
    *,
    status_filter: RedemptionStatusEnum | None = None,
    limit: int = 200,
) -> list[RedemptionRead]:
    permissions.require_hr(user, detail="The redemption register is for HR.")
    stmt = (
        select(PointsRedemption, Employee)
        .join(Employee, Employee.id == PointsRedemption.employee_id)
        .order_by(PointsRedemption.requested_on.desc(), PointsRedemption.created_at.desc())
        .limit(limit)
    )
    if status_filter is not None:
        stmt = stmt.where(PointsRedemption.status == status_filter)
    rows = (await session.execute(tenant_scoped(stmt, PointsRedemption, user))).all()
    return [
        RedemptionRead(
            id=r.id,
            employee_id=e.id,
            employee_name=e.full_name,
            points=r.points,
            rupee_value=r.rupee_value,
            item_sku=r.item_sku,
            item_description=r.item_description,
            status=r.status,
            requested_on=r.requested_on,
        )
        for r, e in rows
    ]


async def import_partner_records(
    session: AsyncSession,
    user: User,
    payload: RewardsImportCreate,
    *,
    request: Request | None = None,
) -> RewardsImportRead:
    """Load what the merchandise partner says it shipped and billed.

    Stored as received, for reconciliation to compare against our own
    redemptions. A fulfilment naming a redemption that is not ours keeps that
    reference as the partner's, because an orphan is a finding. An order already
    imported is skipped and named; an identical invoice line is not loaded twice.
    """
    permissions.require_hr(user, detail="Partner records are imported by HR.")
    today = date.today()
    order_ids = {f.order_id for f in payload.fulfilments}
    existing_orders: set[str] = set()
    if order_ids:
        existing_orders = set(
            (
                await session.scalars(
                    tenant_scoped(
                        select(FulfilmentRecord.order_id).where(
                            FulfilmentRecord.order_id.in_(order_ids)
                        ),
                        FulfilmentRecord,
                        user,
                    )
                )
            ).all()
        )
    claimed = {f.redemption_id for f in payload.fulfilments if f.redemption_id}
    ours: set[uuid.UUID] = set()
    if claimed:
        ours = set(
            (
                await session.scalars(
                    tenant_scoped(
                        select(PointsRedemption.id).where(PointsRedemption.id.in_(claimed)),
                        PointsRedemption,
                        user,
                    )
                )
            ).all()
        )

    skipped: list[str] = []
    seen: set[str] = set()
    imported = 0
    for f in payload.fulfilments:
        if f.order_id in existing_orders or f.order_id in seen:
            skipped.append(f.order_id)
            continue
        seen.add(f.order_id)
        known = f.redemption_id if f.redemption_id in ours else None
        session.add(
            FulfilmentRecord(
                tenant_id=user.tenant_id,
                redemption_id=known,
                external_redemption_ref=(
                    f.external_redemption_ref
                    or (str(f.redemption_id) if f.redemption_id and known is None else None)
                ),
                order_id=f.order_id,
                item_sku=f.item_sku,
                status=f.status.strip().lower(),
                fulfilled_on=f.fulfilled_on,
                imported_on=today,
            )
        )
        imported += 1

    line_orders = {line.order_id for line in payload.invoice_lines}
    existing_lines: set[tuple[str, str | None, str, str]] = set()
    if line_orders:
        existing_lines = {
            (row.order_id, row.invoice_ref, str(row.amount), row.invoiced_on.isoformat())
            for row in (
                await session.scalars(
                    tenant_scoped(
                        select(RewardInvoiceLine).where(
                            RewardInvoiceLine.order_id.in_(line_orders)
                        ),
                        RewardInvoiceLine,
                        user,
                    )
                )
            ).all()
        }
    lines = 0
    for line in payload.invoice_lines:
        signature = (
            line.order_id,
            line.invoice_ref,
            str(line.amount.quantize(ledger_engine.money(0))),
            line.invoiced_on.isoformat(),
        )
        if signature in existing_lines:
            continue
        existing_lines.add(signature)
        session.add(
            RewardInvoiceLine(
                tenant_id=user.tenant_id,
                order_id=line.order_id,
                amount=line.amount,
                item_sku=line.item_sku,
                invoice_ref=line.invoice_ref,
                invoiced_on=line.invoiced_on,
            )
        )
        lines += 1

    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="reward_partner_import",
        entity_id=None,
        actor=user,
        summary=(
            f"Imported {imported} fulfilment(s) and {lines} invoice line(s) from the "
            f"merchandise partner; {len(skipped)} already-imported order(s) skipped."
        ),
        request=request,
    )
    await session.commit()
    return RewardsImportRead(
        fulfilments_imported=imported,
        fulfilments_skipped=skipped,
        invoice_lines_imported=lines,
        note=(
            "Stored as received. Run the month's reconciliation to compare them with "
            "the redemptions on record."
        ),
    )


async def list_reconciliations(
    session: AsyncSession, user: User
) -> list[ReconciliationRunRead]:
    permissions.require_hr(user, detail="Reconciliations are for HR and finance.")
    rows = (
        await session.scalars(
            tenant_scoped(
                select(ReconciliationRun).order_by(ReconciliationRun.period_label.desc()),
                ReconciliationRun,
                user,
            )
        )
    ).all()
    return [
        ReconciliationRunRead(
            id=r.id,
            period_label=r.period_label,
            status=r.status,
            redemption_count=r.redemption_count,
            fulfilment_count=r.fulfilment_count,
            invoice_line_count=r.invoice_line_count,
            redeemed_value=r.redeemed_value,
            invoiced_value=r.invoiced_value,
            gap_count=r.gap_count,
            gaps=list(r.gaps or []),
            run_on=r.run_on,
            created_at=r.created_at,
        )
        for r in rows
    ]


# ── Scheduler-callable ──────────────────────────────────────────────────────


async def run_expiry(
    session: AsyncSession, tenant_id: uuid.UUID, *, as_of: date | None = None
) -> dict[str, Any]:
    """Scheduler-callable: write EXPIRED rows for lots past the policy."""
    return await rewards.expire_points(
        session, SystemActor(tenant_id=tenant_id), as_of=as_of or date.today()
    )


async def _ensure_expiry_templates(session: AsyncSession, actor: SystemActor) -> None:
    present = set(
        (
            await session.scalars(
                tenant_scoped(
                    select(NotificationTemplate.channel).where(
                        NotificationTemplate.event_key == EXPIRY_EVENT
                    ),
                    NotificationTemplate,
                    actor,
                )
            )
        ).all()
    )
    for channel, (subject, body) in _EXPIRY_TEMPLATES.items():
        if channel not in present:
            session.add(
                NotificationTemplate(
                    tenant_id=actor.tenant_id,
                    event_key=EXPIRY_EVENT,
                    channel=channel,
                    subject=subject,
                    body=body,
                )
            )
    await session.flush()


async def send_expiry_warnings(
    session: AsyncSession, tenant_id: uuid.UUID, *, as_of: date | None = None
) -> ExpiryWarningRunRead:
    """Scheduler-callable: warn each person whose points expire within the
    policy's notice window.

    One notification per person per expiry date, and never a second one for a
    date already warned about - checked against the notifications already on
    record, so a scheduler running this every night does not nag.
    """
    actor = SystemActor(tenant_id=tenant_id)
    today = as_of or date.today()
    policy_row = await session.scalar(
        tenant_scoped(select(PointsExpiryPolicy), PointsExpiryPolicy, actor)
    )
    if policy_row is None or not policy_row.enabled:
        return ExpiryWarningRunRead(
            employees_warned=0,
            lots_warned=0,
            already_warned=0,
            note="No expiry policy is enabled, so no points are expiring.",
        )
    policy = ledger_engine.ExpiryPolicy(
        expire_after_days=policy_row.expire_after_days,
        notify_days_before=policy_row.notify_days_before,
        disclosure=policy_row.disclosure,
        enabled=True,
    )

    employee_ids = list(
        (
            await session.scalars(
                tenant_scoped(
                    select(PointsLedgerEntry.employee_id).distinct(),
                    PointsLedgerEntry,
                    actor,
                )
            )
        ).all()
    )
    logins: dict[uuid.UUID, uuid.UUID] = {}
    if employee_ids:
        logins = {
            employee_id: user_id
            for employee_id, user_id in (
                await session.execute(
                    tenant_scoped(
                        select(User.employee_id, User.id).where(
                            User.employee_id.in_(employee_ids), User.is_active.is_(True)
                        ),
                        User,
                        actor,
                    )
                )
            ).all()
        }
    await _ensure_expiry_templates(session, actor)

    people = lots = already = 0
    for employee_id in employee_ids:
        login = logins.get(employee_id)
        if login is None:
            continue
        entries = await rewards._ledger_entries(session, actor, employee_id=employee_id)
        expiring = ledger_engine.expiring_soon(entries, policy=policy, as_of=today)
        if not expiring:
            continue
        by_date: dict[str, list[dict[str, Any]]] = {}
        for lot in expiring:
            by_date.setdefault(lot["expires_on"], []).append(lot)
        warned_dates = set(
            (
                await session.scalars(
                    tenant_scoped(
                        select(Notification.payload["expires_on"].astext).where(
                            Notification.recipient_user_id == login,
                            Notification.event_key == EXPIRY_EVENT,
                        ),
                        Notification,
                        actor,
                    )
                )
            ).all()
        )
        fresh = {d: group for d, group in by_date.items() if d not in warned_dates}
        already += len(by_date) - len(fresh)
        for expires_on, group in sorted(fresh.items()):
            await dispatch.notify(
                session,
                tenant_id=tenant_id,
                event_key=EXPIRY_EVENT,
                recipient_user_ids=[login],
                payload={
                    "points": sum(lot["points"] for lot in group),
                    "expires_on": expires_on,
                    "disclosure": policy.disclosure,
                },
                commit=False,
            )
            lots += len(group)
        if fresh:
            people += 1

    if people:
        await audit.record(
            session,
            tenant_id=tenant_id,
            action=AuditAction.CREATE,
            entity_type="points_expiry_warning",
            entity_id=None,
            actor=None,
            actor_type="system",
            summary=f"Expiry warnings sent to {people} people for {lots} lot(s).",
        )
    await session.commit()
    return ExpiryWarningRunRead(
        employees_warned=people,
        lots_warned=lots,
        already_warned=already,
        note=(
            f"Warned {people} people. Delivery follows each person's notification "
            "preferences; email is a console adapter in this deployment."
            if people
            else "Nobody had points inside the warning window that had not already "
            "been warned about."
        ),
    )
