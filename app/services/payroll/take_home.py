"""Take-home: what a salary pays into the bank in a full month.

The Salary master shows it beside each revision and under the grid as HR
types, and letters print it in their annexure. All of them get it here, from
the payroll engine run for one full month with the statutory rules in force on
the salary's date — so PF, ESIC, PT and LWF are worked out exactly as the first
payslip will, not guessed. TDS counts only when HR typed it on the Salary master
(it depends on declarations nobody has made yet).

When the month cannot be worked out (no PF rules set up, say) there is no
figure, only the reason: a take-home with guesses in it is worse than none.
"""

from __future__ import annotations

import calendar
import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee import Employee
from app.models.payroll import ComponentType, EmployeeSalary
from app.models.user import User
from app.schemas.payroll import TakeHomeLineRead, TakeHomeRead
from app.services.documents.indian_numbers import format_inr

__all__ = ["MonthUnavailableError", "one_month", "take_home"]

TWELVE = Decimal(12)
#: The statutory lines payroll works out, as the Salary master names them.
_EMPLOYEE_STATUTORY = {
    "PF_EMPLOYEE": "Provident Fund (employee)",
    "VPF": "Voluntary Provident Fund",
    "ESI_EMPLOYEE": "ESIC (employee)",
    "PT": "Professional Tax",
    "LWF_EMPLOYEE": "Labour Welfare Fund (employee)",
    "TDS": "TDS",
}
_EMPLOYER_STATUTORY = {  # typeable through PF_ER_MANUAL / ESIC_ER_MANUAL
    "PF_EMPLOYER": "Provident Fund (employer)",
    "ESI_EMPLOYER": "ESIC (employer)",
    "LWF_EMPLOYER": "Labour Welfare Fund (employer)",
}
_FIRST = ("PF_EMPLOYEE", "VPF", "ESI_EMPLOYEE")
_LAST = ("LWF_EMPLOYEE", "PT", "TDS")
_ORDER = {code: i for i, code in enumerate(_FIRST)} | {
    code: len(_FIRST) + 1 + i for i, code in enumerate(_LAST)
}
_CENT = Decimal("0.01")


class MonthUnavailableError(Exception):
    """The engine could not work the month out; the message says why."""


async def _rule_sets(
    session: AsyncSession, user: User, on: date, state_code: str | None
) -> dict[str, Any]:
    from app.services.payroll.run import _resolve_rule_sets

    resolved = await _resolve_rule_sets(session, user, on, {state_code} if state_code else set())
    rules = {key: value for key, value in resolved.items() if ":" not in key}
    if state_code:
        for kind in ("professional_tax", "labour_welfare_fund"):
            if f"{kind}:{state_code}" in resolved:
                rules[kind] = resolved[f"{kind}:{state_code}"]
    return rules


async def one_month(
    session: AsyncSession, user: User, employee: Employee, salary: EmployeeSalary, as_of: date
):
    """The payroll engine's computation of `salary` for the whole month `as_of`
    falls in, every day paid, with the rules in force on `as_of`.

    `salary` may be unsaved: the engine reads only its components.
    """
    month, _ = await _month_and_rules(session, user, employee, salary, as_of)
    return month


async def _month_and_rules(
    session: AsyncSession, user: User, employee: Employee, salary: EmployeeSalary, as_of: date
):
    from app.services.payroll import statutory_service
    from app.services.payroll.engine import PayrollInput, PayrollRuleError, compute_payroll
    from app.services.payroll.statutory import StatutoryError

    start = as_of.replace(day=1)
    end = as_of.replace(day=calendar.monthrange(as_of.year, as_of.month)[1])
    days = Decimal((end - start).days + 1)

    work_state = await statutory_service.work_state_for_period(session, user, employee, start, end)
    state = None if work_state.needs_review else work_state.state_code
    rules = await _rule_sets(session, user, as_of, state)
    election = await statutory_service.active_pf_election(session, user, employee.id, as_of)
    try:
        month = compute_payroll(
            PayrollInput(
                employee_salary=salary,
                period_start=start,
                period_end=end,
                working_days=days,
                rule_sets=rules,
                pf_election=election,
                designation=employee.designation,
                gender=employee.gender.value if employee.gender else None,
                state_review=work_state.reason if work_state.needs_review else None,
            )
        )
    except (PayrollRuleError, StatutoryError) as exc:
        raise MonthUnavailableError(str(exc)) from exc
    return month, rules


