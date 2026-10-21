"""Statutory registers.

Every register an Indian employer has to maintain has a **prescribed format**:
named columns, in a fixed order, under a fixed heading, with the rule that
prescribes it. An inspector comparing a printout against the rule is comparing
column names — a register that carries the right numbers under the wrong
headings is a defective register.

So the formats live here as data (`RegisterSpec`), one spec per register, each
naming the rule it comes from. Rendering is one generic function over a spec.
Building each register's table by hand is how columns drift out of prescribed
order, and how a column quietly gets renamed to whatever the developer found
clearer.

The column *names* are prescribed by law and so are written out here. The
*values* — rates, ceilings, amounts — are never in this file; they come from
the computed records, which came from SME-approved rule sets.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

__all__ = [
    "REGISTERS",
    "RegisterColumn",
    "RegisterError",
    "RegisterSpec",
    "RenderedRegister",
    "render_register",
    "to_csv",
]


class RegisterError(ValueError):
    """A register cannot be produced as prescribed."""


@dataclass(frozen=True, slots=True)
class RegisterColumn:
    """One prescribed column.

    `heading` is the wording from the rule and is not ours to shorten.
    `source` reads it out of a row dict.
    """

    heading: str
    source: str
    align: str = "left"


@dataclass(frozen=True, slots=True)
class RegisterSpec:
    code: str
    title: str
    # The rule that prescribes this format, printed on the register itself so
    # the reader can check it against the source.
    prescribed_by: str
    columns: tuple[RegisterColumn, ...]
    # Columns whose values are summed into a totals row.
    total_columns: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


# ═══════════════════════════════════════════════════════════════════════════
# The prescribed formats
# ═══════════════════════════════════════════════════════════════════════════

BONUS_FORM_C = RegisterSpec(
    code="BONUS_FORM_C",
    title="Register showing the details of the amount of bonus due to each employee",
    prescribed_by="Payment of Bonus Rules, 1975 - Form C",
    columns=(
        RegisterColumn("S. No.", "serial"),
        RegisterColumn("Name of the employee", "employee_name"),
        RegisterColumn("Father / Husband name", "guardian_name"),
        RegisterColumn(
            "Whether he has completed 15 years of age at the beginning of the accounting year",
            "completed_fifteen",
        ),
        RegisterColumn("Designation", "designation"),
        RegisterColumn("Number of days worked in the year", "days_worked", "right"),
        RegisterColumn(
            "Total salary or wage in respect of the accounting year", "annual_wage", "right"
        ),
        RegisterColumn("Amount of bonus payable", "bonus_payable", "right"),
        RegisterColumn(
            "Deductions - Puja bonus or other customary bonus paid during the accounting year",
            "customary_bonus",
            "right",
        ),
        RegisterColumn("Deductions - Amount of bonus paid in advance", "advance_bonus", "right"),
        RegisterColumn(
            "Deductions - Amount of loss caused by misconduct", "misconduct_loss", "right"
        ),
        RegisterColumn("Net amount payable", "net_payable", "right"),
        RegisterColumn("Amount actually paid", "amount_paid", "right"),
        RegisterColumn("Date on which paid", "paid_on"),
        RegisterColumn("Signature or thumb impression of the employee", "signature"),
    ),
    total_columns=("annual_wage", "bonus_payable", "net_payable", "amount_paid"),
    notes=(
        "The accounting year is the financial year the bonus relates to, not the "
        "year it was paid in.",
    ),
)

LWF_REGISTER = RegisterSpec(
    code="LWF_REGISTER",
    title="Register of Labour Welfare Fund contributions",
    prescribed_by="State Labour Welfare Fund Rules - format varies by state",
    columns=(
        RegisterColumn("S. No.", "serial"),
        RegisterColumn("Name of the employee", "employee_name"),
        RegisterColumn("Designation", "designation"),
        RegisterColumn("Wages for the period", "wages", "right"),
        RegisterColumn("Employee contribution", "employee_contribution", "right"),
        RegisterColumn("Employer contribution", "employer_contribution", "right"),
        RegisterColumn("Total contribution", "total_contribution", "right"),
        RegisterColumn("Period of deduction", "period"),
    ),
    total_columns=("employee_contribution", "employer_contribution", "total_contribution"),
    notes=(
        "The prescribed columns differ between states. The state's own format, as "
        "confirmed by the Compliance SME, governs - this is the common shape.",
    ),
)

PF_CONTRIBUTION_REGISTER = RegisterSpec(
    code="PF_REGISTER",
    title="Register of Provident Fund contributions",
    prescribed_by="Employees Provident Funds Scheme, 1952 - Form 3A basis",
    columns=(
        RegisterColumn("S. No.", "serial"),
        RegisterColumn("UAN", "uan"),
        RegisterColumn("Name of the member", "employee_name"),
        RegisterColumn("Wages on which contributions are payable", "pf_wage_base", "right"),
        RegisterColumn("Worker contribution", "employee_contribution", "right"),
        RegisterColumn("Voluntary contribution", "voluntary_contribution", "right"),
        RegisterColumn("Employer contribution", "employer_contribution", "right"),
        RegisterColumn("Month", "period"),
    ),
    total_columns=(
        "pf_wage_base",
        "employee_contribution",
        "voluntary_contribution",
        "employer_contribution",
    ),
)

ESI_CONTRIBUTION_REGISTER = RegisterSpec(
    code="ESI_REGISTER",
    title="Register of Employees State Insurance contributions",
    prescribed_by=("Employees State Insurance (General) Regulations, 1950 - Register of Employees"),
    columns=(
        RegisterColumn("S. No.", "serial"),
        RegisterColumn("Insurance number", "esi_number"),
        RegisterColumn("Name of the insured person", "employee_name"),
        RegisterColumn("Number of days for which wages paid", "paid_days", "right"),
        RegisterColumn("Total wages paid", "esi_wage_base", "right"),
        RegisterColumn("Employee contribution", "employee_contribution", "right"),
        RegisterColumn("Employer contribution", "employer_contribution", "right"),
        RegisterColumn("Contribution period", "contribution_period"),
    ),
    total_columns=("esi_wage_base", "employee_contribution", "employer_contribution"),
    notes=(
        "An employee whose wages crossed the ceiling mid-period still appears here "
        "for the remaining months of that contribution period.",
    ),
)

PROFESSIONAL_TAX_REGISTER = RegisterSpec(
    code="PT_REGISTER",
    title="Register of Professional Tax deducted",
    prescribed_by="State Professions Tax Acts - format varies by state",
    columns=(
        RegisterColumn("S. No.", "serial"),
        RegisterColumn("Name of the employee", "employee_name"),
        RegisterColumn("State of employment", "state"),
        RegisterColumn("Monthly salary or wage", "gross_earnings", "right"),
        RegisterColumn("Tax deducted", "tax_deducted", "right"),
        RegisterColumn("Month", "period"),
    ),
    total_columns=("tax_deducted",),
    notes=(
        "Employees whose work state could not be established are listed separately "
        "rather than filed under a guessed state.",
    ),
)

REGISTERS: dict[str, RegisterSpec] = {
    spec.code: spec
    for spec in (
        BONUS_FORM_C,
        LWF_REGISTER,
        PF_CONTRIBUTION_REGISTER,
        ESI_CONTRIBUTION_REGISTER,
        PROFESSIONAL_TAX_REGISTER,
    )
}


# ═══════════════════════════════════════════════════════════════════════════
# Rendering
# ═══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class RenderedRegister:
    spec: RegisterSpec
    headings: list[str]
    rows: list[list[str]]
    totals: dict[str, Decimal]
    heading_lines: list[str]
    row_count: int
    excluded: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.spec.code,
            "title": self.spec.title,
            "prescribed_by": self.spec.prescribed_by,
            "heading_lines": self.heading_lines,
            "headings": self.headings,
            "rows": self.rows,
            "totals": {key: str(value) for key, value in self.totals.items()},
            "row_count": self.row_count,
            "notes": list(self.spec.notes),
            "excluded": self.excluded,
        }


def render_register(
    spec: RegisterSpec,
    rows: Sequence[dict[str, Any]],
    *,
    heading_lines: Sequence[str] = (),
    row_filter: Callable[[dict[str, Any]], bool] | None = None,
) -> RenderedRegister:
    """Render rows against a prescribed format.

    Every column in the spec appears, in the spec's order, whether or not the
    data has a value for it — a register missing a prescribed column is
    defective even when the column would be empty. A missing value renders as
    an empty cell, and columns like the signature column are expected to be
    empty on a register that is about to be signed.
    """
    excluded: list[str] = []
    included: list[dict[str, Any]] = []
    for row in rows:
        if row_filter is not None and not row_filter(row):
            excluded.append(str(row.get("employee_name") or row.get("serial") or "?"))
            continue
        included.append(row)

    rendered: list[list[str]] = []
    totals: dict[str, Decimal] = {key: Decimal(0) for key in spec.total_columns}

    for index, row in enumerate(included, start=1):
        cells: list[str] = []
        for column in spec.columns:
            if column.source == "serial" and "serial" not in row:
                value: Any = index
            else:
                value = row.get(column.source)
            cells.append(_cell(value))
            if column.source in totals and value is not None:
                totals[column.source] += _as_decimal(value, column.heading)
        rendered.append(cells)

    return RenderedRegister(
        spec=spec,
        headings=[column.heading for column in spec.columns],
        rows=rendered,
        totals=totals,
        heading_lines=list(heading_lines),
        row_count=len(rendered),
        excluded=excluded,
    )


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


def _as_decimal(value: Any, heading: str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        raise RegisterError(
            f"The column '{heading}' carries a float. Register money is Decimal, so "
            "that a total footed by an inspector matches the one printed."
        )
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError) as exc:
        raise RegisterError(
            f"The column '{heading}' is totalled but carries a non-numeric value {value!r}."
        ) from exc


def to_csv(rendered: RenderedRegister) -> str:
    """The register as CSV, headings and totals included.

    A register is filed and inspected, so the heading block — which
    establishment, which period, under which rule — travels with the table.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")

    writer.writerow([rendered.spec.title])
    writer.writerow([f"Prescribed by: {rendered.spec.prescribed_by}"])
    for line in rendered.heading_lines:
        writer.writerow([line])
    writer.writerow([])

    writer.writerow(rendered.headings)
    writer.writerows(rendered.rows)

    if rendered.totals:
        totals_row: list[str] = []
        for position, column in enumerate(rendered.spec.columns):
            if column.source in rendered.totals:
                totals_row.append(str(rendered.totals[column.source]))
            elif position == 0:
                totals_row.append("Total")
            else:
                totals_row.append("")
        writer.writerow(totals_row)

    for note in rendered.spec.notes:
        writer.writerow([f"Note: {note}"])

    return buffer.getvalue()
