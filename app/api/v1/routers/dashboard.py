"""Home dashboard aggregation.

One call for the home screen's headline numbers rather than six, so the shell
renders in a single round-trip. FRD 6 budgets 2s p95 for core screens and this
is the screen every user sees first.

Counts are real where the tables exist; the widgets that depend on modules not
built (tasks) report zero rather than inventing a number. The shoutout count is
the caller's visible feed, not every shoutout in the company - a private
shoutout is not a number anybody else gets to see move.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import func, select

from app.core.deps import CurrentUser, DbSession, tenant_scoped
from app.models.employee import Employee, EmploymentStatus
from app.models.leave import LeaveRequest, LeaveRequestStatus
from app.services.culture import feed
from app.services.leave import service as leave_service

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


class DashboardSummary(BaseModel):
    greeting_name: str
    headcount: int
    new_hires_30d: int
    on_leave_today: int
    pending_approvals: int
    shoutouts_this_week: int
    open_tasks: int


@router.get("/summary", response_model=DashboardSummary)
async def summary(user: CurrentUser, session: DbSession) -> DashboardSummary:
    today = date.today()
    thirty_days_ago = today - timedelta(days=30)
    week_ago = datetime.now(UTC) - timedelta(days=7)

    headcount = await session.scalar(
        tenant_scoped(
            select(func.count(Employee.id)).where(
                Employee.deleted_at.is_(None),
                Employee.status == EmploymentStatus.ACTIVE,
            ),
            Employee,
            user,
        )
    )

    new_hires = await session.scalar(
        tenant_scoped(
            select(func.count(Employee.id)).where(
                Employee.deleted_at.is_(None),
                Employee.date_of_joining >= thirty_days_ago,
            ),
            Employee,
            user,
        )
    )

    on_leave = await session.scalar(
        tenant_scoped(
            select(func.count(LeaveRequest.id)).where(
                LeaveRequest.status == LeaveRequestStatus.APPROVED,
                LeaveRequest.start_date <= today,
                LeaveRequest.end_date >= today,
            ),
            LeaveRequest,
            user,
        )
    )

    # Requests waiting on *this* person, not every pending request in the
    # company: "3 requests waiting on you" has to be true for whoever reads it.
    pending = await leave_service.pending_for(session, user)

    shoutouts = await feed.count_visible_since(session, user, week_ago)

    return DashboardSummary(
        greeting_name=user.full_name.split(" ")[0],
        headcount=headcount or 0,
        new_hires_30d=new_hires or 0,
        on_leave_today=on_leave or 0,
        pending_approvals=pending or 0,
        shoutouts_this_week=shoutouts,
        # Tasks and workflows are not built; reporting 0 is honest, a fake number is not.
        open_tasks=0,
    )
