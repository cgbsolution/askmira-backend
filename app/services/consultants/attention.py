"""Consultants who need HR's attention when payouts start, and their opening TDS figures.

On release, a consultant who still has a salary, has no hourly rate or has no
timesheet approver is listed so HR can fix them. Salaries are ended from the
list. A consultant timesheet adjustment still pending in payroll is listed too,
so HR can cancel it or let it through once and nothing is paid twice.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.consultant_payouts import (
    ConsultantPayoutLine,
    ConsultantPayoutRun,
    ConsultantTdsOpening,
    PayoutRunStatus,
)
from app.models.contractors import TdsSection
from app.models.employee import ApproverRoute, Employee, EmploymentStatus, EmploymentType
from app.models.payroll import EmployeeSalary
from app.models.payroll_inputs import PayrollAdjustment
from app.models.timesheets import HourlyRate
from app.models.user import User
from app.services import audit
from app.services.consultants import is_consultant, timesheet_pay_waiting_in_payroll

__all__ = ["attention_list", "end_salaries", "pending_adjustments", "set_opening"]

HAS_SALARY = "Has a current salary"
NO_RATE = "No hourly rate"
NO_APPROVER = "No timesheet approver"
NO_GST_RATE = "No GST rate"

_HR_ONLY = "Consultant payouts are run by HR."


def _text(value: Any) -> str:
    return str(Decimal(str(value)).quantize(Decimal("0.01")))


def _current_salary():
    """A salary that has not ended: open-ended, or ending today or later."""
    return or_(EmployeeSalary.effective_to.is_(None), EmployeeSalary.effective_to >= date.today())


async def attention_list(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """Each consultant who needs something fixed, with the reasons."""
    permissions.require_hr(user, detail=_HR_ONLY)
    people = (
        await session.scalars(
            tenant_scoped(
                select(Employee)
                .where(
                    Employee.employment_type == EmploymentType.CONSULTANT,
                    Employee.status != EmploymentStatus.EXITED,
                )
                .order_by(Employee.employee_code),
                Employee,
                user,
            )
        )
    ).all()
    ids = [person.id for person in people]
    salaried = set(
        (
            await session.scalars(
                tenant_scoped(
                    select(EmployeeSalary.employee_id).where(
                        EmployeeSalary.employee_id.in_(ids), _current_salary()
                    ),
                    EmployeeSalary,
                    user,
                )
            )
        ).all()
    )
    rated = set(
        (
            await session.scalars(
                tenant_scoped(
                    select(HourlyRate.employee_id).where(HourlyRate.employee_id.in_(ids)),
                    HourlyRate,
                    user,
                )
            )
        ).all()
    )
    owed = await _tds_left_owing(session, user, people)
    out: list[dict[str, Any]] = []
    for person in people:
        reasons: list[str] = []
        if person.id in salaried:
            reasons.append(HAS_SALARY)
        if person.id not in rated:
            reasons.append(NO_RATE)
        if (person.timesheet_approver_route or ApproverRoute.INTERNAL) is ApproverRoute.CUSTOMER:
            if not (person.customer_contact_email or "").strip():
                reasons.append(NO_APPROVER)
        elif person.internal_approver_user_id is None:
            reasons.append(NO_APPROVER)
        # Records from before payouts may charge GST with no rate yet; the old
        # fixed monthly figure is shown as a guide until HR sets one.
        no_gst_rate = person.gst_applicable and person.gst_rate is None
        if no_gst_rate:
            reasons.append(NO_GST_RATE)
        if person.id in owed:
            reasons.append(owed[person.id])
        if reasons:
            out.append(
                {
                    "employee_id": str(person.id),
                    "code": person.employee_code,
                    "name": person.full_name,
                    "reasons": reasons,
                    "old_gst_amount": (
                        _text(person.gst_amount)
                        if no_gst_rate and person.gst_amount is not None
                        else None
                    ),
                }
            )
    return out


async def _tds_left_owing(
    session: AsyncSession, user: User, people: list[Employee]
) -> dict[uuid.UUID, str]:
    """TDS a payout carried forward that no later payout will take, by consultant.

    A carry is taken only by a payout in the same financial year and section,
    so one left on a consultant's latest line from a closed year, or under a
    section they are no longer taxed under, has to be settled by hand.
    """
    from app.services import tds_non_salary
    from app.services.attendance.service import local_today
    from app.services.consultants.statement import section_name

    if not people:
        return {}
    rows = (
        await session.execute(
            tenant_scoped(
                select(ConsultantPayoutLine, ConsultantPayoutRun.pay_date)
                .join(ConsultantPayoutRun, ConsultantPayoutRun.id == ConsultantPayoutLine.run_id)
                .where(
                    ConsultantPayoutLine.employee_id.in_([person.id for person in people]),
                    ConsultantPayoutLine.excluded.is_(False),
                    ConsultantPayoutRun.status.in_(
                        [PayoutRunStatus.APPROVED, PayoutRunStatus.PAID]
                    ),
                )
                # Latest first, ordered as the carry itself is (tds_carried_in).
                .order_by(
                    ConsultantPayoutLine.employee_id,
                    ConsultantPayoutRun.pay_date.desc(),
                    ConsultantPayoutRun.approved_at.desc().nulls_last(),
                    ConsultantPayoutLine.id.desc(),
                ),
                ConsultantPayoutLine,
                user,
            )
        )
    ).all()
    latest: dict[uuid.UUID, tuple[ConsultantPayoutLine, date]] = {}
    for line, pay_date in rows:
        latest.setdefault(line.employee_id, (line, pay_date))
    this_year, _ = tds_non_salary.fy_bounds(await local_today(session, user))
    sections = {person.id: person.tds_section or TdsSection.S194J_PROFESSIONAL for person in people}
    owed: dict[uuid.UUID, str] = {}
    for employee_id, (line, pay_date) in latest.items():
        carried = Decimal(str(line.tds_carried_out or 0))
        if carried <= 0:
            continue
        year, _ = tds_non_salary.fy_bounds(pay_date)
        if year >= this_year and line.section == sections[employee_id]:
            continue
        owed[employee_id] = (
            f"TDS still owed from an earlier payout: ₹{carried:,.2f} "
            f"({section_name(line.section)}, FY {year.year}-{(year.year + 1) % 100:02d}). "
            "Settle it by hand."
        )
    return owed


async def pending_adjustments(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """Consultant timesheet adjustments payroll may still pay (pending, or approved and
    not yet applied by a run), to cancel or let through."""
    permissions.require_hr(user, detail=_HR_ONLY)
    rows = (
        await session.execute(
            tenant_scoped(
                select(PayrollAdjustment, Employee)
                .join(Employee, Employee.id == PayrollAdjustment.employee_id)
                .where(
                    Employee.employment_type == EmploymentType.CONSULTANT,
                    timesheet_pay_waiting_in_payroll(),
                )
                .order_by(Employee.employee_code, PayrollAdjustment.created_at),
                PayrollAdjustment,
                user,
            )
        )
    ).all()
    return [
        {
            "adjustment_id": str(adjustment.id),
            "employee_code": person.employee_code,
            "name": person.full_name,
            "amount": _text(adjustment.amount),
        }
        for adjustment, person in rows
    ]


async def end_salaries(
    session: AsyncSession,
    user: User,
    employee_ids: list[uuid.UUID],
    *,
    effective_to: date,
    request: Request | None = None,
) -> int:
    """End the current salary of each consultant listed; returns how many salaries ended.

    Only consultants: anybody else's salary is changed from the Salary master.
    """
    permissions.require_hr(user, detail=_HR_ONLY)
    wanted = set(employee_ids)
    people = (
        await session.scalars(
            tenant_scoped(select(Employee).where(Employee.id.in_(wanted)), Employee, user)
        )
    ).all()
    if len(people) != len(wanted):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Consultant not found")
    for person in people:
        if not is_consultant(person):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"{person.full_name} is not a consultant; change their salary in the "
                    "Salary master."
                ),
            )
    salaries = (
        await session.scalars(
            tenant_scoped(
                select(EmployeeSalary).where(
                    EmployeeSalary.employee_id.in_(wanted),
                    or_(
                        EmployeeSalary.effective_to.is_(None),
                        EmployeeSalary.effective_to > effective_to,
                    ),
                ),
                EmployeeSalary,
                user,
            )
        )
    ).all()
    if any(salary.effective_from > effective_to for salary in salaries):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A salary starts after that date. Pick a later last day.",
        )
    for salary in salaries:
        salary.effective_to = effective_to
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="employee_salary",
        entity_id=None,
        actor=user,
        changes={
            "employee_ids": sorted(str(person.id) for person in people),
            "effective_to": effective_to.isoformat(),
            "salaries_ended": len(salaries),
        },
        summary=f"Ended the salary of {len(people)} consultant(s) as they move to payouts",
        request=request,
    )
    await session.commit()
    return len(salaries)


async def set_opening(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    fy_start: date,
    section: TdsSection,
    fees_paid: Decimal,
    taxed_base: Decimal,
    tds_deducted: Decimal,
    request: Request | None = None,
) -> dict[str, Any]:
    """Record what was paid to a consultant earlier in a financial year, before payout runs."""
    permissions.require_hr(user, detail=_HR_ONLY)
    if (fy_start.month, fy_start.day) != (4, 1):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The financial year starts on 1 April.",
        )
    if min(fees_paid, taxed_base, tds_deducted) < 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Amounts cannot be negative."
        )
    if taxed_base > fees_paid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The amount TDS was taken on cannot be more than the fees paid.",
        )
    person = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
    )
    if person is None or not is_consultant(person):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Consultant not found")
    row = await session.scalar(
        tenant_scoped(
            select(ConsultantTdsOpening).where(
                ConsultantTdsOpening.employee_id == employee_id,
                ConsultantTdsOpening.fy_start == fy_start,
                ConsultantTdsOpening.section == section,
            ),
            ConsultantTdsOpening,
            user,
        )
    )
    if row is None:
        row = ConsultantTdsOpening(
            tenant_id=user.tenant_id, employee_id=employee_id, fy_start=fy_start, section=section
        )
        session.add(row)
    row.fees_paid, row.taxed_base, row.tds_deducted = fees_paid, taxed_base, tds_deducted
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="consultant_tds_opening",
        entity_id=row.id,
        actor=user,
        changes={
            "employee_id": str(employee_id),
            "fy_start": fy_start.isoformat(),
            "section": section.value,
            "fees_paid": str(fees_paid),
            "taxed_base": str(taxed_base),
            "tds_deducted": str(tds_deducted),
        },
        summary=f"Set {person.full_name}'s opening TDS for the year from {fy_start:%d %b %Y}",
        request=request,
    )
    await session.commit()
    return {
        "id": str(row.id),
        "employee_id": str(employee_id),
        "fy_start": fy_start.isoformat(),
        "section": section.value,
        "fees_paid": _text(fees_paid),
        "taxed_base": _text(taxed_base),
        "tds_deducted": _text(tds_deducted),
    }
