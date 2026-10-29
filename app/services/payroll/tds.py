"""Monthly TDS inside a payroll run: slice 07's projection, applied to one payslip.

Pure. No session, no request, no today. The run gathers what it needs — the
employee's declarations and regime (through `app.services.tax.service`), what
approved runs earlier in the financial year already paid and deducted — and
hands it here with this month's figures.

═══════════════════════════════════════════════════════════════════════════════
NO TAX RULE LIVES HERE EITHER
═══════════════════════════════════════════════════════════════════════════════

Slabs, rebates, cess, section ceilings and HRA percentages are read by
`app.services.tax.computation` from the approved `tds` rule set the run
resolved by its own effective date. This module only decides *what income to
project*, which is arithmetic, not policy.

Two rules worth knowing before changing anything:

**Recurring pay is projected; one-off pay is taxed in the month it is paid.**
Salary for the months still to come is projected at this month's full
(unprorated) rate, and the liability on it is spread across the months that
remain — slice 07's re-levelling. A bonus, an encashment, arrears or a referral
payout is not recurring: the extra liability it creates is deducted in full in
the month it is paid. Spreading it would under-deduct now and leave a spike
in March for a payment made in October.

**Earlier months come from approved payslips, not from the salary record.** A
revision in October must not re-price April, and a month with loss of pay was
taxed on what was actually paid.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.services.tax.computation import (
    ProjectionInput,
    TaxRuleError,
    monthly_tds,
    months_remaining_in_year,
    project,
    round_tds,
)

__all__ = [
    "MonthFigures",
    "PriorPay",
    "TdsContext",
    "TdsResult",
    "compute_monthly_tds",
    "financial_year_for",
    "fy_start_month",
    "without_income_paid_by_payroll",
]

ZERO = Decimal(0)
RUPEE = Decimal("0.01")

# The Indian financial year opens in April. Slice 07's service uses the same
# fallback; a rule set that says otherwise (`financial_year_start_month`) wins.
_DEFAULT_FY_START_MONTH = 4
_MONTHS = 12


def _money(value: Decimal) -> Decimal:
    return value.quantize(RUPEE, rounding=ROUND_HALF_UP)


def fy_start_month(rules: dict[str, Any]) -> int:
    raw = rules.get("financial_year_start_month", _DEFAULT_FY_START_MONTH)
    try:
        month = int(raw)
    except (TypeError, ValueError) as exc:
        raise TaxRuleError(
            f"The income tax rule set's financial_year_start_month '{raw}' is not a month."
        ) from exc
    if not 1 <= month <= _MONTHS:
        raise TaxRuleError(
            f"The income tax rule set's financial_year_start_month {month} is not a month."
        )
    return month


def financial_year_for(on: date, start_month: int) -> tuple[str, date, date]:
    """The financial year containing a date: `("2026-27", first day, last day)`.

    The label matches slice 07's tables (`TaxDeclaration.financial_year`).
    """
    start_year = on.year if on.month >= start_month else on.year - 1
    start = date(start_year, start_month, 1)
    end_year = start_year + 1 if start_month > 1 else start_year
    end_month = start_month - 1 if start_month > 1 else _MONTHS
    import calendar

    end = date(end_year, end_month, calendar.monthrange(end_year, end_month)[1])
    return f"{start_year}-{str(start_year + 1)[-2:]}", start, end


@dataclass(frozen=True, slots=True)
class PriorPay:
    """What approved runs earlier in this financial year paid and deducted."""

    recurring_taxable: Decimal = ZERO
    basic_plus_da: Decimal = ZERO
    hra: Decimal = ZERO
    employer_nps: Decimal = ZERO
    professional_tax: Decimal = ZERO
    # Taxable one-off income already paid: bonus, encashment, arrears, perquisites.
    one_off_taxable: Decimal = ZERO
    tds_deducted: Decimal = ZERO
    months_on_record: int = 0
    #: Earlier months of the year paid on the previous payroll (before the
    #: company's first AskMira run), counted at the salary structure. Their tax
    #: is taken as deducted at the year's steady monthly rate.
    months_from_structure: int = 0


@dataclass(frozen=True, slots=True)
class TdsContext:
    """Everything about the employee's year that is not this month's pay."""

    rules: dict[str, Any]
    rule_set_id: str
    financial_year: str
    # Months left in the financial year, including this one.
    months_remaining: int
    # Regime, declarations (declared or accepted, as the run decided), rent,
    # age and previous-employer income. Its salary fields are replaced here.
    base: ProjectionInput
    prior: PriorPay = field(default_factory=PriorPay)
    # Why declared rather than proved amounts were used, or the reverse.
    declaration_basis: str = ""


@dataclass(frozen=True, slots=True)
class MonthFigures:
    """This month's pay, as the engine computed it."""

    taxable_this_month: Decimal
    taxable_full_month: Decimal
    basic_plus_da_this_month: Decimal
    basic_plus_da_full_month: Decimal
    hra_this_month: Decimal
    hra_full_month: Decimal
    employer_nps_this_month: Decimal
    employer_nps_full_month: Decimal
    professional_tax: Decimal
    one_off_taxable: Decimal = ZERO


@dataclass(frozen=True, slots=True)
class TdsResult:
    amount: Decimal
    recurring_amount: Decimal
    one_off_amount: Decimal
    annual_liability: Decimal
    working: dict[str, Any]