async def take_home(
    session: AsyncSession, user: User, employee: Employee, salary: EmployeeSalary, as_of: date
) -> TakeHomeRead:
    """Earnings less the employee's deductions, a month and a year."""
    from app.services.documents.annexure import letter_label

    try:
        month, rules = await _month_and_rules(session, user, employee, salary, as_of)
    except MonthUnavailableError as exc:
        return TakeHomeRead(
            note=f"Take-home can't be worked out yet: {exc} Set up Payroll → Statutory rules."
        )

    stored = {c.component_code: c for c in salary.components}

    def annual_of(code: str, monthly: Decimal) -> Decimal:
        """A typed line's own yearly figure; a worked-out one is twelve months."""
        component = stored.get(code)
        if component is not None and Decimal(str(component.monthly_amount)) == monthly:
            return Decimal(str(component.annual_amount))
        return monthly * TWELVE

    def row(code: str, name: str, amount: Decimal, basis: str | None) -> TakeHomeLineRead:
        return TakeHomeLineRead(
            code=code,
            name=name,
            monthly=amount.quantize(_CENT),
            annual=annual_of(code, amount).quantize(_CENT),
            basis=_readable(basis),
        )

    deductions: list[TakeHomeLineRead] = []
    employee_side: dict[str, TakeHomeLineRead] = {}
    employer_side: dict[str, TakeHomeLineRead] = {}
    annual = Decimal(0)
    for line in month.lines:
        amount = Decimal(line.amount)
        if line.component_type is ComponentType.EARNING:
            annual += annual_of(line.code, amount)
        elif line.component_type is ComponentType.DEDUCTION:
            if line.code in _EMPLOYEE_STATUTORY:
                employee_side[line.code] = row(
                    line.code, _EMPLOYEE_STATUTORY[line.code], amount, line.basis
                )
            if amount:
                annual -= annual_of(line.code, amount)
                deductions.append(
                    row(line.code, letter_label(line.code, line.name), amount, line.basis)
                )
        elif (
            line.component_type is ComponentType.EMPLOYER_CONTRIBUTION
            and line.code in _EMPLOYER_STATUTORY
        ):
            employer_side[line.code] = row(
                line.code, _EMPLOYER_STATUTORY[line.code], amount, line.basis
            )
    # TDS is not one of the engine's lines; it is a typed TDS MANUAL here.
    if month.tds:
        annual -= month.tds * TWELVE
        tds = row("TDS", "TDS", month.tds, "Typed in TDS MANUAL")
        deductions.append(tds)
        employee_side["TDS"] = tds
    # As the letter lists them: PF, VPF and ESIC first, PT and TDS last.
    deductions.sort(key=lambda line: _ORDER.get(line.code, len(_FIRST)))

    # The engine reports the base it charged ESI on (nothing when ESI doesn't
    # apply); a full month's ESI wages are the earnings that count toward it.
    esi_wages = sum(
        (
            Decimal(str(c.monthly_amount))
            for c in salary.components
            if c.counts_toward_esi and ComponentType(c.component_type) is ComponentType.EARNING
        ),
        Decimal(0),
    )
    # PF and ESIC always have a row, so "nothing due" reads as a decision, not a gap.
    for side, names in ((employee_side, _EMPLOYEE_STATUTORY), (employer_side, _EMPLOYER_STATUTORY)):
        for code in [c for c in names if c.startswith(("PF_", "ESI_"))]:
            if code not in side:
                side[code] = TakeHomeLineRead(
                    code=code,
                    name=names[code],
                    monthly=Decimal("0.00"),
                    annual=Decimal("0.00"),
                    basis=_not_due(code, rules, esi_wages),
                )

    employee_rows = [employee_side[c] for c in _EMPLOYEE_STATUTORY if c in employee_side]
    employer_rows = [employer_side[c] for c in _EMPLOYER_STATUTORY if c in employer_side]
    return TakeHomeRead(
        monthly=month.net_pay.quantize(_CENT),
        annual=annual.quantize(_CENT),
        deductions=deductions,
        employee=employee_rows,
        employer=employer_rows,
        employee_total=sum((r.monthly for r in employee_rows), Decimal("0.00")),
        employer_total=sum((r.monthly for r in employer_rows), Decimal("0.00")),
        review_flags=list(month.review_flags),
        warnings=_employer_pf_warnings(employee, salary, rules, month),
    )


def _employer_pf_warnings(
    employee: Employee, salary: EmployeeSalary, rules: dict[str, Any], month: Any
) -> list[str]:
    """A typed employer PF below the pension (EPS) share cannot be filed: the PF
    return takes the EPS share first and the EPF share is what is left."""
    from app.services.filings.service import _eps_member

    typed = next((c for c in salary.components if c.component_code == "PF_ER_MANUAL"), None)
    pf = rules.get("pf")
    if typed is None or pf is None or not (month.pf_employee or month.pf_employer):
        return []
    if not _eps_member(employee):
        return []
    try:
        eps_ceiling = Decimal(str(pf.rules["eps_wage_ceiling"]))
        eps_rate = Decimal(str(pf.rules["eps_rate"]))
    except KeyError:
        return []
    eps_share = (min(Decimal(month.pf_wage_base), eps_ceiling) * eps_rate / 100).quantize(
        Decimal(1), rounding=ROUND_HALF_UP
    )
    if month.pf_employer >= eps_share:
        return []
    return [
        f"PF EMPLOYER CONTRIBUTION MANUAL {format_inr(month.pf_employer)} is below the pension "
        f"(EPS) share of {format_inr(eps_share)} ({eps_rate}% of "
        f"{format_inr(min(Decimal(month.pf_wage_base), eps_ceiling))}), so the PF return "
        f"could not be filed. Type at least {format_inr(eps_share)}, or leave it empty for "
        "the PF rules' figure."
    ]


_NUMBER = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w%.])")


def _readable(basis: str | None) -> str | None:
    """The engine's basis line as HR reads it: rupees grouped, "typed" said plainly."""
    if not basis:
        return None
    if basis == "manual amount from the salary master":
        return "Typed on the Salary master (MANUAL row)"
    text = _NUMBER.sub(lambda m: format_inr(m.group(1)).removesuffix(".00"), basis)
    return text[:1].upper() + text[1:]


def _not_due(code: str, rules: dict[str, Any], esi_wages: Decimal) -> str:
    """Why a PF or ESIC row is at nothing, as precisely as the rules can say."""
    if code.startswith("PF_"):
        if "pf" not in rules:
            return "Nothing due: no PF rules set up (Payroll → Statutory rules)"
        return "Nothing due: no PF wages this month"
    esi = rules.get("esi")
    if esi is None:
        return "Nothing due: no ESI rules set up (Payroll → Statutory rules)"
    ceiling = esi.rules.get("wage_threshold")
    if ceiling is not None and esi_wages > Decimal(str(ceiling)):
        return (
            f"Nothing due: ESI wages {format_inr(esi_wages).removesuffix('.00')} are above "
            f"the ESI ceiling of {format_inr(ceiling).removesuffix('.00')}"
        )
    return "Nothing due this month"
