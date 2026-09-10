"""Payroll computation for one employee for one period.

Two corrections this module exists to make, both from slice 01:

1. **PF is computed on BASIC + DA, not on gross.** The earlier implementation
   used gross, which over-deducts for every employee whose structure has
   allowances outside the PF wage base. The wage base is now the sum of
   components flagged `counts_toward_pf` on the *materialised* employee salary,
   so a later edit to a component definition cannot move a past run's base.

2. **LOP proration happens before statutory computation, not after.** An
   employee with loss-of-pay days has a lower PF and ESI wage base that month;
   computing statutory deductions on the full amount and then prorating the net
   produces a different — and wrong — number.

No statutory rate, slab or threshold appears in this file. Every one of them is
read from an effective-dated `PayrollRuleSet` supplied and approved by the
Compliance SME. A rule set without approval is refused.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from app.models.payroll import ComponentType, EmployeeSalary, PayrollRuleSet
from app.services.payroll.statutory import (
    PfElection,
    StatutoryError,
    compute_lwf,
    compute_vpf,
    resolve_pf_base,
    should_continue_esi,
)

__all__ = [
    "PayrollComputation",
    "PayrollInput",
    "PayrollRuleError",
    "compute_payroll",
]

_RUPEE = Decimal("0.01")


class PayrollRuleError(ValueError):
    """A rule set is missing, unapproved, or malformed for its rule type."""


def _money(value: Decimal) -> Decimal:
    return value.quantize(_RUPEE, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class PayrollInput:
    """Everything one employee's computation needs.

    `working_days` and `lop_days` come from attendance (slice 02). Until that
    lands, callers pass the period's calendar working days and zero LOP.
    """

    employee_salary: EmployeeSalary
    period_start: date
    period_end: date
    working_days: Decimal
    lop_days: Decimal = Decimal(0)
    # {rule_type: PayrollRuleSet} already resolved for the run's effective date.
    rule_sets: dict[str, PayrollRuleSet] = field(default_factory=dict)

    # ── Slice 09 inputs ─────────────────────────────────────────────────────
    # The employee's own PF choices: ceiling-versus-actual-wage, and VPF.
    pf_election: PfElection | None = None
    # Whether this employee was contributing to ESI when the current
    # contribution period opened. `None` means "no record", which the engine
    # treats as "decide from this month's wages" — correct for a first month
    # and for a tenant not yet tracking coverage, wrong for a mid-period
    # crossing, which is why `run.py` records coverage when a period opens.
    esi_contributing_at_period_start: bool | None = None
    # Designation, for LWF exemptions that are expressed by designation.
    designation: str | None = None
    # Where the employee's work state could not be resolved for the whole
    # period, the reason. Both state levies — Professional Tax and the Labour
    # Welfare Fund — are then left undeducted and flagged rather than guessed.
    state_review: str | None = None


@dataclass(frozen=True, slots=True)
class ComputedLine:
    code: str
    name: str
    component_type: ComponentType
    amount: Decimal
    basis: str
    rule_set_id: str | None = None


@dataclass(frozen=True, slots=True)
class PayrollComputation:
    working_days: Decimal
    paid_days: Decimal
    lop_days: Decimal

    gross_earnings: Decimal
    total_deductions: Decimal
    net_pay: Decimal
    employer_cost: Decimal

    pf_wage_base: Decimal
    esi_wage_base: Decimal
    pf_employee: Decimal
    pf_employer: Decimal
    esi_employee: Decimal
    esi_employer: Decimal
    professional_tax: Decimal
    tds: Decimal

    lwf_employee: Decimal = Decimal(0)
    lwf_employer: Decimal = Decimal(0)
    vpf: Decimal = Decimal(0)

    lines: list[ComputedLine] = field(default_factory=list)
    # Things payroll declined to decide alone. A non-empty list does not stop
    # the computation; it stops the run being approved without a look.
    review_flags: list[str] = field(default_factory=list)


def _require_rule_set(rule_sets: dict[str, PayrollRuleSet], rule_type: str) -> PayrollRuleSet:
    rule_set = rule_sets.get(rule_type)
    if rule_set is None:
        raise PayrollRuleError(
            f"No {rule_type.upper()} rule set is active for this run's effective date. "
            "Payroll cannot compute a statutory deduction without one."
        )
    if not rule_set.is_approved:
        raise PayrollRuleError(
            f"The {rule_type.upper()} rule set effective {rule_set.effective_from} has not "
            "been approved by a Compliance SME. Payroll will not compute against it."
        )
    return rule_set


def _rule(rule_set: PayrollRuleSet, key: str) -> Decimal:
    """Read one numeric value out of a rule set, or fail with a usable message."""
    if key not in rule_set.rules:
        raise PayrollRuleError(
            f"The {rule_set.rule_type} rule set effective {rule_set.effective_from} is "
            f"missing '{key}'. Ask the Compliance SME to supply it."
        )
    return Decimal(str(rule_set.rules[key]))


@contextmanager
def _statutory_errors() -> Iterator[None]:
    """Surface a statutory configuration problem as a payroll rule error.

    `run.py` catches `PayrollRuleError` per employee and records it against
    that employee rather than failing the whole run. A `StatutoryError`
    escaping uncaught would abort the run for everyone, which is the wrong
    blast radius for one badly configured state.
    """
    try:
        yield
    except StatutoryError as exc:
        raise PayrollRuleError(str(exc)) from exc


def _esi_applies(
    *,
    wage_base: Decimal,
    threshold: Decimal,
    month: int,
    rules: dict,
    contributing_at_period_start: bool | None,
) -> tuple[bool, str]:
    """Whether ESI is deducted this month.

    With a recorded coverage state, the contribution-period rule applies: an
    employee who crosses the ceiling mid-period keeps contributing until the
    period ends. Without one — a first month, or a tenant that has not started
    tracking coverage — the decision falls back to this month's wages, which is
    right for a period boundary and is all that can honestly be inferred.
    """
    if contributing_at_period_start is None or "contribution_periods" not in rules:
        within = wage_base <= threshold
        return within, (
            f"Wages of {wage_base} are within the ESI ceiling of {threshold}."
            if within
            else f"Wages of {wage_base} are above the ESI ceiling of {threshold}."
        )

    with _statutory_errors():
        return should_continue_esi(
            current_wages=wage_base,
            threshold=threshold,
            month=month,
            config=rules,
            was_contributing_at_period_start=contributing_at_period_start,
        )


def compute_payroll(payroll_input: PayrollInput) -> PayrollComputation:
    """Compute one employee's pay for one period.

    Order matters and is deliberate: prorate first, derive the statutory wage
    bases from the prorated amounts, then apply the rule sets.
    """
    salary = payroll_input.employee_salary
    working_days = payroll_input.working_days
    lop_days = payroll_input.lop_days

    if working_days <= 0:
        raise PayrollRuleError("Working days must be greater than zero")
    if lop_days < 0:
        raise PayrollRuleError("Loss-of-pay days cannot be negative")
    if lop_days > working_days:
        raise PayrollRuleError(
            f"Loss-of-pay days ({lop_days}) exceed working days ({working_days})"
        )

    paid_days = working_days - lop_days
    lop_factor = paid_days / working_days

    # ── 1. Prorate ──────────────────────────────────────────────────────────
    lines: list[ComputedLine] = []
    prorated: dict[str, Decimal] = {}

    for component in sorted(salary.components, key=lambda c: c.display_order):
        full = Decimal(str(component.monthly_amount))
        if component.pro_rate_on_lop and lop_days > 0:
            amount = _money(full * lop_factor)
            basis = f"structure, prorated {paid_days}/{working_days} days"
        else:
            amount = _money(full)
            basis = "structure"

        prorated[component.component_code] = amount
        lines.append(
            ComputedLine(
                code=component.component_code,
                name=component.component_name,
                component_type=component.component_type,
                amount=amount,
                basis=basis,
            )
        )

    gross_earnings = sum(
        (
            line.amount
            for line in lines
            if line.component_type is ComponentType.EARNING
        ),
        start=Decimal(0),
    )

    # ── 2. Statutory wage bases, from the prorated amounts ──────────────────
    # PF on BASIC + DA (whatever the tenant has flagged), never on gross.
    pf_wage_base = sum(
        (
            prorated[c.component_code]
            for c in salary.components
            if c.counts_toward_pf
        ),
        start=Decimal(0),
    )
    esi_wage_base = sum(
        (
            prorated[c.component_code]
            for c in salary.components
            if c.counts_toward_esi
        ),
        start=Decimal(0),
    )

    if pf_wage_base == 0 and any(c.counts_toward_pf for c in salary.components):
        # Flagged components exist but all resolved to zero — worth surfacing
        # rather than silently deducting nothing.
        raise PayrollRuleError(
            "The PF wage base resolved to zero despite components being flagged for it. "
            "Check the salary structure."
        )

    # ── 3. Provident Fund ───────────────────────────────────────────────────
    pf_rules = _require_rule_set(payroll_input.rule_sets, "pf")
    pf_ceiling = _rule(pf_rules, "wage_ceiling")
    pf_employee_rate = _rule(pf_rules, "employee_rate")
    pf_employer_rate = _rule(pf_rules, "employer_rate")

    # A tenant may opt out of the ceiling at the rule-set level, and an
    # individual employee may elect PF on actual wages. Both are configuration.
    with _statutory_errors():
        resolved_base = resolve_pf_base(
            wage_base=pf_wage_base,
            ceiling=pf_ceiling,
            ceiling_applies_by_default=bool(pf_rules.rules.get("apply_wage_ceiling", True)),
            election=payroll_input.pf_election,
        )
    pf_base = resolved_base.base

    pf_employee = _money(pf_base * pf_employee_rate / Decimal(100))
    pf_employer = _money(pf_base * pf_employer_rate / Decimal(100))

    # Voluntary PF is an *additional employee* contribution on the full wage
    # base. It never moves the employer's — treating it as a matched
    # contribution would silently inflate employer cost.
    with _statutory_errors():
        vpf = compute_vpf(pf_wage_base=pf_wage_base, election=payroll_input.pf_election)

    # ── 4. ESI, only below the eligibility threshold ────────────────────────
    esi_employee = Decimal(0)
    esi_employer = Decimal(0)
    esi_rules = payroll_input.rule_sets.get("esi")
    if esi_rules is not None:
        if not esi_rules.is_approved:
            raise PayrollRuleError(
                f"The ESI rule set effective {esi_rules.effective_from} has not been "
                "approved by a Compliance SME."
            )
        threshold = _rule(esi_rules, "wage_threshold")
        applies, esi_reason = _esi_applies(
            wage_base=esi_wage_base,
            threshold=threshold,
            month=payroll_input.period_start.month,
            rules=esi_rules.rules,
            contributing_at_period_start=payroll_input.esi_contributing_at_period_start,
        )
        if applies:
            esi_employee = _money(
                esi_wage_base * _rule(esi_rules, "employee_rate") / Decimal(100)
            )
            esi_employer = _money(
                esi_wage_base * _rule(esi_rules, "employer_rate") / Decimal(100)
            )

    # ── 5. Professional Tax — a state slab, looked up by gross ──────────────
    #
    # PT is a state levy: different slabs, different returns, different filing
    # deadlines. Where the state cannot be established — an employee who moved
    # mid-period, or one with no work location — payroll flags it for a human
    # rather than picking a state. A wrong guess here files in the wrong state,
    # and nobody notices until an inspection.
    professional_tax = Decimal(0)
    review_flags: list[str] = []
    pt_rules = payroll_input.rule_sets.get("professional_tax")

    if payroll_input.state_review:
        review_flags.append(payroll_input.state_review)
        pt_rules = None
    elif pt_rules is not None:
        if not pt_rules.is_approved:
            raise PayrollRuleError(
                f"The Professional Tax rule set for {pt_rules.jurisdiction} effective "
                f"{pt_rules.effective_from} has not been approved by a Compliance SME."
            )
        professional_tax = _lookup_slab(pt_rules, gross_earnings)

    # ── 6. Labour Welfare Fund ──────────────────────────────────────────────
    #
    # A periodic state levy, not a monthly one. In a month the state does not
    # collect it, this is exactly zero — never a twelfth of the annual amount.
    lwf_employee = Decimal(0)
    lwf_employer = Decimal(0)
    lwf_rules = payroll_input.rule_sets.get("labour_welfare_fund")
    if lwf_rules is not None:
        if not lwf_rules.is_approved:
            raise PayrollRuleError(
                f"The Labour Welfare Fund rule set for {lwf_rules.jurisdiction} effective "
                f"{lwf_rules.effective_from} has not been approved by a Compliance SME."
            )
        with _statutory_errors():
            lwf = compute_lwf(
                config=lwf_rules.rules,
                month=payroll_input.period_start.month,
                monthly_wages=gross_earnings,
                designation=payroll_input.designation,
            )
        lwf_employee = lwf.employee_contribution
        lwf_employer = lwf.employer_contribution

    # ── 7. TDS ──────────────────────────────────────────────────────────────
    # Income tax is an annual projection against declared investments, which is
    # slice 07. Until then TDS is zero rather than a guess — an approximate
    # deduction from someone's salary is worse than none.
    tds = Decimal(0)

    total_deductions = (
        pf_employee + vpf + esi_employee + professional_tax + lwf_employee + tds
    )
    total_deductions += sum(
        (
            line.amount
            for line in lines
            if line.component_type is ComponentType.DEDUCTION
        ),
        start=Decimal(0),
    )

    employer_contributions = sum(
        (
            line.amount
            for line in lines
            if line.component_type is ComponentType.EMPLOYER_CONTRIBUTION
        ),
        start=Decimal(0),
    )

    lines.extend(
        [
            ComputedLine("PF_EMPLOYEE", "Provident Fund (employee)", ComponentType.DEDUCTION,
                         pf_employee, f"{pf_employee_rate}% of PF wage base {pf_base}",
                         str(pf_rules.id)),
            ComputedLine("PF_EMPLOYER", "Provident Fund (employer)",
                         ComponentType.EMPLOYER_CONTRIBUTION, pf_employer,
                         f"{pf_employer_rate}% of PF wage base {pf_base}", str(pf_rules.id)),
        ]
    )
    if esi_rules is not None and (esi_employee or esi_employer):
        lines.extend(
            [
                ComputedLine("ESI_EMPLOYEE", "ESI (employee)", ComponentType.DEDUCTION,
                             esi_employee, f"ESI wage base {esi_wage_base}", str(esi_rules.id)),
                ComputedLine("ESI_EMPLOYER", "ESI (employer)",
                             ComponentType.EMPLOYER_CONTRIBUTION, esi_employer,
                             f"ESI wage base {esi_wage_base}", str(esi_rules.id)),
            ]
        )
    if vpf:
        lines.append(
            ComputedLine("VPF", "Voluntary Provident Fund", ComponentType.DEDUCTION, vpf,
                         f"employee election on PF wage base {pf_wage_base}", str(pf_rules.id))
        )
    if lwf_employee or lwf_employer:
        lines.extend(
            [
                ComputedLine("LWF_EMPLOYEE", "Labour Welfare Fund (employee)",
                             ComponentType.DEDUCTION, lwf_employee,
                             f"{lwf_rules.jurisdiction} levy, month "  # type: ignore[union-attr]
                             f"{payroll_input.period_start.month}",
                             str(lwf_rules.id)),  # type: ignore[union-attr]
                ComputedLine("LWF_EMPLOYER", "Labour Welfare Fund (employer)",
                             ComponentType.EMPLOYER_CONTRIBUTION, lwf_employer,
                             f"{lwf_rules.jurisdiction} levy, month "  # type: ignore[union-attr]
                             f"{payroll_input.period_start.month}",
                             str(lwf_rules.id)),  # type: ignore[union-attr]
            ]
        )
    if professional_tax:
        lines.append(
            ComputedLine("PT", "Professional Tax", ComponentType.DEDUCTION, professional_tax,
                         f"{pt_rules.jurisdiction} slab on gross {gross_earnings}",  # type: ignore[union-attr]
                         str(pt_rules.id))  # type: ignore[union-attr]
        )

    return PayrollComputation(
        working_days=working_days,
        paid_days=paid_days,
        lop_days=lop_days,
        gross_earnings=_money(gross_earnings),
        total_deductions=_money(total_deductions),
        net_pay=_money(gross_earnings - total_deductions),
        employer_cost=_money(
            gross_earnings + employer_contributions + pf_employer + esi_employer + lwf_employer
        ),
        pf_wage_base=_money(pf_wage_base),
        esi_wage_base=_money(esi_wage_base),
        pf_employee=pf_employee,
        pf_employer=pf_employer,
        esi_employee=esi_employee,
        esi_employer=esi_employer,
        professional_tax=professional_tax,
        tds=tds,
        lwf_employee=lwf_employee,
        lwf_employer=lwf_employer,
        vpf=vpf,
        lines=lines,
        review_flags=review_flags,
    )


def _lookup_slab(rule_set: PayrollRuleSet, monthly_gross: Decimal) -> Decimal:
    """Find the slab a monthly gross falls into.

    Slabs are supplied by the Compliance SME as
    `[{"up_to": 15000, "amount": 150}, {"up_to": null, "amount": 200}]` —
    ordered, with a null upper bound on the last. Nothing about the values is
    known to this code.
    """
    slabs = rule_set.rules.get("slabs")
    if not isinstance(slabs, list) or not slabs:
        raise PayrollRuleError(
            f"The Professional Tax rule set for {rule_set.jurisdiction} has no slabs"
        )

    for slab in slabs:
        upper = slab.get("up_to")
        if upper is None or monthly_gross <= Decimal(str(upper)):
            return _money(Decimal(str(slab.get("amount", 0))))

    # Reachable only if the last slab has a bound and gross exceeds it, which
    # is a malformed rule set rather than an untaxed employee.
    raise PayrollRuleError(
        f"Monthly gross {monthly_gross} falls outside every Professional Tax slab for "
        f"{rule_set.jurisdiction}. The last slab needs an open upper bound."
    )
