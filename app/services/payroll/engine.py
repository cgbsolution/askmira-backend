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

import enum
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Any

from app.models.payroll import (
    STATUTORY_OVERRIDE_CODES,
    ComponentType,
    EmployeeSalary,
    PayrollRuleSet,
)
from app.services.loans.recovery import RecoveryError, plan_recovery
from app.services.payroll.statutory import (
    PfElection,
    StatutoryError,
    compute_lwf,
    compute_vpf,
    resolve_pf_base,
    should_continue_esi,
)
from app.services.payroll.tds import MonthFigures, TdsContext, compute_monthly_tds
from app.services.tax.computation import TaxRuleError

__all__ = [
    "ComputedInputLine",
    "InputKind",
    "InputLine",
    "LoanRecoveryInput",
    "PayrollComputation",
    "PayrollInput",
    "PayrollRuleError",
    "SupplementaryContext",
    "compute_payroll",
    "professional_tax_slab",
]

_RUPEE = Decimal("0.01")
_ZERO = Decimal(0)

# The component codes slice 07's projection treats as HRA and employer NPS.
# Mirrored rather than re-decided, so the TDS a payslip deducts and the
# projection an employee previews read the same components.
_HRA_CODES = frozenset({"HRA"})
_EMPLOYER_NPS_CODES = frozenset({"NPS_ER", "EMPLOYER_NPS"})


class InputKind(str, enum.Enum):
    """How a line from another module lands on a payslip.

    The distinction that matters is between what moves cash and what does not:
    a perquisite is taxable income the employee never receives as money, so it
    raises TDS without touching net pay.
    """

    # Paid in cash. `taxable_amount` says how much of it is taxable.
    EARNING = "earning"
    # Paid in cash, exempt against a bill.
    REIMBURSEMENT = "reimbursement"
    # Always taken.
    DEDUCTION = "deduction"
    # Taken only when it fits inside net pay; skipped — and left unconsumed — otherwise.
    RECOVERY = "recovery"
    # Non-cash taxable value. Raises TDS, never net pay.
    PERQUISITE = "perquisite"
    # Owed by the employer; employer cost only. May be negative (an arrear refund).
    EMPLOYER_CONTRIBUTION = "employer_contribution"


@dataclass(frozen=True, slots=True)
class InputLine:
    """One item another module wants on this payslip."""

    provider: str
    code: str
    name: str
    kind: InputKind
    amount: Decimal
    taxable_amount: Decimal = _ZERO
    basis: str = ""
    # The record consumed: {"type": "reimbursement_claim", "id": "..."}.
    source: dict[str, Any] = field(default_factory=dict)
    # A payment *for work* - overtime, a shift allowance. It raises the ESI
    # contribution and the Professional Tax slab, as wages do; it never counts
    # toward PF wages (basic and DA) or toward the ESI coverage threshold, which
    # ESIC decides on wages excluding overtime.
    counts_as_wages: bool = False


@dataclass(frozen=True, slots=True)
class SupplementaryContext:
    """An off-cycle payslip for somebody the month's regular run already paid.

    Only the input lines are paid. The structure is not paid twice, PF is not
    deducted again - one-off pay is not PF wages, and the month's PF was taken
    on the structure - and the month's levies are topped up rather than
    recomputed:

    - **Professional Tax** is the slab on the month's whole gross (what the
      regular run paid plus these wage lines), less what the regular run already
      deducted. Usually nothing; sometimes the wage lines cross a slab.
    - **ESI** is contributed on these wage lines only if the regular run found
      the employee covered this month.
    - **Labour Welfare Fund** is never taken twice in a month.
    - **TDS** is the extra annual liability these lines create, deducted now -
      the same rule the regular run applies to one-off pay. The recurring share
      for the month was already deducted by the regular run.
    """

    #: Structure gross plus wage lines the regular run paid this month.
    regular_gross: Decimal
    pt_already_deducted: Decimal
    esi_covered: bool


@dataclass(frozen=True, slots=True)
class LoanRecoveryInput:
    """One loan's instalment, recovered against the scheme's net-pay floor."""

    provider: str
    code: str
    name: str
    emi: Decimal
    outstanding_principal: Decimal
    carried_forward: Decimal
    minimum_net_pay: Decimal
    allow_partial: bool
    source: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ComputedInputLine:
    provider: str
    code: str
    name: str
    kind: InputKind
    # What was actually applied to the payslip — zero for a skipped recovery.
    amount: Decimal
    # What the source asked for.
    requested: Decimal
    taxable_amount: Decimal
    basis: str
    source: dict[str, Any]
    skipped: bool = False
    # A loan recovery's full decision (amount, carried forward, reason).
    recovery: dict[str, Any] | None = None
    counts_as_wages: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "code": self.code,
            "name": self.name,
            "kind": self.kind.value,
            "amount": str(self.amount),
            "requested": str(self.requested),
            "taxable_amount": str(self.taxable_amount),
            "basis": self.basis,
            "source": self.source,
            "skipped": self.skipped,
            "recovery": self.recovery,
            "counts_as_wages": self.counts_as_wages,
        }


