"""Earned wage access: what can be drawn, asking, approving, disbursing.

What has been earned this month:

    monthly gross, times days paid so far, divided by days in the month

where monthly gross is the earnings in the salary in force today, and days
paid so far are the judged attendance days up to yesterday (their payable
fraction). Where the workspace does not track attendance, the calendar days
up to yesterday are used, and the figure says so. What can be drawn is the
policy's share of that, less anything already asked for this month.

On approval the amount becomes a one-time deduction in this month's payroll,
created approved — the employee asked (the maker) and HR approved (the
checker). Disbursing is recorded with its bank reference; the transfer itself
happens outside the system.
"""

from __future__ import annotations

import calendar
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.attendance import AttendanceDay
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.ewa import EwaRequest, EwaStatus
from app.models.payroll import ComponentType, EmployeeSalary
from app.models.payroll_inputs import (
    AdjustmentCategory,
    AdjustmentKind,
    AdjustmentRecurrence,
    AdjustmentStatus,
    PayrollAdjustment,
)
from app.models.tenant import Tenant
from app.models.user import User
from app.services import audit
from app.services.payroll.service import active_salary_on

__all__ = [
    "EwaPolicy",
    "available_for",
    "decide",
    "get_policy",
    "list_requests",
    "mark_disbursed",
    "request_advance",
    "set_policy",
]

_OPEN = (EwaStatus.REQUESTED, EwaStatus.APPROVED, EwaStatus.DISBURSED)


class EwaPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    #: Share of what has been earned that may be drawn, in percent.
    max_percent: Decimal = Field(
        default=Decimal("50"), gt=0, le=100, max_digits=5, decimal_places=2
    )
    min_amount: Decimal = Field(default=Decimal("500"), ge=0, max_digits=12, decimal_places=2)
    max_requests_per_month: int = Field(default=2, ge=1, le=10)


async def get_policy(session: AsyncSession, user: User) -> EwaPolicy:
    tenant = await session.get(Tenant, user.tenant_id)
    raw = ((tenant.settings or {}).get("ewa") or {}) if tenant else {}
    return EwaPolicy.model_validate(raw) if raw else EwaPolicy()


async def set_policy(
    session: AsyncSession, user: User, policy: EwaPolicy, *, request: Request | None
) -> EwaPolicy:
    if not permissions.is_admin(user):
        raise permissions.forbidden("Only an admin sets the earned wage access policy.")
    tenant = await session.get(Tenant, user.tenant_id)
    before = await get_policy(session, user)
    settings = dict(tenant.settings or {})
    settings["ewa"] = policy.model_dump(mode="json")
    tenant.settings = settings
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="ewa_policy",
        entity_id=tenant.id,
        actor=user,
        changes={"before": before.model_dump(mode="json"), "after": policy.model_dump(mode="json")},
        summary=(
            f"Earned wage access {'on' if policy.enabled else 'off'}, up to {policy.max_percent}%"
        ),
        request=request,
    )
    await session.commit()
    return policy


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_DOWN)


async def available_for(
    session: AsyncSession, user: User, employee_id: uuid.UUID, *, today: date | None = None
) -> dict[str, Any]:
    day = today or date.today()
    policy = await get_policy(session, user)
    salary = await active_salary_on(session, user, employee_id, day)
    days_in_month = calendar.monthrange(day.year, day.month)[1]
    month_start = day.replace(day=1)
    yesterday = day - timedelta(days=1)

    gross = Decimal(0)
    if salary is not None:
        loaded = await session.scalar(
            tenant_scoped(
                select(EmployeeSalary)
                .options(selectinload(EmployeeSalary.components))
                .where(EmployeeSalary.id == salary.id),
                EmployeeSalary,
                user,
            )
        )
        gross = sum(
            (
                Decimal(str(c.monthly_amount))
                for c in (loaded.components if loaded else [])
                if c.component_type is ComponentType.EARNING
            ),
            start=Decimal(0),
        )

    tracked, paid_days = (0, Decimal(0))
    if yesterday >= month_start:
        tracked, paid = (
            await session.execute(
                tenant_scoped(
                    select(
                        func.count(), func.coalesce(func.sum(AttendanceDay.payable_fraction), 0)
                    ).where(
                        AttendanceDay.employee_id == employee_id,
                        AttendanceDay.attendance_date >= month_start,
                        AttendanceDay.attendance_date <= yesterday,
                    ),
                    AttendanceDay,
                    user,
                )
            )
        ).one()
        paid_days = Decimal(str(paid))
    basis = "attendance"
    if not tracked:
        paid_days = Decimal(max(0, (yesterday - month_start).days + 1))
        basis = "calendar"
    earned = _money(gross * paid_days / Decimal(days_in_month)) if gross else Decimal("0.00")

    drawn = await session.scalar(
        tenant_scoped(
            select(func.coalesce(func.sum(EwaRequest.amount), 0)).where(
                EwaRequest.employee_id == employee_id,
                EwaRequest.period_year == day.year,
                EwaRequest.period_month == day.month,
                EwaRequest.status.in_(_OPEN),
            ),
            EwaRequest,
            user,
        )
    )
    count = await session.scalar(
        tenant_scoped(
            select(func.count())
            .select_from(EwaRequest)
            .where(
                EwaRequest.employee_id == employee_id,
                EwaRequest.period_year == day.year,
                EwaRequest.period_month == day.month,
                EwaRequest.status.in_(_OPEN),
            ),
            EwaRequest,
            user,
        )
    )
    limit = _money(earned * policy.max_percent / Decimal(100))
    available = max(Decimal("0.00"), limit - Decimal(str(drawn or 0)))
    return {
        "enabled": policy.enabled,
        "monthly_gross": str(_money(gross)),
        "days_paid_so_far": str(paid_days),
        "days_in_month": days_in_month,
        "basis": basis,
        "earned_to_date": str(earned),
        "max_percent": str(policy.max_percent),
        "already_drawn": str(_money(Decimal(str(drawn or 0)))),
        "available": str(available),
        "min_amount": str(policy.min_amount),
        "requests_left": max(0, policy.max_requests_per_month - int(count or 0)),
    }


