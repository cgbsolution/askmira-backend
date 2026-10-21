"""Income tax projection and TDS levelling.

═══════════════════════════════════════════════════════════════════════════════
NO NUMBER IN THIS FILE IS A TAX RULE
═══════════════════════════════════════════════════════════════════════════════

Every slab, ceiling, rate, percentage and threshold is read from an
effective-dated `PayrollRuleSet` supplied and approved by the Compliance SME.
There is no 150000, no 50000, no 40/50 percent, no cess rate. A Budget change
is a configuration change, not a release — and a test greps this module to keep
it that way.

The only numeric literals present are structural: 12 months in a year, 100 for
a percentage, 0 and 1. Those are arithmetic, not policy.
═══════════════════════════════════════════════════════════════════════════════

Two behaviours worth reading before changing anything:

**TDS re-levels every month.** It is not a flat twelfth of an annual figure
computed in April. Each month the engine reprojects the annual liability from
whatever is currently known — declarations, verified proofs, salary revisions,
previous-employer income — subtracts what has already been deducted, and
spreads the residual across the months that remain. A proof verified in January
therefore reduces February and March, which is what an employee expects.

**Unproven declarations fall to their accepted amounts.** After the proof
window closes, a declaration with no verified proof contributes nothing. That
usually spikes March TDS, and the spike is legitimate — but it must be
surfaced in advance rather than discovered on a payslip.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

__all__ = [
    "DeductionLine",
    "HraWorking",
    "TaxComputation",
    "TaxRuleError",
    "compute_annual_tax",
    "compute_hra_exemption",
    "monthly_tds",
    "project",
]

# Structural constants. Not tax policy — arithmetic.
MONTHS_IN_YEAR = 12
PERCENT = Decimal(100)
ZERO = Decimal(0)
RUPEE = Decimal("0.01")


class TaxRuleError(ValueError):
    """The rule set is missing something the computation needs.

    Raised rather than defaulted. A silently assumed slab produces a number
    that looks plausible and is wrong, which is the worst outcome available.
    """


def _money(value: Decimal) -> Decimal:
    return value.quantize(RUPEE, rounding=ROUND_HALF_UP)


def _require(rules: dict[str, Any], path: str) -> Any:
    """Read a required value from the rule set, or fail naming the path."""
    current: Any = rules
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise TaxRuleError(
                f"The income tax rule set is missing '{path}'. Ask the Compliance SME "
                "to supply it — this value is never assumed."
            )
        current = current[part]
    return current


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


@dataclass(frozen=True, slots=True)
class HraWorking:
    """The three-way HRA comparison, with the binding constraint named.

    An employee who is told "your HRA exemption is 84,000" and cannot see why
    raises a ticket. Recording which of the three bound is the difference
    between an explanation and an assertion.
    """

    actual_hra_received: Decimal
    rent_less_basic_percentage: Decimal
    percentage_of_basic: Decimal
    exemption: Decimal
    # "actual_hra" | "rent_less_basic" | "percentage_of_basic"
    binding_constraint: str
    is_metro: bool
    annual_rent: Decimal

    def explain(self) -> str:
        labels = {
            "actual_hra": "the HRA you actually received",
            "rent_less_basic": "rent paid less a percentage of basic salary",
            "percentage_of_basic": (
                f"a percentage of basic salary ({'metro' if self.is_metro else 'non-metro'} rate)"
            ),
        }
        return (
            f"Your HRA exemption is {self.exemption}, limited by {labels[self.binding_constraint]}."
        )


@dataclass(frozen=True, slots=True)
class DeductionLine:
    section: str
    declared: Decimal
    # What was actually proved and accepted.
    accepted: Decimal
    # After the section ceiling from configuration is applied.
    allowed: Decimal
    ceiling: Decimal | None
    capped: bool
    note: str = ""


@dataclass(slots=True)
class TaxComputation:
    regime: str
    gross_annual_income: Decimal
    total_exemptions: Decimal
    total_deductions: Decimal
    taxable_income: Decimal
    tax_before_rebate: Decimal
    rebate: Decimal
    surcharge: Decimal
    cess: Decimal
    annual_tax_liability: Decimal
    deduction_lines: list[DeductionLine] = field(default_factory=list)
    hra: HraWorking | None = None
    slab_breakdown: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Taxable income from other modules that was added to gross, and amounts
    # that would be added if still unclaimed at year end.
    other_income: list[dict[str, str]] = field(default_factory=list)
    pending_if_unclaimed: list[dict[str, str]] = field(default_factory=list)

    def as_working(self) -> dict[str, Any]:
        """The full working, for storage and for showing the employee."""
        return {
            "other_income": self.other_income,
            "pending_if_unclaimed": self.pending_if_unclaimed,
            "regime": self.regime,
            "gross_annual_income": str(self.gross_annual_income),
            "total_exemptions": str(self.total_exemptions),
            "total_deductions": str(self.total_deductions),
            "taxable_income": str(self.taxable_income),
            "tax_before_rebate": str(self.tax_before_rebate),
            "rebate": str(self.rebate),
            "surcharge": str(self.surcharge),
            "cess": str(self.cess),
            "annual_tax_liability": str(self.annual_tax_liability),
            "slabs": self.slab_breakdown,
            "deductions": [
                {
                    "section": line.section,
                    "declared": str(line.declared),
                    "accepted": str(line.accepted),
                    "allowed": str(line.allowed),
                    "ceiling": str(line.ceiling) if line.ceiling is not None else None,
                    "capped": line.capped,
                    "note": line.note,
                }
                for line in self.deduction_lines
            ],
            "hra": (
                {
                    "actual_hra_received": str(self.hra.actual_hra_received),
                    "rent_less_basic_percentage": str(self.hra.rent_less_basic_percentage),
                    "percentage_of_basic": str(self.hra.percentage_of_basic),
                    "exemption": str(self.hra.exemption),
                    "binding_constraint": self.hra.binding_constraint,
                    "is_metro": self.hra.is_metro,
                    "explanation": self.hra.explain(),
                }
                if self.hra
                else None
            ),
            "notes": self.notes,
        }


# ── HRA ─────────────────────────────────────────────────────────────────────


def compute_hra_exemption(
    *,
    hra_received: Decimal,
    basic_plus_da: Decimal,
    annual_rent: Decimal,
    is_metro: bool,
    rules: dict[str, Any],
) -> HraWorking:
    """The least of three, with the binding one recorded.

    All three percentages come from configuration. The *structure* of the
    calculation is statute and lives here; the numbers do not.
    """
    hra_rules = _require(rules, "hra")

    metro_percent = _decimal(_require(hra_rules, "metro_percent"))
    non_metro_percent = _decimal(_require(hra_rules, "non_metro_percent"))
    rent_offset_percent = _decimal(_require(hra_rules, "rent_less_basic_percent"))

    applicable_percent = metro_percent if is_metro else non_metro_percent

    candidates: dict[str, Decimal] = {
        "actual_hra": _money(hra_received),
        "rent_less_basic": _money(
            max(ZERO, annual_rent - (basic_plus_da * rent_offset_percent / PERCENT))
        ),
        "percentage_of_basic": _money(basic_plus_da * applicable_percent / PERCENT),
    }

    # Deterministic on a tie: dict order is insertion order, and `min` keeps
    # the first minimum. So a tie reports "actual_hra", which is the most
    # defensible answer to "why".
    binding = min(candidates, key=lambda key: candidates[key])

    return HraWorking(
        actual_hra_received=candidates["actual_hra"],
        rent_less_basic_percentage=candidates["rent_less_basic"],
        percentage_of_basic=candidates["percentage_of_basic"],
        exemption=candidates[binding],
        binding_constraint=binding,
        is_metro=is_metro,
        annual_rent=_money(annual_rent),
    )


# ── Deductions ──────────────────────────────────────────────────────────────


def apply_section_ceiling(
    section: str,
    amount: Decimal,
    rules: dict[str, Any],
    *,
    age: int | None = None,
) -> tuple[Decimal, Decimal | None]:
    """Cap an amount at its section ceiling. Returns `(allowed, ceiling)`.

    A section with no configured ceiling is uncapped, which is correct for
    sections that have none — and is not the same as a ceiling of zero.
    """
    limits = rules.get("section_limits", {})
    config = limits.get(section)
    if config is None:
        return amount, None

    # Some ceilings vary by age band (80D is the common case). The bands come
    # from configuration; the idea that a band exists is statute.
    if "ceiling_by_age" in config:
        if age is None:
            raise TaxRuleError(
                f"Section {section} has an age-banded ceiling but no age was supplied"
            )
        for band in config["ceiling_by_age"]:
            max_age = band.get("max_age")
            if max_age is None or age <= int(max_age):
                ceiling = _decimal(band["ceiling"])
                break
        else:
            raise TaxRuleError(
                f"No {section} ceiling band covers age {age}. The last band needs an "
                "open upper bound."
            )
    else:
        ceiling = _decimal(_require(config, "ceiling"))

    return min(amount, ceiling), ceiling


# ── Slabs, rebate, surcharge, cess ──────────────────────────────────────────


def _tax_from_slabs(taxable: Decimal, slabs: list[dict[str, Any]]) -> tuple[Decimal, list[dict]]:
    """Apply a marginal slab table. Returns the tax and the working.

    Slabs are supplied as `[{"up_to": 300000, "rate": 0}, …]`, ordered, with a
    null upper bound on the last.
    """
    if not slabs:
        raise TaxRuleError("The regime has no tax slabs configured")

    tax = ZERO
    lower = ZERO
    breakdown: list[dict] = []

    for slab in slabs:
        upper_raw = slab.get("up_to")
        upper = _decimal(upper_raw) if upper_raw is not None else None
        rate = _decimal(_require(slab, "rate"))

        if upper is not None and taxable <= lower:
            break

        band_top = taxable if upper is None else min(taxable, upper)
        band_amount = max(ZERO, band_top - lower)

        if band_amount > ZERO:
            band_tax = band_amount * rate / PERCENT
            tax += band_tax
            breakdown.append(
                {
                    "from": str(_money(lower)),
                    "to": str(_money(band_top)),
                    "rate": str(rate),
                    "tax": str(_money(band_tax)),
                }
            )

        if upper is None:
            break
        lower = upper

    if slabs[-1].get("up_to") is not None and taxable > _decimal(slabs[-1]["up_to"]):
        raise TaxRuleError(
            f"Taxable income {taxable} exceeds the highest configured slab. The last "
            "slab needs an open upper bound."
        )

    return _money(tax), breakdown


def _rebate(taxable: Decimal, tax: Decimal, regime_rules: dict[str, Any]) -> Decimal:
    """Section 87A-style rebate, if the regime configures one."""
    config = regime_rules.get("rebate")
    if not config:
        return ZERO

    income_limit = _decimal(_require(config, "income_limit"))
    if taxable > income_limit:
        return ZERO

    return min(tax, _decimal(_require(config, "max_rebate")))


def _surcharge(taxable: Decimal, tax: Decimal, rules: dict[str, Any]) -> Decimal:
    """Surcharge on high incomes, from a configured band table."""
    bands = rules.get("surcharge_bands")
    if not bands:
        return ZERO

    for band in bands:
        above = _decimal(_require(band, "above"))
        upper_raw = band.get("up_to")
        upper = _decimal(upper_raw) if upper_raw is not None else None
        if taxable > above and (upper is None or taxable <= upper):
            return _money(tax * _decimal(_require(band, "rate")) / PERCENT)
    return ZERO


def compute_annual_tax(
    *,
    taxable_income: Decimal,
    regime: str,
    rules: dict[str, Any],
) -> tuple[Decimal, Decimal, Decimal, Decimal, list[dict]]:
    """Tax on a taxable income under a regime.

    Returns `(tax_before_rebate, rebate, surcharge, cess, slab_breakdown)`.
    """
    regime_rules = _require(rules, f"regimes.{regime}")
    slabs = _require(regime_rules, "slabs")

    tax, breakdown = _tax_from_slabs(taxable_income, slabs)
    rebate = _rebate(taxable_income, tax, regime_rules)
    after_rebate = max(ZERO, tax - rebate)

    surcharge = _surcharge(taxable_income, after_rebate, rules)
    cess_percent = _decimal(_require(rules, "cess_percent"))
    cess = _money((after_rebate + surcharge) * cess_percent / PERCENT)

    return tax, rebate, surcharge, cess, breakdown


# ── The projection ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ProjectionInput:
    """Everything the projection needs, already gathered.

    A dataclass rather than a session query so the engine stays a pure function
    — which is what makes the regime comparison cheap and the tests real.
    """

    regime: str
    # Annual salary income from the current employer, before exemptions.
    gross_salary: Decimal
    # Annual BASIC + DA. Drives the HRA calculation.
    basic_plus_da: Decimal
    # Annual HRA component received.
    hra_received: Decimal = ZERO
    # `[(section, declared, accepted)]`.
    declarations: tuple[tuple[str, Decimal, Decimal], ...] = ()
    annual_rent: Decimal = ZERO
    is_metro: bool = False
    age: int | None = None
    # Previous employer, for a mid-year joiner.
    previous_gross: Decimal = ZERO
    previous_tds: Decimal = ZERO
    previous_exempt: Decimal = ZERO
    # Professional tax paid, which is deductible under the old regime.
    professional_tax: Decimal = ZERO
    # Employer PF contribution, deductible under 80CCD(2) in both regimes.
    employer_nps: Decimal = ZERO
    # Taxable income from outside the salary structure, already computed by the
    # module that owns it — the taxable part of a gratuity or an encashment,
    # arrears received this year, a concessional-loan perquisite, entitlement
    # left unclaimed at year end. `(code, label, amount, source, basis)`.
    # Added to gross here and nowhere else, so each figure is counted once.
    other_income: tuple[tuple[str, str, Decimal, str, str], ...] = ()
    # Same shape, informational only: amounts that *would* become taxable (an
    # unclaimed reimbursement before year end). Never added to income.
    pending_if_unclaimed: tuple[tuple[str, str, Decimal, str, str], ...] = ()
    # Plain-language notes from the gatherer, e.g. a missing loan rule set.
    income_notes: tuple[str, ...] = ()


def project(inputs: ProjectionInput, rules: dict[str, Any]) -> TaxComputation:
    """Project the annual tax liability.

    The regime decides which exemptions and deductions apply, and *that list*
    is configuration too — `regimes.<name>.deductions_allowed`. A Budget that
    lets the new regime claim a deduction it previously could not is a rule-set
    edit.
    """
    regime_rules = _require(rules, f"regimes.{inputs.regime}")
    allowed_sections = set(regime_rules.get("deductions_allowed", []))

    computation = TaxComputation(
        regime=inputs.regime,
        gross_annual_income=ZERO,
        total_exemptions=ZERO,
        total_deductions=ZERO,
        taxable_income=ZERO,
        tax_before_rebate=ZERO,
        rebate=ZERO,
        surcharge=ZERO,
        cess=ZERO,
        annual_tax_liability=ZERO,
    )

    other_total = sum((amount for _, _, amount, _, _ in inputs.other_income), start=ZERO)
    gross = inputs.gross_salary + inputs.previous_gross + other_total
    computation.gross_annual_income = _money(gross)

    if inputs.previous_gross > ZERO:
        computation.notes.append(
            f"Includes {_money(inputs.previous_gross)} from a previous employer, with "
            f"{_money(inputs.previous_tds)} already deducted there."
        )

    def _line(item: tuple[str, str, Decimal, str, str]) -> dict[str, str]:
        code, label, amount, source, basis = item
        return {
            "code": code,
            "label": label,
            "amount": str(_money(amount)),
            "source": source,
            "basis": basis,
        }

    computation.other_income = [_line(item) for item in inputs.other_income]
    computation.pending_if_unclaimed = [_line(item) for item in inputs.pending_if_unclaimed]
    if other_total > ZERO:
        computation.notes.append(
            f"Includes {_money(other_total)} of taxable income from outside the salary "
            "structure: " + "; ".join(label for _, label, _, _, _ in inputs.other_income) + "."
        )
    computation.notes.extend(inputs.income_notes)

    # ── Exemptions ──────────────────────────────────────────────────────────
    exemptions = inputs.previous_exempt

    if "HRA" in allowed_sections and inputs.hra_received > ZERO:
        hra = compute_hra_exemption(
            hra_received=inputs.hra_received,
            basic_plus_da=inputs.basic_plus_da,
            annual_rent=inputs.annual_rent,
            is_metro=inputs.is_metro,
            rules=rules,
        )
        computation.hra = hra
        exemptions += hra.exemption
    elif inputs.hra_received > ZERO:
        computation.notes.append(
            f"HRA exemption is not available under the {inputs.regime} regime, so the "
            "HRA component is fully taxable."
        )

    computation.total_exemptions = _money(exemptions)

    # ── Standard deduction ──────────────────────────────────────────────────
    standard = _decimal(regime_rules.get("standard_deduction", 0))
    deductions = standard
    if standard > ZERO:
        computation.deduction_lines.append(
            DeductionLine(
                section="Standard deduction",
                declared=standard,
                accepted=standard,
                allowed=standard,
                ceiling=standard,
                capped=False,
                note="Applied automatically under this regime.",
            )
        )

    # ── Declared deductions ─────────────────────────────────────────────────
    # Grouped by section first, so a ceiling applies once across every line in
    # that section rather than once per line — three 80C investments of 100,000
    # each do not give a 300,000 deduction.
    by_section: dict[str, list[tuple[Decimal, Decimal]]] = {}
    for section, declared, accepted in inputs.declarations:
        by_section.setdefault(section, []).append((declared, accepted))

    for section, lines in sorted(by_section.items()):
        declared_total = sum((d for d, _ in lines), start=ZERO)
        accepted_total = sum((a for _, a in lines), start=ZERO)

        if section not in allowed_sections:
            computation.deduction_lines.append(
                DeductionLine(
                    section=section,
                    declared=_money(declared_total),
                    accepted=_money(accepted_total),
                    allowed=ZERO,
                    ceiling=None,
                    capped=False,
                    note=f"Not available under the {inputs.regime} regime.",
                )
            )
            continue

        allowed, ceiling = apply_section_ceiling(section, accepted_total, rules, age=inputs.age)
        deductions += allowed

        note = ""
        if accepted_total < declared_total:
            note = (
                f"Declared {_money(declared_total)}, proved and accepted {_money(accepted_total)}."
            )
        if ceiling is not None and accepted_total > ceiling:
            note = (note + " " if note else "") + f"Capped at the {section} limit."

        computation.deduction_lines.append(
            DeductionLine(
                section=section,
                declared=_money(declared_total),
                accepted=_money(accepted_total),
                allowed=_money(allowed),
                ceiling=ceiling,
                capped=ceiling is not None and accepted_total > ceiling,
                note=note.strip(),
            )
        )

    # Professional tax, where the regime allows it.
    if "PT" in allowed_sections and inputs.professional_tax > ZERO:
        deductions += inputs.professional_tax
        computation.deduction_lines.append(
            DeductionLine(
                section="PT",
                declared=_money(inputs.professional_tax),
                accepted=_money(inputs.professional_tax),
                allowed=_money(inputs.professional_tax),
                ceiling=None,
                capped=False,
                note="Professional tax paid.",
            )
        )

    # Employer NPS, which both regimes typically allow.
    if "80CCD2" in allowed_sections and inputs.employer_nps > ZERO:
        allowed, ceiling = apply_section_ceiling("80CCD2", inputs.employer_nps, rules)
        deductions += allowed
        computation.deduction_lines.append(
            DeductionLine(
                section="80CCD2",
                declared=_money(inputs.employer_nps),
                accepted=_money(inputs.employer_nps),
                allowed=_money(allowed),
                ceiling=ceiling,
                capped=ceiling is not None and inputs.employer_nps > ceiling,
                note="Employer contribution to NPS.",
            )
        )

    computation.total_deductions = _money(deductions)

    taxable = max(ZERO, gross - exemptions - deductions)
    computation.taxable_income = _money(taxable)

    tax, rebate, surcharge, cess, breakdown = compute_annual_tax(
        taxable_income=computation.taxable_income, regime=inputs.regime, rules=rules
    )
    computation.tax_before_rebate = tax
    computation.rebate = rebate
    computation.surcharge = surcharge
    computation.cess = cess
    computation.slab_breakdown = breakdown
    computation.annual_tax_liability = _money(max(ZERO, tax - rebate) + surcharge + cess)

    return computation


# ── TDS levelling ───────────────────────────────────────────────────────────


def monthly_tds(
    *,
    annual_liability: Decimal,
    already_deducted: Decimal,
    months_remaining: int,
    previous_employer_tds: Decimal = ZERO,
) -> Decimal:
    """Spread the residual liability across the months that remain.

    Not a flat twelfth. Each month reprojects from what is currently known and
    divides what is left by the months left — which is why a proof verified in
    January reduces February and March rather than being carried to next year.

    A negative residual (over-deducted, usually after a late proof) returns
    zero rather than a refund: refunds are settled at filing, and a negative
    TDS line on a payslip is not a thing.
    """
    if months_remaining <= 0:
        raise TaxRuleError("Months remaining must be at least one")

    residual = annual_liability - already_deducted - previous_employer_tds
    if residual <= ZERO:
        return ZERO

    return _money(residual / Decimal(months_remaining))


def months_remaining_in_year(current_month: int, fy_start_month: int) -> int:
    """Months left in the financial year, inclusive of the current one.

    `fy_start_month` is configuration — April in India, but the tenant setting
    exists because a group company with a foreign parent may run a different
    year.
    """
    offset = (current_month - fy_start_month) % MONTHS_IN_YEAR
    return MONTHS_IN_YEAR - offset


def compare_regimes(inputs: ProjectionInput, rules: dict[str, Any]) -> dict[str, Any]:
    """Project under both regimes, so an employee can choose with their own numbers.

    Most employees cannot make this choice unaided, and every one who guesses
    wrong becomes a support ticket in March. Showing both is cheaper than
    answering the ticket.
    """
    from dataclasses import replace

    results: dict[str, TaxComputation] = {}
    for regime in _require(rules, "regimes"):
        results[regime] = project(replace(inputs, regime=regime), rules)

    best = min(results, key=lambda name: results[name].annual_tax_liability)
    saving = (
        max(r.annual_tax_liability for r in results.values()) - results[best].annual_tax_liability
    )

    return {
        "regimes": {name: result.as_working() for name, result in results.items()},
        "lower_liability_regime": best,
        "saving": str(_money(saving)),
        # Deliberately worded as an observation, not advice. A projection is
        # not tax advice, and an employee's circumstances may make the higher
        # projection the right choice.
        "note": (
            f"On the figures currently declared, the {best} regime projects a lower "
            f"liability by {_money(saving)}. This is a projection from your own "
            "declarations, not tax advice."
        ),
    }