class PayrollRuleError(ValueError):
    """A rule set is missing, unapproved, or malformed for its rule type."""


def _money(value: Decimal) -> Decimal:
    return value.quantize(_RUPEE, rounding=ROUND_HALF_UP)


_WHOLE_RUPEE = Decimal(1)
#: How each fund rounds a contribution, unless its rule set says otherwise.
#: EPFO's ECR and ESIC's return both carry whole rupees: EPF rounds to the
#: nearest rupee, ESIC up to the next one. A payslip that deducted paise would
#: never agree with what is remitted, and the payroll ledger would not reconcile
#: to the challan.
_CONTRIBUTION_ROUNDING = {
    "nearest_rupee": ROUND_HALF_UP,
    "next_rupee": ROUND_CEILING,
}


def _reported_wage(rule_set: PayrollRuleSet, base: Decimal, default_rounding: str) -> str:
    """The wage a contribution was charged on, as the payslip's basis line shows it."""
    if str(rule_set.rules.get("contribution_rounding", default_rounding)) == "paise":
        return str(base)
    whole = base.quantize(_WHOLE_RUPEE, rounding=ROUND_HALF_UP)
    return str(whole) if whole == base else f"{whole} ({base} to the rupee)"


def _contribution(
    rule_set: PayrollRuleSet, base: Decimal, rate: Decimal, default_rounding: str
) -> Decimal:
    """A statutory contribution on a wage, rounded the way the fund collects it.

    `contribution_rounding` on the rule set is "nearest_rupee", "next_rupee" or
    "paise" (no rounding beyond paise). The wage is taken in whole rupees first,
    as the return reports it, so the contribution is the one the portal computes.
    """
    rounding = str(rule_set.rules.get("contribution_rounding", default_rounding))
    if rounding == "paise":
        return _money(base * rate / Decimal(100))
    mode = _CONTRIBUTION_ROUNDING.get(rounding)
    if mode is None:
        raise PayrollRuleError(
            f"The {rule_set.rule_type} rule set's contribution_rounding '{rounding}' is not "
            "one of nearest_rupee, next_rupee or paise."
        )
    wage = base.quantize(_WHOLE_RUPEE, rounding=ROUND_HALF_UP)
    return (wage * rate / Decimal(100)).quantize(_WHOLE_RUPEE, rounding=mode).quantize(_RUPEE)


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
    # Days of the period before the employee joined or after they left. They
    # are unpaid exactly as loss of pay is — the components the tenant prorates
    # for LOP are prorated for them too — but reported apart, because they are
    # not an absence and an employee reading "14 days loss of pay" in their
    # joining month would rightly ask what they missed.
    not_employed_days: Decimal = Decimal(0)
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
    # Gender, for a state whose Professional Tax slabs differ for women
    # (`slabs_by_gender` on the rule set). Unknown reads the general slabs.
    gender: str | None = None
    # Where the employee's work state could not be resolved for the whole
    # period, the reason. Both state levies — Professional Tax and the Labour
    # Welfare Fund — are then left undeducted and flagged rather than guessed.
    state_review: str | None = None

    # ── Inputs from other modules, and TDS ──────────────────────────────────
    # Loan EMIs, reimbursements, arrears, referral bonuses and the like, as
    # gathered by `services/payroll/inputs.py`. Empty for a caller that only
    # wants the structure recomputed (the arrears engine).
    inputs: tuple[InputLine, ...] = ()
    loan_recoveries: tuple[LoanRecoveryInput, ...] = ()
    # The employee's year for the income tax projection. `None` leaves TDS at
    # zero — right for a structure-only recomputation, and the run refuses to be
    # approved without it (see `run.py`).
    tds: TdsContext | None = None
    # Set for an off-cycle payslip of somebody already paid by this month's
    # regular run: pays the input lines only. See `SupplementaryContext`.
    supplementary: SupplementaryContext | None = None


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
    #: Set when the deductions that cannot wait pass the legal ceiling alone.
    over_deduction_ceiling: dict[str, str] | None = None

    # Cash earnings from input lines (arrears, encashment, bonus, referral,
    # reimbursements). Deliberately **not** part of `gross_earnings`, which stays
    # the structure's earnings: the PF ECR, the bonus wage and the registers read
    # `gross_earnings` as monthly wages, and a one-off payment is not a wage.
    #
    #     net_pay = gross_earnings + other_earnings - total_deductions
    other_earnings: Decimal = Decimal(0)
    input_lines: list[ComputedInputLine] = field(default_factory=list)
    tds_working: dict[str, Any] | None = None
    # Unpaid days outside employment (before joining, after leaving).
    not_employed_days: Decimal = Decimal(0)
    # The structure's ESI wages, excluding overtime and other wage lines: what
    # ESI coverage is decided on. `esi_wage_base` is what contributions were
    # charged on, which includes them.
    esi_eligibility_base: Decimal = Decimal(0)
    # An off-cycle payslip that paid input lines only.
    supplementary: bool = False


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


