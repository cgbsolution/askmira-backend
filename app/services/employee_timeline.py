"""One person's history across every module, newest first.

Leave, attendance corrections, lifecycle changes, pay revisions, reviews,
goals, recognition, assets, documents, letters, expenses, loans, helpdesk
tickets, onboarding and exit — each read from its own table, each as it is
recorded there. Nothing here is a second copy: the timeline is assembled on
request, so it can never disagree with the module it came from.

Who reads what follows the modules themselves:

- **HR and the person**: everything.
- **Their line manager**: the working record — leave, attendance, lifecycle,
  reviews, goals, recognition, assets, onboarding, exit. Not pay, loans,
  expenses, documents, letters or helpdesk tickets, which the manager cannot
  read in those modules either.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.appraisal import Appraisal, AppraisalCycle
from app.models.assets import Asset, AssetAssignment
from app.models.attendance import AttendanceRegularisation
from app.models.culture import Shoutout
from app.models.documents import IssuedLetter
from app.models.employee import Employee, EmployeeDocument
from app.models.exit_workflow import ExitRecord
from app.models.expenses import ExpenseClaim
from app.models.goals import Goal
from app.models.helpdesk import HelpdeskTicket
from app.models.leave import LeaveRequest, LeaveType
from app.models.lifecycle import LifecycleEvent
from app.models.loans import LoanAccount
from app.models.onboarding import OnboardingPlan
from app.models.payroll import EmployeeSalary
from app.models.user import User

__all__ = ["MANAGER_MODULES", "MODULES", "timeline"]

MODULES = (
    "leave",
    "attendance",
    "lifecycle",
    "pay",
    "performance",
    "goals",
    "recognition",
    "assets",
    "documents",
    "letters",
    "expenses",
    "loans",
    "helpdesk",
    "onboarding",
    "exit",
)
#: What a line manager reads about their report.
MANAGER_MODULES = frozenset(
    {
        "leave",
        "attendance",
        "lifecycle",
        "performance",
        "goals",
        "recognition",
        "assets",
        "onboarding",
        "exit",
    }
)
MAX_ROWS = 300


def _human(value: Any) -> str:
    text = getattr(value, "value", value)
    return str(text).replace("_", " ")


def _at(value: date | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


async def _rows(session: AsyncSession, stmt) -> list[Any]:
    return list((await session.execute(stmt)).all())


async def timeline(
    session: AsyncSession,
    user: User,
    employee_id: uuid.UUID,
    *,
    modules: set[str] | None = None,
) -> dict[str, Any]:
    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
    )
    if employee is None:
        raise permissions.forbidden("That person is not visible to you.")
    is_self = user.employee_id == employee_id
    if permissions.is_hr(user) or is_self:
        allowed = set(MODULES)
    elif await permissions.is_line_manager_of(session, user, employee_id):
        allowed = set(MANAGER_MODULES)
    else:
        raise permissions.forbidden("Only HR, the person and their manager read this timeline.")
    wanted = allowed & (modules or set(MODULES))

    events: list[dict[str, Any]] = []

    def add(
        at,
        module: str,
        title: str,
        detail: str | None = None,
        status: Any = None,
        link: str | None = None,
    ) -> None:
        if at is None:
            return
        events.append(
            {
                "at": _at(at),
                "module": module,
                "title": title,
                "detail": detail,
                "status": _human(status) if status is not None else None,
                "link": link,
            }
        )

    if "leave" in wanted:
        for leave, leave_type in await _rows(
            session,
            tenant_scoped(
                select(LeaveRequest, LeaveType)
                .join(LeaveType, LeaveType.id == LeaveRequest.leave_type_id)
                .where(LeaveRequest.employee_id == employee_id),
                LeaveRequest,
                user,
            ),
        ):
            span = (
                f"{leave.start_date:%d %b %Y}"
                if leave.start_date == leave.end_date
                else f"{leave.start_date:%d %b} to {leave.end_date:%d %b %Y}"
            )
            add(
                leave.start_date,
                "leave",
                f"{leave_type.name}, {span}",
                f"{leave.total_days} day(s)",
                leave.status,
                "/time",
            )

    if "attendance" in wanted:
        for (reg,) in await _rows(
            session,
            tenant_scoped(
                select(AttendanceRegularisation).where(
                    AttendanceRegularisation.employee_id == employee_id
                ),
                AttendanceRegularisation,
                user,
            ),
        ):
            add(
                reg.attendance_date,
                "attendance",
                f"Attendance request: {_human(reg.kind)}",
                reg.reason,
                reg.status,
                "/time/attendance",
            )

    if "lifecycle" in wanted:
        for (event,) in await _rows(
            session,
            tenant_scoped(
                select(LifecycleEvent).where(LifecycleEvent.employee_id == employee_id),
                LifecycleEvent,
                user,
            ),
        ):
            changes = ", ".join(
                f"{key.replace('_id', '').replace('_', ' ')} → {value}"
                for key, value in sorted((event.new_values or {}).items())
                if not key.endswith("_id")
            )
            add(
                event.effective_from,
                "lifecycle",
                _human(event.event_type).capitalize(),
                changes or event.reason,
                event.status,
            )

    if "pay" in wanted:
        for (salary,) in await _rows(
            session,
            tenant_scoped(
                select(EmployeeSalary).where(EmployeeSalary.employee_id == employee_id),
                EmployeeSalary,
                user,
            ),
        ):
            add(
                salary.effective_from,
                "pay",
                "Salary revision",
                f"Annual CTC {salary.ctc_annual}",
                None,
            )

    if "performance" in wanted:
        for appraisal, cycle in await _rows(
            session,
            tenant_scoped(
                select(Appraisal, AppraisalCycle)
                .join(AppraisalCycle, AppraisalCycle.id == Appraisal.cycle_id)
                .where(Appraisal.employee_id == employee_id),
                Appraisal,
                user,
            ),
        ):
            add(
                appraisal.updated_at or appraisal.created_at,
                "performance",
                f"Review: {cycle.name}",
                f"Rating {appraisal.final_rating}" if appraisal.final_rating else None,
                appraisal.status,
            )

    if "goals" in wanted:
        for (goal,) in await _rows(
            session,
            tenant_scoped(select(Goal).where(Goal.owner_employee_id == employee_id), Goal, user),
        ):
            add(
                goal.start_date or goal.created_at,
                "goals",
                f"Goal: {goal.title}",
                f"Due {goal.due_date:%d %b %Y}",
                goal.status,
                "/performance/goals",
            )

    if "recognition" in wanted:
        for (shout,) in await _rows(
            session,
            tenant_scoped(
                select(Shoutout).where(Shoutout.to_employee_id == employee_id), Shoutout, user
            ),
        ):
            add(
                shout.created_at, "recognition", "Recognised", shout.message[:160], None, "/culture"
            )

    if "assets" in wanted:
        for assignment, asset in await _rows(
            session,
            tenant_scoped(
                select(AssetAssignment, Asset)
                .join(Asset, Asset.id == AssetAssignment.asset_id)
                .where(AssetAssignment.employee_id == employee_id),
                AssetAssignment,
                user,
            ),
        ):
            add(assignment.assigned_at, "assets", f"Assigned {asset.name}", asset.asset_tag)
            if assignment.returned_at:
                add(assignment.returned_at, "assets", f"Returned {asset.name}", asset.asset_tag)

    if "documents" in wanted:
        for (doc,) in await _rows(
            session,
            tenant_scoped(
                select(EmployeeDocument).where(EmployeeDocument.employee_id == employee_id),
                EmployeeDocument,
                user,
            ),
        ):
            add(doc.created_at, "documents", f"Document added: {doc.title}", _human(doc.category))

    if "letters" in wanted:
        for (letter,) in await _rows(
            session,
            tenant_scoped(
                select(IssuedLetter).where(IssuedLetter.employee_id == employee_id),
                IssuedLetter,
                user,
            ),
        ):
            add(
                letter.issued_at or letter.created_at,
                "letters",
                f"Letter: {_human(letter.category)}",
                None,
                letter.status,
            )

    if "expenses" in wanted:
        for (claim,) in await _rows(
            session,
            tenant_scoped(
                select(ExpenseClaim).where(ExpenseClaim.employee_id == employee_id),
                ExpenseClaim,
                user,
            ),
        ):
            add(
                claim.incurred_on,
                "expenses",
                f"Expense: {_human(claim.category_key)}",
                f"₹{claim.amount}",
                claim.status,
                "/expenses",
            )

    if "loans" in wanted:
        for (loan,) in await _rows(
            session,
            tenant_scoped(
                select(LoanAccount).where(LoanAccount.employee_id == employee_id), LoanAccount, user
            ),
        ):
            add(loan.created_at, "loans", "Loan", f"Principal ₹{loan.principal}", loan.status)

    if "helpdesk" in wanted:
        for (ticket,) in await _rows(
            session,
            tenant_scoped(
                select(HelpdeskTicket).where(HelpdeskTicket.employee_id == employee_id),
                HelpdeskTicket,
                user,
            ),
        ):
            add(
                ticket.created_at,
                "helpdesk",
                f"Ticket: {ticket.subject}",
                None,
                ticket.status,
                "/helpdesk",
            )

    if "onboarding" in wanted:
        for (plan,) in await _rows(
            session,
            tenant_scoped(
                select(OnboardingPlan).where(OnboardingPlan.employee_id == employee_id),
                OnboardingPlan,
                user,
            ),
        ):
            add(plan.created_at, "onboarding", "Onboarding plan", None, plan.status)

    if "exit" in wanted:
        for (record,) in await _rows(
            session,
            tenant_scoped(
                select(ExitRecord).where(ExitRecord.employee_id == employee_id), ExitRecord, user
            ),
        ):
            last_day = record.approved_last_working_day or record.requested_last_working_day
            add(
                record.resignation_date,
                "exit",
                f"Exit: {_human(record.exit_type)}",
                f"Last day {last_day:%d %b %Y}" if last_day else None,
                record.status,
            )

    if employee.date_of_joining and "lifecycle" in wanted:
        add(employee.date_of_joining, "lifecycle", "Joined", employee.designation)

    events.sort(key=lambda e: e["at"] or "", reverse=True)
    return {
        "employee_id": str(employee_id),
        "modules": sorted(allowed),
        "events": events[:MAX_ROWS],
        "truncated": len(events) > MAX_ROWS,
    }
