"""Arrear recomputation: what was paid against what should have been.

═══════════════════════════════════════════════════════════════════════════════
THE MISTAKE THIS MODULE EXISTS TO AVOID
═══════════════════════════════════════════════════════════════════════════════

Recomputing a closed period with **today's** rule set.

It is the natural implementation — you have a salary, you have an engine, you
call it — and it is wrong in a way that is almost invisible. If the PF ceiling
moved in July, recomputing March against the current ceiling produces a March PF
figure that could never have been deducted in March. The arrear is then wrong,
the revised return is wrong, and nothing in the output says so.

So `recompute_period` takes the rule sets **as an argument**, and the caller
resolves them by the closed run's own `effective_date`. There is no code path
here that can reach for a current rule.

The second thing this module does not do: touch the closed run. It reads
`Payslip.line_items` — a snapshot written when the run was computed — and
returns a diff. Nothing in this file writes to a payslip or a run.
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.models.payroll import ComponentType, EmployeeSalary, PayrollRuleSet
from app.services.payroll.engine import PayrollInput, compute_payroll

__all__ = [
    "ArrearError",
    "ComponentDiff",
    "PeriodArrear",
    "recompute_period",
    "summarise",
]

ZERO = Decimal(0)


class ArrearError(ValueError):
    """A period cannot be recomputed from what is available."""


@dataclass(frozen=True, slots=True)
class ComponentDiff:
    code: str
    name: str
    component_type: str
    paid: Decimal
    should_have_been: Decimal

    @property
    def difference(self) -> Decimal:
        return self.should_have_been - self.paid

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "component_type": self.component_type,
            "paid": str(self.paid),
            "should_have_been": str(self.should_have_been),
            "difference": str(self.difference),
        }


@dataclass(frozen=True, slots=True)
class PeriodArrear:
    """One closed period's worth of difference."""

    period: str
    diffs: list[ComponentDiff] = field(default_factory=list)
    # The rule versions actually used, so "which rules did this use" is
    # answerable without re-deriving it.
    rule_set_ids: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def earnings_arrear(self) -> Decimal:
        return sum(
            (d.difference for d in self.diffs if d.component_type == "earning"),
            start=ZERO,
        )

    @property
    def employee_deduction_arrear(self) -> Decimal:
        """PF, ESI, PT and tax arrears on the employee's side.

        Payable too: a higher salary for March means a higher March PF, and the
        employee owes the difference.
        """
        return sum(
            (d.difference for d in self.diffs if d.component_type == "deduction"),
            start=ZERO,
        )

    @property
    def employer_contribution_arrear(self) -> Decimal:
        """The employer's side, which is owed but never reaches the net.

        Netting this against the employee's arrear understates what the company
        owes and overstates what the employee receives.
        """
        return sum(
            (
                d.difference
                for d in self.diffs
                if d.component_type == "employer_contribution"
            ),
            start=ZERO,
        )

    @property
    def net_payable(self) -> Decimal:
        return self.earnings_arrear - self.employee_deduction_arrear

    @property
    def has_movement(self) -> bool:
        return any(d.difference != ZERO for d in self.diffs)


