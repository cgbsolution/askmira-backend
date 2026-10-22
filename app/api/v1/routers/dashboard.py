"""Home dashboard aggregation.

One call for the home screen's headline numbers rather than six, so the shell
renders in a single round-trip. FRD 6 budgets 2s p95 for core screens and this
is the screen every user sees first.

Every figure is read from the tables that own it; nothing is estimated. The
pending count is the same number the approvals inbox shows (it is computed by
the inbox), the holidays are the ones that apply to the viewer's location, and
the payroll and filing blocks — HR only — report the latest real run and the
real filing records, or say plainly that there are none. The shoutout count is
the caller's visible feed, not every shoutout in the company: a private
shoutout is not a number anybody else gets to see move.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import CurrentUser, DbSession, require_roles, tenant_scoped
from app.models.employee import Employee, EmploymentStatus
from app.models.filings import FilingRecord, ReturnType
from app.models.leave import LeaveRequest, LeaveRequestStatus
from app.models.payroll import PayrollRun, PayrollRunStatus
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services import quick_links as quick_links_service
from app.services.approvals import service as approvals_service
from app.services.attendance.service import local_today
from app.services.culture import feed
from app.services.leave.holidays import holidays_for_employees

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


class UpcomingHoliday(BaseModel):
    holiday_date: date
    name: str


class LatestPayrollRun(BaseModel):
    id: uuid.UUID
    period_year: int
    period_month: int
    status: str
    employee_count: int
    total_net: Decimal
    payment_date: date | None


class FilingDue(BaseModel):
    """The most recent filing record for one return type, and when the next is due."""

    return_type: ReturnType
    #: The period the next return covers, e.g. "2026-09".
    period: str
    #: generated | filed | rejected | blocked, or null when none exists yet.
    status: str | None
    #: The statutory due date where it is fixed nationally; null where it varies by state.
    due_on: date | None


class DashboardSummary(BaseModel):
    greeting_name: str
    headcount: int
    new_hires_30d: int
    on_leave_today: int
    pending_approvals: int
    shoutouts_this_week: int
    open_tasks: int
    today: date
    upcoming_holidays: list[UpcomingHoliday] = []
    #: HR only; null when no run exists (or for everyone else).
    latest_payroll_run: LatestPayrollRun | None = None
    #: HR only, with payroll switched on.
    filings: list[FilingDue] = []


def _previous_month(today: date) -> date:
    return today.replace(day=1) - timedelta(days=1)


class QuickLinksIn(BaseModel):
    #: Null goes back to the defaults.
    links: list[quick_links_service.QuickLinkIn] | None = Field(default=None, max_length=12)


@router.get("/quick-links")
async def quick_links(user: CurrentUser, session: DbSession) -> dict[str, Any]:
    """The workspace's home-page links."""
    return await quick_links_service.quick_links(session, user)