def compute_monthly_tds(context: TdsContext, figures: MonthFigures) -> TdsResult:
    """This month's TDS, and the working behind it.

    Raises `TaxRuleError` when the rule set is missing something the projection
    needs; the engine turns that into a per-employee payroll error.
    """
    if context.months_remaining < 1:
        raise TaxRuleError("Months remaining in the financial year must be at least one")

    for name, value in (
        ("taxable pay this month", figures.taxable_this_month),
        ("one-off taxable pay", figures.one_off_taxable),
        ("tax already deducted", context.prior.tds_deducted),
    ):
        if not isinstance(value, Decimal):
            raise TaxRuleError(f"The {name} must be a Decimal, not a float")

    prior = context.prior
    later = Decimal(context.months_remaining - 1)

    recurring_gross = (
        prior.recurring_taxable + figures.taxable_this_month + figures.taxable_full_month * later
    )
    basic_plus_da = (
        prior.basic_plus_da
        + figures.basic_plus_da_this_month
        + figures.basic_plus_da_full_month * later
    )
    hra = prior.hra + figures.hra_this_month + figures.hra_full_month * later
    employer_nps = (
        prior.employer_nps
        + figures.employer_nps_this_month
        + figures.employer_nps_full_month * later
    )
    professional_tax = prior.professional_tax + figures.professional_tax * (later + 1)

    without_one_off = replace(
        context.base,
        gross_salary=recurring_gross + prior.one_off_taxable,
        basic_plus_da=basic_plus_da,
        hra_received=hra,
        employer_nps=employer_nps,
        professional_tax=professional_tax,
    )
    projection = project(without_one_off, context.rules)

    rounding = str(context.rules.get("tds_rounding", "nearest_rupee"))
    # Months paid on the previous payroll had tax deducted there; AskMira has no
    # record of how much. Taken at the year's steady rate (the liability spread
    # over every month of the year worked), the remaining months deduct that
    # same rate rather than the whole year's tax again.
    months_in_year = prior.months_from_structure + prior.months_on_record
    months_in_year += context.months_remaining
    assumed_earlier = (
        _money(
            projection.annual_tax_liability
            * Decimal(prior.months_from_structure)
            / Decimal(months_in_year)
        )
        if prior.months_from_structure
        else ZERO
    )
    recurring_amount = monthly_tds(
        annual_liability=projection.annual_tax_liability,
        already_deducted=prior.tds_deducted + assumed_earlier,
        months_remaining=context.months_remaining,
        previous_employer_tds=context.base.previous_tds,
        rounding=rounding,
    )

    with_projection = projection
    one_off_amount = ZERO
    if figures.one_off_taxable > ZERO:
        with_one_off = replace(
            without_one_off,
            gross_salary=without_one_off.gross_salary + figures.one_off_taxable,
        )
        with_projection = project(with_one_off, context.rules)
        one_off_amount = round_tds(
            max(
                ZERO,
                with_projection.annual_tax_liability - projection.annual_tax_liability,
            ),
            rounding,
        )

    amount = _money(recurring_amount + one_off_amount)

    working: dict[str, Any] = {
        "financial_year": context.financial_year,
        "rule_set_id": context.rule_set_id,
        "regime": context.base.regime,
        "declaration_basis": context.declaration_basis,
        "months_remaining": context.months_remaining,
        "projection": {
            "earlier_months_taxable_pay": str(_money(prior.recurring_taxable)),
            "earlier_months_from_salary_structure": prior.months_from_structure,
            "this_month_taxable_pay": str(_money(figures.taxable_this_month)),
            "later_months_projected_at": str(_money(figures.taxable_full_month)),
            "later_months": int(later),
            "earlier_one_off_taxable": str(_money(prior.one_off_taxable)),
            "this_month_one_off_taxable": str(_money(figures.one_off_taxable)),
            "previous_employer_gross": str(_money(context.base.previous_gross)),
            "previous_employer_tds": str(_money(context.base.previous_tds)),
        },
        "annual_liability_excluding_this_month_one_off": str(projection.annual_tax_liability),
        "annual_liability": str(with_projection.annual_tax_liability),
        "tds_already_deducted": str(_money(prior.tds_deducted)),
        "tds_assumed_deducted_by_previous_payroll": str(assumed_earlier),
        "recurring_tds": str(recurring_amount),
        "one_off_tds": str(one_off_amount),
        "tds": str(amount),
        "computation": with_projection.as_working(),
    }

    return TdsResult(
        amount=amount,
        recurring_amount=recurring_amount,
        one_off_amount=one_off_amount,
        annual_liability=with_projection.annual_tax_liability,
        working=working,
    )


def without_income_paid_by_payroll(
    base: ProjectionInput, paid_by_payroll: frozenset[str]
) -> ProjectionInput:
    """The year's other income, less what the run itself pays as input lines.

    Arrears and in-service encashment reach the payslip as input lines with
    their taxable amount, which this module taxes in the month they are paid
    (`one_off_taxable`, then `PriorPay.one_off_taxable`). Left in the base's
    `other_income` as well, the same rupee would be taxed twice.
    """
    kept = tuple(line for line in base.other_income if line[0] not in paid_by_payroll)
    if len(kept) == len(base.other_income):
        return base
    return replace(base, other_income=kept)


def months_remaining(period_month: int, rules: dict[str, Any]) -> int:
    """Months left in the financial year, including `period_month`."""
    return months_remaining_in_year(period_month, fy_start_month(rules))
