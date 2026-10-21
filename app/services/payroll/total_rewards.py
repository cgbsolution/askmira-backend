"""The total-rewards statement: everything the company spends on one person.

Read from what is already recorded, never estimated:

- **Annual CTC, by component** — the salary in force today, as materialised
  when it was assigned (`EmployeeSalaryComponent`): earnings, employer
  contributions, and anything paid outside CTC.
- **Paid this financial year** — from the person's payslips in approved and
  paid runs: gross earnings, employer PF and ESI, net pay.
- **Recognition** — the rewards points balance and its rupee value.
- **Benefits** — each group plan the person is enrolled in today: lives
  covered, the company's premium for them, and any top-up the person pays.

Where nothing applies the statement says so rather than leaving a blank that
reads like zero.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.employee import Employee
from app.models.payroll import (
    ComponentType,
    EmployeeSalary,
    PayrollRun,
    PayrollRunStatus,
    Payslip,
)
from app.models.user import User
from app.services.payroll.service import active_salary_on

__all__ = ["statement"]

_COUNTED_RUNS = (PayrollRunStatus.APPROVED, PayrollRunStatus.PAID)


def _fy_bounds(on: date) -> tuple[str, date, date]:
    start_year = on.year if on.month >= 4 else on.year - 1
    return (
        f"{start_year}-{str(start_year + 1)[-2:]}",
        date(start_year, 4, 1),
        date(start_year + 1, 3, 31),
    )


def _money(value: Any) -> str:
    return str(Decimal(str(value or 0)).quantize(Decimal("0.01")))


async def statement(
    session: AsyncSession, user: User, employee_id: uuid.UUID, *, on: date | None = None
) -> dict[str, Any]:
    """The person themselves, or HR."""
    if not (permissions.is_hr(user) or user.employee_id == employee_id):
        raise permissions.forbidden("A total-rewards statement is for the person and HR.")
    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
    )
    if employee is None:
        raise permissions.forbidden("That person is not visible to you.")
    today = on or date.today()
    fy, fy_start, fy_end = _fy_bounds(today)

    salary = await active_salary_on(session, user, employee_id, today)
    components: list[dict[str, Any]] = []
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
        for c in sorted(loaded.components if loaded else [], key=lambda c: c.component_code):
            if c.component_type is ComponentType.DEDUCTION:
                continue
            components.append(
                {
                    "code": c.component_code,
                    "name": c.component_name,
                    "type": c.component_type.value,
                    "annual": _money(c.annual_amount),
                    "monthly": _money(c.monthly_amount),
                    "part_of_ctc": c.part_of_ctc,
                }
            )

    def _sum(kind: str, *, ctc: bool | None = None) -> str:
        return _money(
            sum(
                (
                    Decimal(c["annual"])
                    for c in components
                    if c["type"] == kind and (ctc is None or c["part_of_ctc"] == ctc)
                ),
                start=Decimal(0),
            )
        )

    paid = (
        await session.execute(
            tenant_scoped(
                select(
                    func.coalesce(func.sum(Payslip.gross_earnings), 0),
                    func.coalesce(func.sum(Payslip.pf_employer), 0),
                    func.coalesce(func.sum(Payslip.esi_employer), 0),
                    func.coalesce(func.sum(Payslip.net_pay), 0),
                    func.count(Payslip.id),
                )
                .join(PayrollRun, PayrollRun.id == Payslip.run_id)
                .where(
                    Payslip.employee_id == employee_id,
                    PayrollRun.status.in_(_COUNTED_RUNS),
                    PayrollRun.effective_date >= fy_start,
                    PayrollRun.effective_date <= min(fy_end, today),
                ),
                Payslip,
                user,
            )
        )
    ).one()

    recognition: dict[str, Any] | None = None
    try:
        from app.services.rewards import service as rewards

        found = await rewards.employee_statement(
            session, user, employee_id=employee_id, financial_year=fy, as_of=today
        )
        recognition = {
            "points": found.get("balance_points", 0),
            "value": found.get("balance_rupees"),
        }
    except Exception:
        recognition = None

    from app.models.benefits import BenefitEnrolment, BenefitPlan, EnrolmentStatus

    benefit_rows = (
        await session.execute(
            tenant_scoped(
                select(BenefitEnrolment, BenefitPlan)
                .join(BenefitPlan, BenefitPlan.id == BenefitEnrolment.plan_id)
                .where(
                    BenefitEnrolment.employee_id == employee_id,
                    BenefitEnrolment.status == EnrolmentStatus.ENROLLED,
                    BenefitPlan.policy_start <= today,
                    BenefitPlan.policy_end >= today,
                ),
                BenefitEnrolment,
                user,
            )
        )
    ).all()
    benefit_lines = [
        {
            "plan": plan.name,
            "kind": plan.kind.value,
            "lives": 1 + len(enrolment.members or []),
            "sum_insured": _money(plan.sum_insured),
            "employer_premium": _money(
                Decimal(str(plan.employer_premium_per_member)) * (1 + len(enrolment.members or []))
            ),
            "your_premium": _money(enrolment.employee_premium_annual),
        }
        for enrolment, plan in benefit_rows
    ]

    return {
        "employee_id": str(employee_id),
        "name": employee.full_name,
        "as_of": today.isoformat(),
        "financial_year": fy,
        "ctc": {
            "annual": _money(salary.ctc_annual) if salary else None,
            "effective_from": salary.effective_from.isoformat() if salary else None,
            "earnings": _sum("earning", ctc=True),
            "employer_contributions": _sum("employer_contribution"),
            "reimbursements": _sum("reimbursement"),
            "outside_ctc": _sum("earning", ctc=False),
            "components": components,
        },
        "paid_this_year": {
            "payslips": int(paid[4]),
            "gross_earnings": _money(paid[0]),
            "employer_pf": _money(paid[1]),
            "employer_esi": _money(paid[2]),
            "net_pay": _money(paid[3]),
        },
        "recognition": recognition,
        "benefits": benefit_lines,
        "not_included": (
            []
            if benefit_lines
            else ["No group benefits plan covers this person now, so none is costed here."]
        ),
    }