@router.put("/quick-links", dependencies=[Depends(require_roles(UserRole.ADMIN))])
async def set_quick_links(
    payload: QuickLinksIn, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    return await quick_links_service.set_quick_links(session, user, payload.links, request=request)


@router.get("/summary", response_model=DashboardSummary)
async def summary(user: CurrentUser, session: DbSession) -> DashboardSummary:
    today = await local_today(session, user)
    thirty_days_ago = today - timedelta(days=30)
    week_ago = datetime.now(UTC) - timedelta(days=7)

    headcount = await session.scalar(
        tenant_scoped(
            select(func.count(Employee.id)).where(
                Employee.deleted_at.is_(None),
                Employee.status.in_(
                    [EmploymentStatus.ACTIVE, EmploymentStatus.ON_NOTICE, EmploymentStatus.ON_LEAVE]
                ),
            ),
            Employee,
            user,
        )
    )

    # Joined in the last 30 days — not pre-joiners with a future date.
    new_hires = await session.scalar(
        tenant_scoped(
            select(func.count(Employee.id)).where(
                Employee.deleted_at.is_(None),
                Employee.date_of_joining >= thirty_days_ago,
                Employee.date_of_joining <= today,
            ),
            Employee,
            user,
        )
    )

    # People, not requests: two approved requests covering today are one person.
    on_leave = await session.scalar(
        tenant_scoped(
            select(func.count(func.distinct(LeaveRequest.employee_id))).where(
                LeaveRequest.status == LeaveRequestStatus.APPROVED,
                LeaveRequest.start_date <= today,
                LeaveRequest.end_date >= today,
            ),
            LeaveRequest,
            user,
        )
    )

    # Exactly what the approvals inbox lists for this person, so the two agree.
    pending = (await approvals_service.inbox(session, user)).total

    shoutouts = await feed.count_visible_since(session, user, week_ago)

    upcoming: list[UpcomingHoliday] = []
    if user.employee_id is not None:
        found = await holidays_for_employees(
            session, user, [user.employee_id], today, today + timedelta(days=90)
        )
        upcoming = [
            UpcomingHoliday(holiday_date=day, name=name)
            for day, name in sorted(found.get(user.employee_id, {}).items())
        ][:5]

    latest_run: LatestPayrollRun | None = None
    filings: list[FilingDue] = []
    tenant = await session.get(Tenant, user.tenant_id)
    payroll_on = bool(((tenant.enabled_modules or {}) if tenant else {}).get("payroll"))
    if permissions.is_hr(user) and payroll_on:
        run = await session.scalar(
            tenant_scoped(
                select(PayrollRun)
                .where(PayrollRun.status != PayrollRunStatus.CANCELLED)
                .order_by(
                    PayrollRun.period_year.desc(),
                    PayrollRun.period_month.desc(),
                    PayrollRun.created_at.desc(),
                ),
                PayrollRun,
                user,
            )
        )
        if run is not None:
            latest_run = LatestPayrollRun(
                id=run.id,
                period_year=run.period_year,
                period_month=run.period_month,
                status=run.status.value,
                employee_count=run.employee_count,
                total_net=Decimal(str(run.total_net)),
                payment_date=run.payment_date,
            )
        filings = await _filings_due(session, user, today)

    return DashboardSummary(
        greeting_name=user.full_name.split(" ")[0],
        headcount=headcount or 0,
        new_hires_30d=new_hires or 0,
        on_leave_today=on_leave or 0,
        pending_approvals=pending,
        shoutouts_this_week=shoutouts,
        # No task engine feeds this yet; 0 is honest, a made-up number is not.
        open_tasks=0,
        today=today,
        upcoming_holidays=upcoming,
        latest_payroll_run=latest_run,
        filings=filings,
    )


async def _filings_due(session: AsyncSession, user: User, today: date) -> list[FilingDue]:
    """Where last month's statutory returns stand.

    PF (ECR) and ESIC for a month are due on the 15th of the next month; TDS is
    deposited by the 7th and returned quarterly in 24Q; Professional Tax due
    dates differ by state, so none is claimed.
    """
    last_month = _previous_month(today)
    monthly = f"{last_month.year:04d}-{last_month.month:02d}"
    due_15 = today.replace(day=15) if today.day <= 15 else None
    # The latest quarter that has ended: 24Q is filed for a completed quarter.
    quarter_end = last_month
    while quarter_end.month not in (3, 6, 9, 12):
        quarter_end = quarter_end.replace(day=1) - timedelta(days=1)
    fy_start = quarter_end.year if quarter_end.month >= 4 else quarter_end.year - 1
    fiscal_quarter = {6: 1, 9: 2, 12: 3, 3: 4}[quarter_end.month]
    quarterly = f"{fy_start}-{fy_start + 1}-Q{fiscal_quarter}"

    wanted = [
        (ReturnType.PF_ECR, monthly, due_15),
        (ReturnType.ESIC, monthly, due_15),
        (ReturnType.PROFESSIONAL_TAX, monthly, None),
        (ReturnType.TDS_24Q, quarterly, None),
    ]
    rows = (
        await session.scalars(
            tenant_scoped(
                select(FilingRecord)
                .where(FilingRecord.period.in_([monthly, quarterly]))
                .order_by(FilingRecord.created_at.desc()),
                FilingRecord,
                user,
            )
        )
    ).all()
    latest: dict[tuple[ReturnType, str], FilingRecord] = {}
    for row in rows:
        latest.setdefault((row.return_type, row.period), row)

    result: list[FilingDue] = []
    for return_type, period, due_on in wanted:
        record = latest.get((return_type, period))
        result.append(
            FilingDue(
                return_type=return_type,
                period=period,
                status=record.status.value if record else None,
                due_on=due_on,
            )
        )
    return result
