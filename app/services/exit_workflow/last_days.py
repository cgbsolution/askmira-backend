"""When somebody's employment ends: one answer for payroll and the directory.

`Employee.date_of_exit` is written only when access is revoked, which is
usually after the last payroll that should pay them; until then the exit
record's approved last working day is the date employment ends. Payroll pays up
to that day, and a bulk move between companies leaves anybody past it alone, so
both read it from here.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.exit_workflow import ExitRecord, ExitStatus
from app.models.user import User

__all__ = ["agreed_last_days"]


async def agreed_last_days(
    session: AsyncSession, user: User, employee_ids: frozenset[uuid.UUID] | set[uuid.UUID]
) -> dict[uuid.UUID, date]:
    """The agreed last working day of everyone with an open exit."""
    if not employee_ids:
        return {}
    rows = (
        await session.execute(
            tenant_scoped(
                select(ExitRecord.employee_id, ExitRecord.approved_last_working_day)
                .where(
                    ExitRecord.employee_id.in_(employee_ids),
                    ExitRecord.status != ExitStatus.CANCELLED,
                    ExitRecord.approved_last_working_day.is_not(None),
                )
                .order_by(ExitRecord.created_at),
                ExitRecord,
                user,
            )
        )
    ).all()
    # The latest exit wins, should there ever be more than one open.
    return {employee_id: last_day for employee_id, last_day in rows}
