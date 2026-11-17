"""A fixed-monthly consultant's monthly fees: added from a date, never edited in place.

HR adds a fee from the day it applies; a raise is a new fee from its day. A
payout prices each day of an hours entry's period at the fee in force that day
(`fee_on`) divided by the days in that day's month.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.consultant_fees import ConsultantMonthlyFee
from app.models.employee import Employee
from app.models.user import User
from app.services import audit
from app.services.consultants import is_consultant

__all__ = ["FeeIn", "add_fee", "fee_history", "fee_on", "in_force", "list_fees"]

_HR_ONLY = "Consultant fees are set by HR."


class FeeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    employee_id: uuid.UUID
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    effective_from: date
    reason: str | None = Field(default=None, max_length=500)


def _read(row: ConsultantMonthlyFee) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "employee_id": str(row.employee_id),
        "amount": str(Decimal(str(row.amount)).quantize(Decimal("0.01"))),
        "effective_from": row.effective_from.isoformat(),
        "reason": row.reason,
    }


async def add_fee(
    session: AsyncSession, user: User, data: FeeIn, *, request: Request | None = None
) -> dict[str, Any]:
    """A new monthly fee from a date. Fees are never edited in place."""
    permissions.require_hr(user, detail=_HR_ONLY)
    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == data.employee_id), Employee, user)
    )
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
    if not is_consultant(employee):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A monthly fee is set only for a consultant.",
        )
    taken = await session.scalar(
        tenant_scoped(
            select(ConsultantMonthlyFee.id).where(
                ConsultantMonthlyFee.employee_id == employee.id,
                ConsultantMonthlyFee.effective_from == data.effective_from,
            ),
            ConsultantMonthlyFee,
            user,
        )
    )
    if taken is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A fee already starts that day. Fees are never edited; start the new one "
            "another day.",
        )
    row = ConsultantMonthlyFee(tenant_id=user.tenant_id, **data.model_dump())
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="consultant_monthly_fee",
        entity_id=row.id,
        actor=user,
        changes={
            "employee": employee.employee_code,
            "amount": str(row.amount),
            "from": row.effective_from.isoformat(),
        },
        summary=f"Monthly fee {row.amount} for {employee.employee_code} from {row.effective_from}",
        request=request,
    )
    await session.commit()
    return _read(row)


async def list_fees(
    session: AsyncSession, user: User, employee_id: uuid.UUID
) -> list[dict[str, Any]]:
    """The consultant's fees, latest first."""
    permissions.require_hr(user, detail=_HR_ONLY)
    rows = await session.scalars(
        tenant_scoped(
            select(ConsultantMonthlyFee)
            .where(ConsultantMonthlyFee.employee_id == employee_id)
            .order_by(ConsultantMonthlyFee.effective_from.desc()),
            ConsultantMonthlyFee,
            user,
        )
    )
    return [_read(row) for row in rows.all()]


async def fee_on(
    session: AsyncSession, user: User, employee_id: uuid.UUID, day: date
) -> Decimal | None:
    """The monthly fee in force that day: the latest starting on or before it."""
    amount = await session.scalar(
        tenant_scoped(
            select(ConsultantMonthlyFee.amount)
            .where(
                ConsultantMonthlyFee.employee_id == employee_id,
                ConsultantMonthlyFee.effective_from <= day,
            )
            .order_by(ConsultantMonthlyFee.effective_from.desc())
            .limit(1),
            ConsultantMonthlyFee,
            user,
        )
    )
    return Decimal(str(amount)) if amount is not None else None


async def fee_history(
    session: AsyncSession, user: User, employee_id: uuid.UUID
) -> list[tuple[date, Decimal]]:
    """Every fee the consultant has had, as (from, amount), earliest first: what
    `in_force` reads, so pricing a period takes one query, not one a day."""
    rows = await session.execute(
        tenant_scoped(
            select(ConsultantMonthlyFee.effective_from, ConsultantMonthlyFee.amount)
            .where(ConsultantMonthlyFee.employee_id == employee_id)
            .order_by(ConsultantMonthlyFee.effective_from),
            ConsultantMonthlyFee,
            user,
        )
    )
    return [(start, Decimal(str(amount))) for start, amount in rows.all()]


def in_force(history: list[tuple[date, Decimal]], day: date) -> Decimal | None:
    """The fee in `history` (earliest first) in force that day, as `fee_on` gives it."""
    found: Decimal | None = None
    for start, amount in history:
        if start > day:
            break
        found = amount
    return found