def _read(row: EwaRequest, employee: Employee | None = None) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "employee_id": str(row.employee_id),
        "employee_name": employee.full_name if employee else None,
        "employee_code": employee.employee_code if employee else None,
        "period": f"{row.period_year}-{row.period_month:02d}",
        "amount": str(row.amount),
        "earned_to_date": str(row.earned_to_date),
        "status": row.status.value,
        "decision_note": row.decision_note,
        "disbursed_on": row.disbursed_on.isoformat() if row.disbursed_on else None,
        "payment_reference": row.payment_reference,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def request_advance(
    session: AsyncSession,
    user: User,
    amount: Decimal,
    *,
    today: date | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    permissions.own_employee_id(user)
    day = today or date.today()
    position = await available_for(session, user, user.employee_id, today=day)
    if not position["enabled"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Earned wage access is not switched on for this workspace.",
        )
    if position["requests_left"] <= 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You have used this month's requests.",
        )
    if amount < Decimal(position["min_amount"]):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"The smallest advance is {position['min_amount']}.",
        )
    if amount > Decimal(position["available"]):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"You can draw up to {position['available']} now.",
        )
    row = EwaRequest(
        tenant_id=user.tenant_id,
        employee_id=user.employee_id,
        period_year=day.year,
        period_month=day.month,
        amount=amount,
        earned_to_date=Decimal(position["earned_to_date"]),
        available=Decimal(position["available"]),
        requested_by_user_id=user.id,
    )
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="ewa_request",
        entity_id=row.id,
        actor=user,
        changes={"amount": str(amount), "earned_to_date": position["earned_to_date"]},
        summary=f"Asked for an advance of {amount} on earned pay",
        request=request,
    )
    await session.commit()
    return _read(row)


async def list_requests(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """HR: everyone's, newest first. Anyone else: their own."""
    stmt = select(EwaRequest, Employee).join(Employee, Employee.id == EwaRequest.employee_id)
    if not permissions.is_hr(user):
        permissions.own_employee_id(user)
        stmt = stmt.where(EwaRequest.employee_id == user.employee_id)
    rows = (
        await session.execute(
            tenant_scoped(stmt.order_by(EwaRequest.created_at.desc()).limit(300), EwaRequest, user)
        )
    ).all()
    return [_read(r, e) for r, e in rows]


async def _get(session: AsyncSession, user: User, request_id: uuid.UUID) -> EwaRequest:
    row = await session.scalar(
        tenant_scoped(select(EwaRequest).where(EwaRequest.id == request_id), EwaRequest, user)
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
    return row


async def decide(
    session: AsyncSession,
    user: User,
    request_id: uuid.UUID,
    *,
    approve: bool,
    note: str | None,
    request: Request | None = None,
) -> dict[str, Any]:
    if not permissions.is_admin(user):
        raise permissions.forbidden("Only an admin approves an advance on pay.")
    row = await _get(session, user, request_id)
    if row.employee_id == user.employee_id:
        raise permissions.forbidden("Somebody else approves your own advance.")
    if row.status is not EwaStatus.REQUESTED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already decided.")
    clean = (note or "").strip() or None
    if not approve and clean is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Say why it is declined."
        )
    row.status = EwaStatus.APPROVED if approve else EwaStatus.REJECTED
    row.decided_by_user_id = user.id
    row.decided_at = datetime.now(UTC)
    row.decision_note = clean
    if approve:
        adjustment = PayrollAdjustment(
            tenant_id=user.tenant_id,
            employee_id=row.employee_id,
            kind=AdjustmentKind.DEDUCTION,
            category=AdjustmentCategory.OTHER_DEDUCTION,
            name="Earned wage advance",
            amount=row.amount,
            taxable=False,
            counts_as_wages=False,
            within_net_pay=True,
            recurrence=AdjustmentRecurrence.ONE_TIME,
            start_year=row.period_year,
            start_month=row.period_month,
            reference=f"ewa:{row.id}",
            reason="Advance on pay already earned this month.",
            status=AdjustmentStatus.APPROVED,
            created_by_user_id=row.requested_by_user_id,
            decided_by_user_id=user.id,
            decided_at=datetime.now(UTC),
        )
        session.add(adjustment)
        await session.flush()
        row.payroll_adjustment_id = adjustment.id
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.APPROVE if approve else AuditAction.REJECT,
        entity_type="ewa_request",
        entity_id=row.id,
        actor=user,
        changes={"status": row.status.value, "note": clean},
        summary=f"{'Approved' if approve else 'Declined'} an advance of {row.amount}",
        request=request,
    )
    await session.commit()
    return _read(row)


async def mark_disbursed(
    session: AsyncSession,
    user: User,
    request_id: uuid.UUID,
    *,
    paid_on: date,
    reference: str | None,
    request: Request | None = None,
) -> dict[str, Any]:
    permissions.require_hr(user, detail="Disbursements are recorded by HR.")
    row = await _get(session, user, request_id)
    if row.status is not EwaStatus.APPROVED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Not approved.")
    row.status = EwaStatus.DISBURSED
    row.disbursed_on = paid_on
    row.payment_reference = (reference or "").strip() or None
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="ewa_request",
        entity_id=row.id,
        actor=user,
        changes={"status": "disbursed"},
        summary=f"Recorded the transfer of an advance of {row.amount}",
        request=request,
    )
    await session.commit()
    return _read(row)