_MANUAL_BASIS = "manual amount from the salary master"


def compute_payroll(payroll_input: PayrollInput) -> PayrollComputation:
    """Compute one employee's pay for one period.

    Order matters and is deliberate: prorate first, derive the statutory wage
    bases from the prorated amounts, then apply the rule sets.
    """
    salary = payroll_input.employee_salary
    working_days = payroll_input.working_days
    lop_days = payroll_input.lop_days
    not_employed_days = payroll_input.not_employed_days

    if working_days <= 0:
        raise PayrollRuleError("Working days must be greater than zero")
    if lop_days < 0:
        raise PayrollRuleError("Loss-of-pay days cannot be negative")
    if not_employed_days < 0:
        raise PayrollRuleError("Days outside employment cannot be negative")
    if not_employed_days >= working_days:
        raise PayrollRuleError(
            f"The employee was not employed on any day of this period "
            f"({not_employed_days} of {working_days} days); they are not paid by it."
        )
    if lop_days + not_employed_days > working_days:
        raise PayrollRuleError(
            f"Loss-of-pay days ({lop_days}) and days outside employment "
            f"({not_employed_days}) exceed working days ({working_days})"
        )

    for item in payroll_input.inputs:
        _check_input(item)
    supplementary = payroll_input.supplementary
    # Overtime, shift allowances and other pay for work: ESI and PT read them.
    wage_inputs = sum(
        (
            item.amount
            for item in payroll_input.inputs
            if item.counts_as_wages and item.kind is InputKind.EARNING
        ),
        start=_ZERO,
    )

    unpaid_days = lop_days + not_employed_days
    paid_days = working_days - unpaid_days
    lop_factor = paid_days / working_days
    unpaid_note = ", ".join(
        part
        for part in (
            f"{lop_days} loss of pay" if lop_days > 0 else "",
            f"{not_employed_days} not employed" if not_employed_days > 0 else "",
        )
        if part
    )

    # ── 1. Prorate ──────────────────────────────────────────────────────────
    lines: list[ComputedLine] = []
    prorated: dict[str, Decimal] = {}
    # A statutory MANUAL wage type on the salary master replaces what payroll
    # would work out for that deduction. It is never paid as a line of its own.
    overrides: dict[str, Decimal] = {}

    for component in sorted(salary.components, key=lambda c: c.display_order):
        if component.component_code in STATUTORY_OVERRIDE_CODES:
            overrides[STATUTORY_OVERRIDE_CODES[component.component_code]] = _money(
                Decimal(str(component.monthly_amount))
            )
            prorated[component.component_code] = _ZERO
            continue
        full = Decimal(str(component.monthly_amount))
        if component.pro_rate_on_lop and unpaid_days > 0:
            amount = _money(full * lop_factor)
            basis = f"structure, prorated {paid_days}/{working_days} days ({unpaid_note})"
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

    # The structure is computed either way - the TDS projection needs this
    # month's figures - but a supplementary payslip does not pay it again.
    if supplementary is not None:
        lines = []
        # The regular run took the month's statutory deductions, manual or not.
        overrides = {}

    gross_earnings = sum(
        (line.amount for line in lines if line.component_type is ComponentType.EARNING),
        start=Decimal(0),
    )

    # ── 2. Statutory wage bases, from the prorated amounts ──────────────────
    # PF on BASIC + DA (whatever the tenant has flagged), never on gross.
    pf_wage_base = sum(
        (prorated[c.component_code] for c in salary.components if c.counts_toward_pf),
        start=Decimal(0),
    )
    esi_wage_base = sum(
        (prorated[c.component_code] for c in salary.components if c.counts_toward_esi),
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
    # Not on a supplementary payslip: one-off pay is not PF wages, and the
    # month's PF was taken on the structure by the regular run.
    pf_rules: PayrollRuleSet | None = None
    pf_base = _ZERO
    pf_employee = pf_employer = vpf = _ZERO
    pf_employee_rate = pf_employer_rate = _ZERO
    if supplementary is None:
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

        pf_employee = _contribution(pf_rules, pf_base, pf_employee_rate, "nearest_rupee")
        pf_employer = _contribution(pf_rules, pf_base, pf_employer_rate, "nearest_rupee")
        if "pf" in overrides:
            pf_employee = overrides["pf"]

        # Voluntary PF is an *additional employee* contribution on the full wage
        # base. It never moves the employer's — treating it as a matched
        # contribution would silently inflate employer cost. It is remitted with
        # the employee's share on the ECR, so it is rounded as that share is.
        with _statutory_errors():
            vpf = compute_vpf(pf_wage_base=pf_wage_base, election=payroll_input.pf_election)
        if (
            vpf
            and payroll_input.pf_election is not None
            and payroll_input.pf_election.vpf_percent is not None
            and pf_rules.rules.get("contribution_rounding", "nearest_rupee") != "paise"
        ):
            vpf = vpf.quantize(_WHOLE_RUPEE, rounding=ROUND_HALF_UP).quantize(_RUPEE)

    # ── 4. ESI, only below the eligibility threshold ────────────────────────
    esi_employee = Decimal(0)
    esi_employer = Decimal(0)
    esi_eligibility_base = _ZERO if supplementary is not None else esi_wage_base
    contribution_base = _ZERO
    esi_rules = payroll_input.rule_sets.get("esi")
    if esi_rules is not None:
        if not esi_rules.is_approved:
            raise PayrollRuleError(
                f"The ESI rule set effective {esi_rules.effective_from} has not been "
                "approved by a Compliance SME."
            )
        if supplementary is not None:
            applies = supplementary.esi_covered
            candidate_base = wage_inputs
        else:
            threshold = _rule(esi_rules, "wage_threshold")
            applies, _ = _esi_applies(
                wage_base=esi_wage_base,
                threshold=threshold,
                month=payroll_input.period_start.month,
                rules=esi_rules.rules,
                contributing_at_period_start=payroll_input.esi_contributing_at_period_start,
            )
            # Coverage is decided on the structure's ESI wages; contributions
            # are charged on wages *including* overtime and shift pay.
            candidate_base = esi_wage_base + wage_inputs
        if applies and candidate_base > 0:
            contribution_base = candidate_base
            esi_employee = _contribution(
                esi_rules, contribution_base, _rule(esi_rules, "employee_rate"), "next_rupee"
            )
            esi_employer = _contribution(
                esi_rules, contribution_base, _rule(esi_rules, "employer_rate"), "next_rupee"
            )

    if "esi" in overrides:
        esi_employee = overrides["esi"]

    # ── 5. Professional Tax — a state slab, looked up by gross ──────────────
    #
    # PT is a state levy: different slabs, different returns, different filing
    # deadlines. Where the state cannot be established — an employee who moved
    # mid-period, or one with no work location — payroll flags it for a human
    # rather than picking a state. A wrong guess here files in the wrong state,
    # and nobody notices until an inspection.
    professional_tax = Decimal(0)
    month_gross = gross_earnings + wage_inputs
    pt_month = payroll_input.period_start.month
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
        if supplementary is not None:
            month_gross = supplementary.regular_gross + wage_inputs
            professional_tax = max(
                _ZERO,
                _lookup_slab(pt_rules, month_gross, month=pt_month, gender=payroll_input.gender)
                - supplementary.pt_already_deducted,
            )
        else:
            professional_tax = _lookup_slab(
                pt_rules, month_gross, month=pt_month, gender=payroll_input.gender
            )

    if "pt" in overrides:
        professional_tax = overrides["pt"]

    # ── 6. Labour Welfare Fund ──────────────────────────────────────────────
    #
    # A periodic state levy, not a monthly one. In a month the state does not
    # collect it, this is exactly zero — never a twelfth of the annual amount.
    lwf_employee = Decimal(0)
    lwf_employer = Decimal(0)
    lwf_rules = payroll_input.rule_sets.get("labour_welfare_fund")
    if supplementary is not None:
        # Collected once in a month, by the regular run.
        lwf_rules = None
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
                monthly_wages=gross_earnings + wage_inputs,
                designation=payroll_input.designation,
            )
        lwf_employee = lwf.employee_contribution
        lwf_employer = lwf.employer_contribution
    if "lwf" in overrides:
        lwf_employee = overrides["lwf"]

    # ── 7. Inputs from other modules ────────────────────────────────────────
    other_earnings = _ZERO
    input_deductions = _ZERO
    input_employer_cost = _ZERO
    one_off_taxable = _ZERO
    computed_inputs: list[ComputedInputLine] = []
    deferred_recoveries: list[InputLine] = []

    for item in payroll_input.inputs:
        if item.kind is InputKind.RECOVERY:
            deferred_recoveries.append(item)
            continue
        if item.kind in (InputKind.EARNING, InputKind.REIMBURSEMENT):
            other_earnings += item.amount
        elif item.kind is InputKind.DEDUCTION:
            input_deductions += item.amount
        elif item.kind is InputKind.EMPLOYER_CONTRIBUTION:
            input_employer_cost += item.amount
        one_off_taxable += item.taxable_amount
        computed_inputs.append(
            ComputedInputLine(
                provider=item.provider,
                code=item.code,
                name=item.name,
                kind=item.kind,
                amount=_money(item.amount),
                requested=_money(item.amount),
                taxable_amount=_money(item.taxable_amount),
                basis=item.basis,
                source=item.source,
                counts_as_wages=item.counts_as_wages,
            )
        )

    # ── 8. TDS ──────────────────────────────────────────────────────────────
    # Slice 07's annual projection, re-levelled every month. Without a context
    # (a structure-only recomputation) it stays zero; a run never approves
    # without one.
    tds = Decimal(0)
    tds_working: dict[str, Any] | None = None
    if payroll_input.tds is not None:
        # The projection reads the month as the regular run did: the same
        # structure figures, and the Professional Tax that run deducted.
        month_pt = (
            supplementary.pt_already_deducted if supplementary is not None else professional_tax
        )
        figures = _month_figures(salary, prorated, month_pt, one_off_taxable)
        try:
            result = compute_monthly_tds(payroll_input.tds, figures)
        except TaxRuleError as exc:
            raise PayrollRuleError(str(exc)) from exc
        if supplementary is not None:
            # The regular run deducted the month's recurring share; this payslip
            # deducts only the tax its one-off lines add.
            tds = result.one_off_amount
            tds_working = {
                **result.working,
                "supplementary": True,
                "tds": str(result.one_off_amount),
                "note": (
                    "Off-cycle payslip: only the tax on this payslip's one-off pay is "
                    "deducted. The month's recurring TDS was deducted by the regular run."
                ),
            }
        else:
            tds = result.amount
            tds_working = result.working
    if "tds" in overrides:
        tds = overrides["tds"]
        tds_working = {
            **(tds_working or {}),
            "manual_override": str(tds),
            "note": (
                "TDS MANUAL on the salary master is deducted instead of the projected monthly tax."
            ),
        }

    total_deductions = pf_employee + vpf + esi_employee + professional_tax + lwf_employee + tds
    total_deductions += sum(
        (line.amount for line in lines if line.component_type is ComponentType.DEDUCTION),
        start=Decimal(0),
    )
    total_deductions += input_deductions

    employer_contributions = sum(
        (
            line.amount
            for line in lines
            if line.component_type is ComponentType.EMPLOYER_CONTRIBUTION
        ),
        start=Decimal(0),
    )
    employer_contributions += input_employer_cost

    # ── 9. Recoveries, last, against what is actually left ──────────────────
    net_running = gross_earnings + other_earnings - total_deductions
    if net_running < 0:
        raise PayrollRuleError(
            f"Net pay would be {_money(net_running)} before any loan or advance recovery. "
            "Deductions exceed earnings; check the salary and the input lines before "
            "paying this employee."
        )

    # The legal ceiling on a wage period's deductions, where the tenant has an
    # approved rule set for it. Recoveries are the deductions that can wait, so
    # they are what the ceiling holds back: what does not fit stays owed for a
    # later month, as the Act allows. Deductions that cannot wait (tax, PF, ESI,
    # an attachment the employer must honour) are never cut here; if they alone
    # pass the ceiling, the payslip is flagged for a person to look at.
    ceiling: Decimal | None = None
    ceiling_percent: Decimal | None = None
    ceiling_rules = payroll_input.rule_sets.get("wage_deductions")
    if ceiling_rules is not None:
        if not ceiling_rules.is_approved:
            raise PayrollRuleError(
                f"The deductions-ceiling rule set effective {ceiling_rules.effective_from} "
                "has not been approved by a Compliance SME."
            )
        ceiling_percent = _rule(ceiling_rules, "max_deduction_percent")
        if not Decimal(0) < ceiling_percent <= Decimal(100):
            raise PayrollRuleError(
                "The deductions ceiling must be a percentage above 0 and at most 100."
            )
        ceiling = _money((gross_earnings + other_earnings) * ceiling_percent / Decimal(100))

    def _within_ceiling(amount: Decimal) -> bool:
        return ceiling is None or total_deductions + amount <= ceiling

    for item in deferred_recoveries:
        if item.amount <= net_running and _within_ceiling(item.amount):
            net_running -= item.amount
            total_deductions += item.amount
            computed_inputs.append(
                ComputedInputLine(
                    provider=item.provider,
                    code=item.code,
                    name=item.name,
                    kind=item.kind,
                    amount=_money(item.amount),
                    requested=_money(item.amount),
                    taxable_amount=_ZERO,
                    basis=item.basis,
                    source=item.source,
                )
            )
        else:
            reason = (
                f"{_money(item.amount)} is more than the {_money(net_running)} net pay left"
                if item.amount > net_running
                else (
                    f"it would take this month's deductions past {ceiling_percent}% of wages "
                    f"({ceiling}), the legal ceiling"
                )
            )
            computed_inputs.append(
                ComputedInputLine(
                    provider=item.provider,
                    code=item.code,
                    name=item.name,
                    kind=item.kind,
                    amount=_ZERO,
                    requested=_money(item.amount),
                    taxable_amount=_ZERO,
                    basis=(
                        f"Not recovered this month: {reason}. It stays owed and is attempted "
                        "again next run."
                    ),
                    source=item.source,
                    skipped=True,
                )
            )

    for loan in payroll_input.loan_recoveries:
        # The ceiling raises the floor the loan must leave: net pay may not drop
        # below what is left once deductions reach the ceiling.
        floor = loan.minimum_net_pay
        if ceiling is not None:
            floor = max(floor, _money(net_running - max(ceiling - total_deductions, _ZERO)))
        try:
            decision = plan_recovery(
                emi=loan.emi,
                net_pay_before_recovery=_money(net_running),
                minimum_net_pay=floor,
                outstanding_principal=loan.outstanding_principal,
                already_carried_forward=loan.carried_forward,
                allow_partial=loan.allow_partial,
            )
        except RecoveryError as exc:
            raise PayrollRuleError(f"{loan.name}: {exc}") from exc
        net_running -= decision.amount
        total_deductions += decision.amount
        computed_inputs.append(
            ComputedInputLine(
                provider=loan.provider,
                code=loan.code,
                name=loan.name,
                kind=InputKind.RECOVERY,
                amount=decision.amount,
                requested=_money(loan.emi + loan.carried_forward),
                taxable_amount=_ZERO,
                basis=(
                    decision.reason
                    if floor == loan.minimum_net_pay
                    else f"{decision.reason} The floor here is set by the {ceiling_percent}% "
                    "legal ceiling on deductions, not the scheme's minimum net pay."
                ),
                source=loan.source,
                skipped=decision.is_skipped,
                recovery=decision.as_dict(),
            )
        )

    if pf_rules is not None:
        pf_wages_shown = _reported_wage(pf_rules, pf_base, "nearest_rupee")
        lines.extend(
            [
                ComputedLine(
                    "PF_EMPLOYEE",
                    "Provident Fund (employee)",
                    ComponentType.DEDUCTION,
                    pf_employee,
                    _MANUAL_BASIS
                    if "pf" in overrides
                    else f"{pf_employee_rate}% of PF wages {pf_wages_shown}",
                    str(pf_rules.id),
                ),
                ComputedLine(
                    "PF_EMPLOYER",
                    "Provident Fund (employer)",
                    ComponentType.EMPLOYER_CONTRIBUTION,
                    pf_employer,
                    f"{pf_employer_rate}% of PF wages {pf_wages_shown}",
                    str(pf_rules.id),
                ),
            ]
        )
    if esi_rules is not None and (esi_employee or esi_employer):
        esi_wages_shown = _reported_wage(esi_rules, contribution_base, "next_rupee")
        lines.extend(
            [
                ComputedLine(
                    "ESI_EMPLOYEE",
                    "ESI (employee)",
                    ComponentType.DEDUCTION,
                    esi_employee,
                    _MANUAL_BASIS if "esi" in overrides else f"ESI wages {esi_wages_shown}",
                    str(esi_rules.id),
                ),
                ComputedLine(
                    "ESI_EMPLOYER",
                    "ESI (employer)",
                    ComponentType.EMPLOYER_CONTRIBUTION,
                    esi_employer,
                    f"ESI wages {esi_wages_shown}",
                    str(esi_rules.id),
                ),
            ]
        )
    elif esi_employee:
        # Typed on the salary master where no ESI rules apply.
        lines.append(
            ComputedLine(
                "ESI_EMPLOYEE",
                "ESI (employee)",
                ComponentType.DEDUCTION,
                esi_employee,
                _MANUAL_BASIS,
            )
        )
    if vpf and pf_rules is not None:
        lines.append(
            ComputedLine(
                "VPF",
                "Voluntary Provident Fund",
                ComponentType.DEDUCTION,
                vpf,
                f"employee election on PF wage base {pf_wage_base}",
                str(pf_rules.id),
            )
        )
    if lwf_rules is None and lwf_employee:
        # Typed on the salary master where no LWF rules apply this month.
        lines.append(
            ComputedLine(
                "LWF_EMPLOYEE",
                "Labour Welfare Fund (employee)",
                ComponentType.DEDUCTION,
                lwf_employee,
                _MANUAL_BASIS,
            )
        )
    elif lwf_employee or lwf_employer:
        lines.extend(
            [
                ComputedLine(
                    "LWF_EMPLOYEE",
                    "Labour Welfare Fund (employee)",
                    ComponentType.DEDUCTION,
                    lwf_employee,
                    _MANUAL_BASIS
                    if "lwf" in overrides
                    else f"{lwf_rules.jurisdiction} levy, month "  # type: ignore[union-attr]
                    f"{payroll_input.period_start.month}",
                    str(lwf_rules.id),  # type: ignore[union-attr]
                ),
                ComputedLine(
                    "LWF_EMPLOYER",
                    "Labour Welfare Fund (employer)",
                    ComponentType.EMPLOYER_CONTRIBUTION,
                    lwf_employer,
                    f"{lwf_rules.jurisdiction} levy, month "  # type: ignore[union-attr]
                    f"{payroll_input.period_start.month}",
                    str(lwf_rules.id),
                ),  # type: ignore[union-attr]
            ]
        )
    if professional_tax:
        lines.append(
            ComputedLine(
                "PT",
                "Professional Tax",
                ComponentType.DEDUCTION,
                professional_tax,
                _MANUAL_BASIS
                if "pt" in overrides
                else f"{pt_rules.jurisdiction} slab on gross {month_gross}",  # type: ignore[union-attr]
                str(pt_rules.id) if pt_rules is not None else None,
            )
        )

    over_ceiling: dict[str, str] | None = None
    if ceiling is not None and total_deductions > ceiling:
        wages = gross_earnings + other_earnings
        over_ceiling = {
            "wages": str(_money(wages)),
            "deductions": str(_money(total_deductions)),
            "share_percent": str(
                (total_deductions / wages * Decimal(100)).quantize(Decimal("0.01"))
                if wages > 0
                else Decimal(100)
            ),
            "ceiling_percent": str(ceiling_percent),
            "ceiling": str(ceiling),
        }

    return PayrollComputation(
        working_days=working_days,
        paid_days=paid_days,
        lop_days=lop_days,
        gross_earnings=_money(gross_earnings),
        total_deductions=_money(total_deductions),
        net_pay=_money(gross_earnings + other_earnings - total_deductions),
        employer_cost=_money(
            gross_earnings
            + other_earnings
            + employer_contributions
            + pf_employer
            + esi_employer
            + lwf_employer
        ),
        pf_wage_base=_money(pf_wage_base) if supplementary is None else _ZERO,
        esi_wage_base=_money(contribution_base),
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
        other_earnings=_money(other_earnings),
        input_lines=computed_inputs,
        tds_working=tds_working,
        not_employed_days=not_employed_days,
        esi_eligibility_base=_money(esi_eligibility_base),
        supplementary=supplementary is not None,
        over_deduction_ceiling=over_ceiling,
    )


