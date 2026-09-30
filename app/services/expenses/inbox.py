"""The line manager's expense items, for the unified approvals inbox.

Registered in `app.services.approvals.service.SOURCES`. The inbox rule holds:
only items the caller can act on — claims and trips from their own reporting
line waiting at the manager stage — and never their own. HR can stand in from
Expenses → Team approvals, but their inbox is not filled with every team's
claims; finance items reach HR through the finance-stage sources.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.employee import Employee
from app.models.expenses import (
    ExpenseClaim,
    ExpenseClaimStatusEnum,
    TravelRequest,
    TravelRequestStatusEnum,
)
from app.models.user import User

__all__ = ["manager_claim_items", "manager_travel_items"]


def _money(value: Any) -> str:
    return f"₹{Decimal(str(value)):,.2f}"


def _span(start, end) -> str:
    if start == end:
        return f"{start:%d %b %Y}"
    return f"{start:%d %b} to {end:%d %b %Y}"


async def manager_claim_items(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    line = await permissions.reporting_line(session, user)
    line.discard(user.employee_id)  # type: ignore[arg-type]
    if not line:
        return []
    rows = (
        await session.execute(
            tenant_scoped(
                select(ExpenseClaim, Employee)
                .join(Employee, Employee.id == ExpenseClaim.employee_id)
                .where(
                    ExpenseClaim.status == ExpenseClaimStatusEnum.PENDING_MANAGER,
                    ExpenseClaim.employee_id.in_(line),
                )
                .order_by(ExpenseClaim.submitted_at),
                ExpenseClaim,
                user,
            )
        )
    ).all()
    return [
        {
            "id": claim.id,
            "title": (
                f"{claim.category_key.replace('_', ' ').capitalize()} claim, "
                f"{claim.incurred_on:%d %b %Y}"
            ),
            "subject_name": employee.full_name,
            "detail": _money(claim.amount)
            + (" · flagged by policy" if claim.flagged_for_approver else ""),
            "submitted_at": claim.submitted_at or claim.created_at,
        }
        for claim, employee in rows
    ]


async def manager_travel_items(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    line = await permissions.reporting_line(session, user)
    line.discard(user.employee_id)  # type: ignore[arg-type]
    if not line:
        return []
    rows = (
        await session.execute(
            tenant_scoped(
                select(TravelRequest, Employee)
                .join(Employee, Employee.id == TravelRequest.employee_id)
                .where(
                    TravelRequest.status == TravelRequestStatusEnum.PENDING_MANAGER,
                    TravelRequest.employee_id.in_(line),
                )
                .order_by(TravelRequest.created_at),
                TravelRequest,
                user,
            )
        )
    ).all()
    return [
        {
            "id": trip.id,
            "title": f"Travel, {_span(trip.from_date, trip.to_date)}",
            "subject_name": employee.full_name,
            "detail": (trip.purpose or "")[:120],
            "submitted_at": trip.created_at,
            "due_on": trip.from_date,
        }
        for trip, employee in rows
    ]
