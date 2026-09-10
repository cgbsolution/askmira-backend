"""The CTC resolution engine.

Given an annual CTC and a salary structure, resolve every component to an exact
amount. Every downstream payroll capability — arrears, gratuity, IT
declarations, full and final — reads the output of this module, so it is the
part of payroll where correctness is least negotiable.

═══════════════════════════════════════════════════════════════════════════════
ROUNDING POLICY
═══════════════════════════════════════════════════════════════════════════════

Rounding is a real source of payroll disputes, so the policy is stated rather
than left to emerge from the arithmetic:

1. Every component is resolved at full `Decimal` precision first. No rounding
   happens mid-calculation — a PERCENTAGE_OF that feeds another PERCENTAGE_OF
   uses the unrounded parent, so rounding error cannot compound down a chain.

2. Each non-BALANCE component is then rounded once, to the structure's
   `rounding_precision` using its `rounding_mode` (default: 0 places,
   ROUND_HALF_UP — whole rupees, which is what an employee checking the
   arithmetic by hand expects).

3. The BALANCE component absorbs the residual: it is computed last as
   `CTC - sum(every other rounded in-CTC component)`. This is what makes the
   breakdown sum to the CTC *exactly*, to the paisa, rather than approximately.

4. That exactness is asserted, not assumed. A drift of even one rupee raises
   `SalaryStructureError` rather than being silently absorbed.

5. Monthly amounts are derived as `annual / 12`, rounded the same way, with the
   monthly residual reconciled into BALANCE so the monthly column also sums
   exactly to `round(CTC / 12)`. Because both columns are independently
   reconciled, `12 x monthly` may differ from `annual` by a few rupees. The
   annual figure is the contractual one; the payroll engine reconciles the
   difference in the final run of the financial year. This is deliberate and
   is what Indian payroll conventionally does.

Components with `part_of_ctc = False` (typically reimbursements) resolve
normally but are excluded from the CTC sum and from the BALANCE residual.
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal

from app.models.payroll import (
    CalculationType,
    ComponentType,
    RoundingMode,
    SalaryStructure,
    SalaryStructureComponent,
)
from app.services.payroll.formula import FormulaError, evaluate_formula, referenced_components

__all__ = [
    "ResolvedComponent",
    "SalaryBreakdown",
    "SalaryStructureError",
    "resolve_ctc",
    "validate_structure",
]

_ROUNDING = {
    RoundingMode.HALF_UP: ROUND_HALF_UP,
    RoundingMode.HALF_EVEN: ROUND_HALF_EVEN,
    RoundingMode.CEILING: ROUND_CEILING,
    RoundingMode.FLOOR: ROUND_FLOOR,
}

_MONTHS = Decimal(12)

# A reserved code meaning "the CTC being resolved". It is the input to
# resolution rather than a component, so it is always available and never
# participates in the dependency graph. A tenant may not define a component
# with this code — `validate_structure` rejects it.
CTC_TOKEN = "CTC"


class SalaryStructureError(ValueError):
    """A structure is invalid, or a resolution failed to reconcile."""


@dataclass(frozen=True, slots=True)
class ComponentSpec:
    """A component flattened with its per-structure overrides already applied.

    Resolution works on these rather than on ORM rows so the engine can be
    tested without a database, and so the override rules are resolved in
    exactly one place.
    """

    code: str
    name: str
    component_type: ComponentType
    calculation_type: CalculationType
    base_component_code: str | None
    value: Decimal | None
    formula: str | None
    taxable: bool
    part_of_ctc: bool
    pro_rate_on_lop: bool
    counts_toward_pf: bool
    counts_toward_esi: bool
    display_order: int


@dataclass(frozen=True, slots=True)
class ResolvedComponent:
    code: str
    name: str
    component_type: ComponentType
    annual_amount: Decimal
    monthly_amount: Decimal
    taxable: bool
    part_of_ctc: bool
    pro_rate_on_lop: bool
    counts_toward_pf: bool
    counts_toward_esi: bool
    display_order: int


@dataclass(frozen=True, slots=True)
class SalaryBreakdown:
    ctc_annual: Decimal
    components: list[ResolvedComponent] = field(default_factory=list)

    @property
    def annual_total_in_ctc(self) -> Decimal:
        return sum(
            (c.annual_amount for c in self.components if c.part_of_ctc), start=Decimal(0)
        )

    @property
    def monthly_total_in_ctc(self) -> Decimal:
        return sum(
            (c.monthly_amount for c in self.components if c.part_of_ctc), start=Decimal(0)
        )

    def by_code(self, code: str) -> ResolvedComponent | None:
        return next((c for c in self.components if c.code == code), None)

    def gross_monthly_earnings(self) -> Decimal:
        """Monthly earnings — what proration and statutory bases start from."""
        return sum(
            (
                c.monthly_amount
                for c in self.components
                if c.component_type is ComponentType.EARNING
            ),
            start=Decimal(0),
        )


# ── Flattening ──────────────────────────────────────────────────────────────


def _flatten(link: SalaryStructureComponent) -> ComponentSpec:
    """Apply a structure's overrides over the component's own definition."""
    component = link.component
    return ComponentSpec(
        code=component.code,
        name=component.name,
        component_type=component.component_type,
        calculation_type=component.calculation_type,
        base_component_code=link.override_base_component_code or component.base_component_code,
        value=(
            Decimal(str(link.override_value))
            if link.override_value is not None
            else (Decimal(str(component.value)) if component.value is not None else None)
        ),
        formula=link.override_formula or component.formula,
        taxable=component.taxable,
        part_of_ctc=component.part_of_ctc,
        pro_rate_on_lop=component.pro_rate_on_lop,
        counts_toward_pf=component.counts_toward_pf,
        counts_toward_esi=component.counts_toward_esi,
        display_order=link.display_order or component.display_order,
    )


def _specs(structure: SalaryStructure) -> list[ComponentSpec]:
    return [_flatten(link) for link in structure.components]


# ── Dependency graph ────────────────────────────────────────────────────────


def _dependencies(spec: ComponentSpec, known: set[str]) -> set[str]:
    """What this component must be resolved after.

    `CTC` is a reserved pseudo-component: it is the input to resolution, always
    available, and never a graph node. A percentage of CTC therefore has no
    dependencies at all.

    BALANCE depends on everything in the CTC, but that is handled by resolving
    it last rather than by an edge per component — an edge set that large makes
    a cycle error unreadable.
    """
    if spec.calculation_type is CalculationType.PERCENTAGE_OF:
        if not spec.base_component_code:
            raise SalaryStructureError(
                f"Component '{spec.code}' is a percentage but names no base component"
            )
        if spec.base_component_code.upper() == CTC_TOKEN:
            return set()
        if spec.base_component_code not in known:
            raise SalaryStructureError(
                f"Component '{spec.code}' is a percentage of "
                f"'{spec.base_component_code}', which is not in this structure"
            )
        return {spec.base_component_code}

    if spec.calculation_type is CalculationType.FORMULA:
        if not spec.formula:
            raise SalaryStructureError(f"Component '{spec.code}' is a formula but has none set")
        try:
            referenced = referenced_components(spec.formula) - {CTC_TOKEN}
        except FormulaError as exc:
            raise SalaryStructureError(f"Component '{spec.code}': {exc}") from exc
        unknown = referenced - known
        if unknown:
            raise SalaryStructureError(
                f"Component '{spec.code}' references "
                f"{', '.join(sorted(unknown))}, which are not in this structure"
            )
        return referenced

    return set()


def _topological_order(specs: list[ComponentSpec]) -> list[ComponentSpec]:
    """Order components so every dependency resolves before its dependents.

    Depth-first with an explicit path, so a cycle can be reported as the actual
    chain — "BASIC -> HRA -> BASIC" — rather than as a RecursionError. Naming
    the cycle is the difference between an HR admin fixing their configuration
    and filing a support ticket.
    """
    by_code = {spec.code: spec for spec in specs}
    known = set(by_code)
    edges = {spec.code: _dependencies(spec, known) for spec in specs}

    ordered: list[ComponentSpec] = []
    visited: set[str] = set()
    visiting: list[str] = []

    def visit(code: str) -> None:
        if code in visited:
            return
        if code in visiting:
            cycle = [*visiting[visiting.index(code) :], code]
            raise SalaryStructureError(
                f"Circular reference between salary components: {' -> '.join(cycle)}"
            )
        visiting.append(code)
        # Sorted for a deterministic order, so the same structure always
        # produces the same breakdown and the same cycle message.
        for dependency in sorted(edges[code]):
            visit(dependency)
        visiting.pop()
        visited.add(code)
        ordered.append(by_code[code])

    for spec in sorted(specs, key=lambda s: (s.display_order, s.code)):
        visit(spec.code)

    return ordered


# ── Validation ──────────────────────────────────────────────────────────────


def validate_structure(structure: SalaryStructure) -> None:
    """Reject a structure that cannot resolve, at save time rather than at run time.

    Raises:
        SalaryStructureError: on a duplicate code, a missing or ambiguous
            BALANCE, a dangling reference, a malformed formula, or a cycle.
    """
    specs = _specs(structure)
    if not specs:
        raise SalaryStructureError("A salary structure needs at least one component")

    codes = [spec.code for spec in specs]
    if CTC_TOKEN in codes:
        raise SalaryStructureError(
            f"'{CTC_TOKEN}' is a reserved code meaning the CTC being resolved. "
            "Rename the component."
        )
    duplicates = {code for code in codes if codes.count(code) > 1}
    if duplicates:
        raise SalaryStructureError(
            f"Component code(s) appear more than once: {', '.join(sorted(duplicates))}"
        )

    balances = [s.code for s in specs if s.calculation_type is CalculationType.BALANCE]
    if len(balances) == 0:
        raise SalaryStructureError(
            "A salary structure needs exactly one BALANCE component to absorb the "
            "residual so the breakdown sums to CTC. This structure has none."
        )
    if len(balances) > 1:
        raise SalaryStructureError(
            "A salary structure may have only one BALANCE component; this one has "
            f"{len(balances)}: {', '.join(sorted(balances))}. With more than one the "
            "residual cannot be attributed."
        )

    balance_spec = next(s for s in specs if s.calculation_type is CalculationType.BALANCE)
    if not balance_spec.part_of_ctc:
        raise SalaryStructureError(
            f"The BALANCE component '{balance_spec.code}' must be part of CTC — "
            "it is the residual of the CTC by definition."
        )

    for spec in specs:
        if spec.calculation_type is CalculationType.FIXED and spec.value is None:
            raise SalaryStructureError(f"Component '{spec.code}' is fixed but has no value")
        if spec.calculation_type is CalculationType.PERCENTAGE_OF and spec.value is None:
            raise SalaryStructureError(
                f"Component '{spec.code}' is a percentage but has no percentage set"
            )

    if structure.rounding_precision < 0 or structure.rounding_precision > 2:
        raise SalaryStructureError("Rounding precision must be 0, 1 or 2 decimal places")

    # Raises on a cycle or a dangling reference.
    _topological_order(specs)


# ── Resolution ──────────────────────────────────────────────────────────────


def _quantum(precision: int) -> Decimal:
    return Decimal(1).scaleb(-precision)


def resolve_ctc(structure: SalaryStructure, ctc_annual: Decimal) -> SalaryBreakdown:
    """Resolve an annual CTC against a structure into exact component amounts.

    Args:
        structure: the template, with its components loaded.
        ctc_annual: the annual cost to company.

    Returns:
        A breakdown whose in-CTC annual amounts sum to `ctc_annual` exactly.

    Raises:
        SalaryStructureError: if the structure is invalid, or if reconciliation
            fails — which should be impossible and is asserted rather than
            trusted.
    """
    if not isinstance(ctc_annual, Decimal):
        # A float here would defeat the entire point of this module.
        raise SalaryStructureError("CTC must be a Decimal, not a float")
    if ctc_annual <= 0:
        raise SalaryStructureError("CTC must be greater than zero")

    validate_structure(structure)

    specs = _specs(structure)
    ordered = _topological_order(specs)
    rounding = _ROUNDING[structure.rounding_mode]
    quantum = _quantum(structure.rounding_precision)

    # A CTC finer than the structure's rounding cannot be represented exactly.
    # 750000.50 in a whole-rupee structure has nowhere to put the 50 paise:
    # BALANCE rounds like every other component, so the residual would be lost.
    # Refuse it here with a message the HR admin can act on, rather than
    # letting it surface later as a reconciliation failure.
    if ctc_annual != ctc_annual.quantize(quantum, rounding=rounding):
        places = structure.rounding_precision
        unit = "whole rupees" if places == 0 else f"{places} decimal place(s)"
        raise SalaryStructureError(
            f"CTC {ctc_annual} is finer than this structure's rounding ({unit}). "
            f"Either round the CTC or raise the structure's rounding precision."
        )

    # ── Pass 1: resolve at full precision, no rounding ──────────────────────
    exact: dict[str, Decimal] = {}
    balance_spec: ComponentSpec | None = None

    for spec in ordered:
        if spec.calculation_type is CalculationType.BALANCE:
            balance_spec = spec
            continue

        if spec.calculation_type is CalculationType.FIXED:
            amount = spec.value or Decimal(0)

        elif spec.calculation_type is CalculationType.PERCENTAGE_OF:
            base_code = spec.base_component_code or ""
            base = (
                ctc_annual if base_code.upper() == CTC_TOKEN else exact.get(base_code)
            )
            if base is None:
                raise SalaryStructureError(
                    f"Component '{spec.code}' resolves before its base '{base_code}'. "
                    "This is an ordering bug, not a configuration error."
                )
            amount = base * (spec.value or Decimal(0)) / Decimal(100)

        elif spec.calculation_type is CalculationType.FORMULA:
            try:
                amount = evaluate_formula(spec.formula or "", {**exact, CTC_TOKEN: ctc_annual})
            except FormulaError as exc:
                raise SalaryStructureError(f"Component '{spec.code}': {exc}") from exc

        else:  # pragma: no cover — the enum has no other members
            raise SalaryStructureError(f"Unknown calculation type for '{spec.code}'")

        if amount < 0:
            raise SalaryStructureError(
                f"Component '{spec.code}' resolved to a negative amount ({amount}). "
                "Check the formula or percentage."
            )
        exact[spec.code] = amount

    if balance_spec is None:  # pragma: no cover — validate_structure guarantees one
        raise SalaryStructureError("No BALANCE component after validation")

    # ── Pass 2: round each component once ───────────────────────────────────
    rounded_annual = {
        code: amount.quantize(quantum, rounding=rounding) for code, amount in exact.items()
    }

    # ── Pass 3: BALANCE absorbs the annual residual ─────────────────────────
    in_ctc_codes = {spec.code for spec in specs if spec.part_of_ctc}
    allocated_annual = sum(
        (amount for code, amount in rounded_annual.items() if code in in_ctc_codes),
        start=Decimal(0),
    )
    balance_annual = (ctc_annual - allocated_annual).quantize(quantum, rounding=rounding)

    if balance_annual < 0:
        raise SalaryStructureError(
            f"The components in this structure total {allocated_annual}, which exceeds the "
            f"CTC of {ctc_annual}. The BALANCE component '{balance_spec.code}' would be "
            f"negative ({balance_annual}). Reduce a fixed component or raise the CTC."
        )
    rounded_annual[balance_spec.code] = balance_annual

    # ── Pass 4: monthly, reconciled the same way ────────────────────────────
    target_monthly = (ctc_annual / _MONTHS).quantize(quantum, rounding=rounding)
    rounded_monthly = {
        code: (amount / _MONTHS).quantize(quantum, rounding=rounding)
        for code, amount in rounded_annual.items()
        if code != balance_spec.code
    }
    allocated_monthly = sum(
        (amount for code, amount in rounded_monthly.items() if code in in_ctc_codes),
        start=Decimal(0),
    )
    rounded_monthly[balance_spec.code] = (target_monthly - allocated_monthly).quantize(
        quantum, rounding=rounding
    )

    by_code = {spec.code: spec for spec in specs}
    components = [
        ResolvedComponent(
            code=spec.code,
            name=spec.name,
            component_type=spec.component_type,
            annual_amount=rounded_annual[spec.code],
            monthly_amount=rounded_monthly[spec.code],
            taxable=spec.taxable,
            part_of_ctc=spec.part_of_ctc,
            pro_rate_on_lop=spec.pro_rate_on_lop,
            counts_toward_pf=spec.counts_toward_pf,
            counts_toward_esi=spec.counts_toward_esi,
            display_order=spec.display_order,
        )
        for spec in sorted(by_code.values(), key=lambda s: (s.display_order, s.code))
    ]

    breakdown = SalaryBreakdown(ctc_annual=ctc_annual, components=components)

    # ── The assertion the whole module exists to make ───────────────────────
    # A drift of even one rupee fails loudly rather than being absorbed.
    if breakdown.annual_total_in_ctc != ctc_annual:
        raise SalaryStructureError(
            f"Reconciliation failed: components sum to {breakdown.annual_total_in_ctc} "
            f"but CTC is {ctc_annual} (difference "
            f"{ctc_annual - breakdown.annual_total_in_ctc}). This is a bug in the "
            "resolution engine, not in the structure."
        )

    return breakdown