def _check_input(item: InputLine) -> None:
    """An input line is money from another module; hold it to payroll's rules."""
    for name, value in (("amount", item.amount), ("taxable amount", item.taxable_amount)):
        if not isinstance(value, Decimal):
            raise PayrollRuleError(f"{item.name}: the {name} must be a Decimal, not a float")
    if item.kind is not InputKind.EMPLOYER_CONTRIBUTION and item.amount < 0:
        raise PayrollRuleError(f"{item.name}: an input line cannot be negative")
    if item.taxable_amount < 0:
        raise PayrollRuleError(f"{item.name}: a taxable amount cannot be negative")
    if item.kind in (InputKind.EARNING, InputKind.PERQUISITE) and item.taxable_amount > item.amount:
        raise PayrollRuleError(
            f"{item.name}: {item.taxable_amount} taxable is more than the {item.amount} paid"
        )
    if (
        item.kind
        in (
            InputKind.REIMBURSEMENT,
            InputKind.DEDUCTION,
            InputKind.RECOVERY,
            InputKind.EMPLOYER_CONTRIBUTION,
        )
        and item.taxable_amount != 0
    ):
        raise PayrollRuleError(f"{item.name}: a {item.kind.value} line carries no taxable income")


def _month_figures(
    salary: EmployeeSalary,
    prorated: dict[str, Decimal],
    professional_tax: Decimal,
    one_off_taxable: Decimal,
) -> MonthFigures:
    """This month's pay in the shape the projection reads.

    `*_this_month` is what was actually paid (after loss of pay); `*_full_month`
    is the materialised monthly amount the later months are projected at.
    """
    taxable_now = taxable_full = _ZERO
    basic_now = basic_full = _ZERO
    hra_now = hra_full = _ZERO
    nps_now = nps_full = _ZERO

    for component in salary.components:
        now = prorated[component.component_code]
        full = _money(Decimal(str(component.monthly_amount)))
        if component.component_type is ComponentType.EARNING and component.taxable:
            taxable_now += now
            taxable_full += full
        if component.counts_toward_pf:
            basic_now += now
            basic_full += full
        if component.component_code in _HRA_CODES:
            hra_now += now
            hra_full += full
        if component.component_code in _EMPLOYER_NPS_CODES:
            nps_now += now
            nps_full += full

    return MonthFigures(
        taxable_this_month=taxable_now,
        taxable_full_month=taxable_full,
        basic_plus_da_this_month=basic_now,
        basic_plus_da_full_month=basic_full,
        hra_this_month=hra_now,
        hra_full_month=hra_full,
        employer_nps_this_month=nps_now,
        employer_nps_full_month=nps_full,
        professional_tax=professional_tax,
        one_off_taxable=one_off_taxable,
    )