def recompute_period(
    *,
    period: str,
    paid_line_items: list[dict[str, Any]],
    new_salary: EmployeeSalary,
    working_days: Decimal,
    lop_days: Decimal,
    period_start,
    period_end,
    historical_rule_sets: dict[str, PayrollRuleSet],
    designation: str | None = None,
) -> PeriodArrear:
    """One closed period, recomputed on the new salary.

    Args:
        paid_line_items: `Payslip.line_items` as written when the run was
            computed. Read, never modified.
        historical_rule_sets: **the versions in force during this period**, not
            the current ones. Resolved by the caller against the closed run's
            own `effective_date`; there is no way to reach a current rule from
            inside this function.

    Returns a per-component diff. Every component that moved appears, including
    statutory deductions and employer contributions.
    """
    if not isinstance(working_days, Decimal) or not isinstance(lop_days, Decimal):
        raise ArrearError("Working days and loss-of-pay days must be Decimals")
    if not historical_rule_sets:
        raise ArrearError(
            f"No rule sets were supplied for {period}. Recomputing a closed period "
            "against today's rules would produce a figure that could never have been "
            "deducted then, so this refuses rather than guessing."
        )

    recomputed = compute_payroll(
        PayrollInput(
            employee_salary=new_salary,
            period_start=period_start,
            period_end=period_end,
            working_days=working_days,
            # The same attendance as the original run. An arrear is a pay
            # difference, not a re-adjudication of who turned up in March.
            lop_days=lop_days,
            rule_sets=historical_rule_sets,
            designation=designation,
        )
    )

    paid = {
        str(item.get("code")): (
            Decimal(str(item.get("amount", 0))),
            str(item.get("name", item.get("code"))),
            str(item.get("type", "earning")),
        )
        for item in paid_line_items
    }
    should = {
        line.code: (line.amount, line.name, _type_of(line.component_type))
        for line in recomputed.lines
    }

    diffs: list[ComponentDiff] = []
    notes: list[str] = []

    for code in sorted(set(paid) | set(should)):
        paid_amount, paid_name, paid_type = paid.get(code, (ZERO, code, ""))
        should_amount, should_name, should_type = should.get(code, (ZERO, code, paid_type))

        if code not in paid:
            notes.append(
                f"'{should_name}' did not exist on the original payslip for {period}; "
                "the whole amount is an arrear."
            )
        elif code not in should:
            notes.append(
                f"'{paid_name}' was paid in {period} but the new structure has no such "
                "component. It is recovered in full — check this is intended."
            )

        diffs.append(
            ComponentDiff(
                code=code,
                name=should_name or paid_name,
                component_type=should_type or paid_type,
                paid=paid_amount,
                should_have_been=should_amount,
            )
        )

    return PeriodArrear(
        period=period,
        diffs=diffs,
        rule_set_ids={
            rule_type: str(rule_set.id)
            for rule_type, rule_set in historical_rule_sets.items()
        },
        notes=notes,
    )


def _type_of(component_type: ComponentType) -> str:
    if component_type is ComponentType.DEDUCTION:
        return "deduction"
    if component_type is ComponentType.EMPLOYER_CONTRIBUTION:
        return "employer_contribution"
    return "earning"


@dataclass(frozen=True, slots=True)
class ArrearSummary:
    employee_count: int
    period_count: int
    earnings: Decimal
    employee_deductions: Decimal
    employer_contributions: Decimal
    net_payable: Decimal
    affected_filings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "employee_count": self.employee_count,
            "period_count": self.period_count,
            "earnings": str(self.earnings),
            "employee_deductions": str(self.employee_deductions),
            "employer_contributions": str(self.employer_contributions),
            "net_payable": str(self.net_payable),
            "affected_filings": self.affected_filings,
        }


# Which already-filed return each statutory head appears in. The arrear does
# not amend anything — it says what the payroll team now has to decide about.
_FILING_BY_COMPONENT = {
    "PF_EMPLOYEE": "PF ECR",
    "PF_EMPLOYER": "PF ECR",
    "VPF": "PF ECR",
    "ESI_EMPLOYEE": "ESIC return",
    "ESI_EMPLOYER": "ESIC return",
    "PT": "Professional Tax challan",
    "LWF_EMPLOYEE": "Labour Welfare Fund return",
    "LWF_EMPLOYER": "Labour Welfare Fund return",
    "TDS": "TDS 24Q",
}


def summarise(
    per_employee: dict[str, list[PeriodArrear]],
) -> ArrearSummary:
    """Totals across a batch, and which filings the movement touches.

    The filings list is a warning, not an action. A revised ECR for a closed
    month is a decision with consequences — sometimes the right answer is to
    carry the difference forward instead — and payroll makes it, not this code.
    """
    earnings = ZERO
    deductions = ZERO
    employer = ZERO
    periods: set[str] = set()
    filings: set[str] = set()

    for arrears in per_employee.values():
        for arrear in arrears:
            if not arrear.has_movement:
                continue
            periods.add(arrear.period)
            earnings += arrear.earnings_arrear
            deductions += arrear.employee_deduction_arrear
            employer += arrear.employer_contribution_arrear

            for diff in arrear.diffs:
                if diff.difference == ZERO:
                    continue
                filing = _FILING_BY_COMPONENT.get(diff.code)
                if filing:
                    filings.add(f"{filing} for {arrear.period}")

    return ArrearSummary(
        employee_count=len(per_employee),
        period_count=len(periods),
        earnings=earnings,
        employee_deductions=deductions,
        employer_contributions=employer,
        net_payable=earnings - deductions,
        affected_filings=sorted(filings),
    )
