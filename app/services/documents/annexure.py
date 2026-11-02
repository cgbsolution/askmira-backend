"""The salary annexure a letter carries: what a month's pay looks like.

Offer, appointment and promotion letters attach a table: the earnings, the
gross, what comes off it (PF, ESI, PT, TDS, insurance), the take-home, what the
company adds (its PF and ESI, gratuity) and the CTC. HR used to type it in
Excel. Here it is worked out from the salary in force on the letter's as-of
date, and the statutory lines come from the payroll engine itself, run for one
full month with the rules in force then — so the letter says what the first
payslip will.

What is not modelled is not invented: TDS is shown only when HR typed it on
the Salary master (it depends on declarations the letter cannot know), and an
engine failure (no PF rules set up, say) becomes a message naming the reason
rather than a table with guesses in it.
"""

from __future__ import annotations

import calendar
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee import Employee
from app.models.payroll import ComponentType, EmployeeSalary
from app.models.user import User

__all__ = ["build_annexure", "letter_label"]

ZERO = Decimal(0)
TWELVE = Decimal(12)

#: How a wage type reads in a letter, where its master name is a code-like
#: capitalised label ("HOUSE RENT ALLOWANCE", "MPF CONTRIBUTION MANUAL").
_LETTER_LABELS: dict[str, str] = {
    "BASIC": "Basic Salary",
    "HRA": "House Rent Allowance (HRA)",
    "SPECIAL": "Special Allowance",
    "CONVEYANCE": "Conveyance Allowance",
    "TRANSPORT": "Transport Allowance",
    "EDUCATION": "Education Allowance",
    "TELEPHONE": "Telephone Allowance",
    "UNIFORM": "Uniform Allowance",
    "BONUS": "Statutory Bonus",
    "GRATUITY": "Gratuity",
    "GRATUITY_PROVISION": "Gratuity",
    "BONUS_PROVISION": "Bonus Provision",
    "INSURANCE": "Medical Insurance Premium",
    "PT_CTC": "Professional Tax",
    "PF_EMPLOYEE": "Provident Fund",
    "PF_EMPLOYER": "Provident Fund",
    "ESI_EMPLOYEE": "ESIC Contribution",
    "ESI_EMPLOYER": "ESIC Contribution",
    "VPF": "Voluntary Provident Fund",
    "PT": "Professional Tax (PT)",
    "TDS": "TDS",
    "LWF_EMPLOYEE": "Labour Welfare Fund",
    "LWF_EMPLOYER": "Labour Welfare Fund",
}

_SMALL_WORDS = {"of", "and", "or", "the", "for", "to", "in", "on"}


def letter_label(code: str, name: str) -> str:
    """A wage type's name as a letter prints it."""
    if code in _LETTER_LABELS:
        return _LETTER_LABELS[code]
    text = (name or code).strip()
    if text.upper().endswith(" MANUAL"):
        text = text[: -len(" MANUAL")]
    if text.isupper():
        words = text.lower().split()
        text = " ".join(
            word if (i and word in _SMALL_WORDS) else word.capitalize()
            for i, word in enumerate(words)
        )
    return text


def _whole(value: Decimal) -> Decimal:
    return value.quantize(Decimal(1), rounding=ROUND_HALF_UP)


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


async def build_annexure(
    session: AsyncSession,
    user: User,
    employee: Employee,
    salary: EmployeeSalary,
    *,
    as_of: date,
    header: dict[str, Any],
) -> dict[str, Any]:
    """The annexure's rows for one salary, a month worked out as payroll would.

    `header` carries the name, location, designation and the "W.E.F." label
    the table opens with. Returns `{"error": ...}` when the month cannot be
    worked out, so the letter says why instead of printing guesses.
    """
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
        return {
            "error": (
                "The salary annexure could not be worked out for this employee: "
                f"{exc} Set up the payroll rules (Payroll -> Statutory rules) or the "
                "salary first."
            )
        }

    stored = {c.component_code: c for c in salary.components}

    def annual_of(code: str, monthly: Decimal) -> Decimal:
        """A structure line's own annual figure; a statutory one is twelve months."""
        component = stored.get(code)
        if component is not None and Decimal(str(component.monthly_amount)) == monthly:
            return Decimal(str(component.annual_amount))
        return monthly * TWELVE

    earnings: list[tuple[str, Decimal, Decimal]] = []
    employee_lines: dict[str, tuple[str, Decimal, Decimal]] = {}
    employer_lines: dict[str, tuple[str, Decimal, Decimal]] = {}
    for line in month.lines:
        amount = Decimal(line.amount)
        if line.component_type is ComponentType.EARNING:
            if amount:
                earnings.append(
                    (letter_label(line.code, line.name), amount, annual_of(line.code, amount))
                )
        elif line.component_type is ComponentType.DEDUCTION:
            if line.code == "TDS" and not amount:
                continue
            employee_lines[line.code] = (
                letter_label(line.code, line.name),
                amount,
                annual_of(line.code, amount),
            )
        elif line.component_type is ComponentType.EMPLOYER_CONTRIBUTION:
            component = stored.get(line.code)
            if component is not None and not component.part_of_ctc:
                continue
            employer_lines[line.code] = (
                letter_label(line.code, line.name),
                amount,
                annual_of(line.code, amount),
            )

    # The rows a reader expects even at nothing: PF, ESIC, PT and TDS on the
    # employee's side; PF and ESIC on the company's.
    for code in ("PF_EMPLOYEE", "ESI_EMPLOYEE", "PT"):
        employee_lines.setdefault(code, (_LETTER_LABELS[code], ZERO, ZERO))
    if month.tds:
        employee_lines["TDS"] = ("TDS", month.tds, month.tds * TWELVE)
    else:
        employee_lines.setdefault("TDS", ("TDS", ZERO, ZERO))
    for code in ("PF_EMPLOYER", "ESI_EMPLOYER"):
        employer_lines.setdefault(code, (_LETTER_LABELS[code], ZERO, ZERO))

    order = ["PF_EMPLOYEE", "VPF", "ESI_EMPLOYEE"]
    employee_rows = [employee_lines[c] for c in order if c in employee_lines]
    employee_rows += [
        row
        for code, row in employee_lines.items()
        if code not in order and code not in ("PT", "TDS") and row[1]
    ]
    employee_rows += [employee_lines["PT"], employee_lines["TDS"]]

    employer_order = ["PF_EMPLOYER", "ESI_EMPLOYER"]
    employer_rows = [employer_lines[c] for c in employer_order]
    employer_rows += [
        row for code, row in employer_lines.items() if code not in employer_order and row[1]
    ]

    def total(rows: list[tuple[str, Decimal, Decimal]]) -> tuple[Decimal, Decimal]:
        return (
            sum((_whole(r[1]) for r in rows), start=ZERO),
            sum((_whole(r[2]) for r in rows), start=ZERO),
        )

    gross = total(earnings)
    deductions = total(employee_rows)
    employer_total = total(employer_rows)
    ctc_annual = Decimal(str(salary.ctc_annual))

    return {
        **header,
        "effective_from": header.get("effective_from") or salary.effective_from,
        "earnings": earnings,
        "gross": gross,
        "employee": employee_rows,
        "employee_total": deductions,
        "net": (gross[0] - deductions[0], gross[1] - deductions[1]),
        "employer": employer_rows,
        "employer_total": employer_total,
        "ctc": (_whole(ctc_annual / TWELVE), _whole(ctc_annual)),
        "review_flags": list(month.review_flags),
    }