def professional_tax_slab(
    rule_set: PayrollRuleSet,
    monthly_gross: Decimal,
    *,
    month: int | None = None,
    gender: str | None = None,
) -> Decimal:
    """The Professional Tax a state's approved slabs charge on a month's gross."""
    return _lookup_slab(rule_set, monthly_gross, month=month, gender=gender)


def _lookup_slab(
    rule_set: PayrollRuleSet,
    monthly_gross: Decimal,
    *,
    month: int | None = None,
    gender: str | None = None,
) -> Decimal:
    """Find the slab a monthly gross falls into, and what it charges this month.

    Slabs are supplied by the Compliance SME as
    `[{"up_to": 15000, "amount": 150}, {"up_to": null, "amount": 200}]` —
    ordered, with a null upper bound on the last. Nothing about the values is
    known to this code. Two shapes some states need, both optional:

    - a slab may charge a different amount in particular months, as
      `"month_amounts": {"2": 300}` (the February instalment that makes up a
      state's annual maximum);
    - `slabs_by_gender: {"female": [...]}` replaces the slabs for that gender
      (a state that exempts women below a higher wage).
    """
    slabs = rule_set.rules.get("slabs")
    by_gender = rule_set.rules.get("slabs_by_gender") or {}
    if gender and isinstance(by_gender, dict) and by_gender.get(str(gender).lower()):
        slabs = by_gender[str(gender).lower()]
    if not isinstance(slabs, list) or not slabs:
        raise PayrollRuleError(
            f"The Professional Tax rule set for {rule_set.jurisdiction} has no slabs"
        )

    for number, slab in enumerate(slabs, start=1):
        upper = slab.get("up_to")
        if upper is None and number < len(slabs):
            # Everyone would stop at this slab and those above it would never
            # charge: a malformed table, not a lower tax.
            raise PayrollRuleError(
                f"Professional Tax slab {number} for {rule_set.jurisdiction} has no upper "
                "bound but is not the last slab. Only the last slab is open-ended."
            )
        if upper is None or monthly_gross <= Decimal(str(upper)):
            amount = slab.get("amount", 0)
            month_amounts = slab.get("month_amounts") or {}
            if month is not None and str(month) in month_amounts:
                amount = month_amounts[str(month)]
            return _money(Decimal(str(amount)))

    # Reachable only if the last slab has a bound and gross exceeds it, which
    # is a malformed rule set rather than an untaxed employee.
    raise PayrollRuleError(
        f"Monthly gross {monthly_gross} falls outside every Professional Tax slab for "
        f"{rule_set.jurisdiction}. The last slab needs an open upper bound."
    )
