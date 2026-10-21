"""The full and final computation.

═══════════════════════════════════════════════════════════════════════════════
THE THING THIS MODULE REFUSES TO DO
═══════════════════════════════════════════════════════════════════════════════

Floor the net at zero.

An employee who resigned without notice, has an outstanding loan and did not
return a laptop owes the company money. Clamping that to zero writes off a debt
nobody decided to write off, and — worse — tells the employee they were owed
nothing when a demand was due. `SettlementResult.net_amount` is signed, and
`is_demand` says which way it went.
═══════════════════════════════════════════════════════════════════════════════

Every line carries the basis it was computed on. The employee reads this
statement on the day they lose access to anyone who could explain it, so
"Notice shortfall recovery, 45,000" is not enough — it has to say "15 days at
3,000 per day on basic + DA".

This module is pure arithmetic over inputs the caller has gathered. It does not
query, and it does not know which slice each figure came from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

__all__ = [
    "SettlementError",
    "SettlementInput",
    "SettlementResult",
    "StatementLine",
    "compute_settlement",
    "notice_shortfall_recovery",
]

ZERO = Decimal(0)
RUPEE = Decimal("0.01")


class SettlementError(ValueError):
    """A settlement cannot be computed from what has been supplied."""


def _money(value: Decimal) -> Decimal:
    return value.quantize(RUPEE, rounding=ROUND_HALF_UP)


def _check(name: str, value: Decimal, *, allow_negative: bool = False) -> Decimal:
    if not isinstance(value, Decimal):
        raise SettlementError(f"{name} must be a Decimal, not a float")
    if value < ZERO and not allow_negative:
        raise SettlementError(
            f"{name} is {value}. Recoveries are recorded as positive amounts under "
            "their own line type — a negative earning is ambiguous on a statement the "
            "employee has to read."
        )
    return value


@dataclass(frozen=True, slots=True)
class StatementLine:
    """One line, and why it says what it says."""

    line_type: str  # earning | recovery | tax
    code: str
    label: str
    amount: Decimal
    basis: str
    source: str | None = None
    display_order: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "line_type": self.line_type,
            "code": self.code,
            "label": self.label,
            "amount": str(self.amount),
            "basis": self.basis,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class SettlementInput:
    """Everything the settlement is built from.

    Each figure comes from the slice that owns it — gratuity and encashment
    from slice 08, arrears from slice 12, loans from slice 14 — and arrives
    here with the basis string that slice produced. Recomputing any of them
    would risk a settlement that disagrees with the record it came from.
    """

    # ── Earnings ────────────────────────────────────────────────────────────
    final_month_salary: Decimal = ZERO
    final_month_basis: str = ""
    leave_encashment: Decimal = ZERO
    leave_encashment_basis: str = ""
    gratuity: Decimal = ZERO
    gratuity_basis: str = ""
    pending_reimbursements: Decimal = ZERO
    reimbursement_basis: str = ""
    pro_rata_bonus: Decimal = ZERO
    bonus_basis: str = ""
    pending_arrears: Decimal = ZERO
    arrears_basis: str = ""

    # ── Recoveries, all positive ────────────────────────────────────────────
    notice_shortfall: Decimal = ZERO
    notice_basis: str = ""
    loan_outstanding: Decimal = ZERO
    loan_basis: str = ""
    asset_recovery: Decimal = ZERO
    asset_basis: str = ""
    excess_leave_taken: Decimal = ZERO
    excess_leave_basis: str = ""
    other_recoveries: Decimal = ZERO
    other_recovery_basis: str = ""

    # ── Tax ─────────────────────────────────────────────────────────────────
    # Recomputed at exit against actual figures rather than a projection. It is
    # frequently a refund, which is why it is signed: a negative value is money
    # back to the employee and appears on the statement as such.
    final_tds: Decimal = ZERO
    tax_basis: str = ""
    tax_working: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SettlementResult:
    total_earnings: Decimal
    total_recoveries: Decimal
    total_tax: Decimal
    net_amount: Decimal
    lines: list[StatementLine] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def is_demand(self) -> bool:
        """The company is owed, rather than owing."""
        return self.net_amount < ZERO

    @property
    def amount_due_from_employee(self) -> Decimal:
        """What to put on the demand. Zero when the settlement is payable."""
        return -self.net_amount if self.is_demand else ZERO

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_earnings": str(self.total_earnings),
            "total_recoveries": str(self.total_recoveries),
            "total_tax": str(self.total_tax),
            "net_amount": str(self.net_amount),
            "is_demand": self.is_demand,
            "amount_due_from_employee": str(self.amount_due_from_employee),
            "lines": [line.as_dict() for line in self.lines],
            "notes": self.notes,
        }


# The statement's order. Earnings first, then recoveries, then tax, because
# that is the order the reader will check them in.
_EARNINGS = (
    ("SALARY_FINAL", "Salary for the final month", "final_month_salary", "final_month_basis"),
    ("LEAVE_ENCASHMENT", "Leave encashment", "leave_encashment", "leave_encashment_basis"),
    ("GRATUITY", "Gratuity", "gratuity", "gratuity_basis"),
    ("REIMBURSEMENTS", "Pending reimbursements", "pending_reimbursements", "reimbursement_basis"),
    ("BONUS", "Statutory bonus (pro rata)", "pro_rata_bonus", "bonus_basis"),
    ("ARREARS", "Pending arrears", "pending_arrears", "arrears_basis"),
)

_RECOVERIES = (
    ("NOTICE_SHORTFALL", "Notice period shortfall", "notice_shortfall", "notice_basis"),
    ("LOAN", "Outstanding loan and advances", "loan_outstanding", "loan_basis"),
    ("ASSETS", "Unreturned assets", "asset_recovery", "asset_basis"),
    ("EXCESS_LEAVE", "Leave taken beyond accrual", "excess_leave_taken", "excess_leave_basis"),
    ("OTHER_RECOVERY", "Other recoveries", "other_recoveries", "other_recovery_basis"),
)

_SOURCE = {
    "LEAVE_ENCASHMENT": "exit_benefits",
    "GRATUITY": "exit_benefits",
    "BONUS": "statutory",
    "ARREARS": "arrears",
    "LOAN": "loans",
    "ASSETS": "clearance",
}


def compute_settlement(data: SettlementInput) -> SettlementResult:
    """The statement, its total, and which way it points."""
    lines: list[StatementLine] = []
    notes: list[str] = []
    order = 0

    for code, label, attribute, basis_attribute in _EARNINGS:
        amount = _check(label, getattr(data, attribute))
        if amount == ZERO:
            continue
        order += 1
        lines.append(
            StatementLine(
                line_type="earning",
                code=code,
                label=label,
                amount=_money(amount),
                basis=getattr(data, basis_attribute) or _missing_basis(label),
                source=_SOURCE.get(code),
                display_order=order,
            )
        )

    for code, label, attribute, basis_attribute in _RECOVERIES:
        amount = _check(label, getattr(data, attribute))
        if amount == ZERO:
            continue
        order += 1
        lines.append(
            StatementLine(
                line_type="recovery",
                code=code,
                label=label,
                amount=_money(amount),
                basis=getattr(data, basis_attribute) or _missing_basis(label),
                source=_SOURCE.get(code),
                display_order=order,
            )
        )

    total_earnings = sum((line.amount for line in lines if line.line_type == "earning"), ZERO)
    total_recoveries = sum((line.amount for line in lines if line.line_type == "recovery"), ZERO)

    # ── Tax, signed ─────────────────────────────────────────────────────────
    tax = _check("Final TDS", data.final_tds, allow_negative=True)
    if tax != ZERO:
        order += 1
        refund = tax < ZERO
        lines.append(
            StatementLine(
                line_type="tax",
                code="TDS_FINAL",
                label="Income tax refund on final computation"
                if refund
                else "Income tax deducted at source",
                amount=_money(abs(tax)),
                basis=data.tax_basis or _missing_basis("Final TDS"),
                source="tax",
                display_order=order,
            )
        )
        if refund:
            notes.append(
                "The full-year tax was recomputed at exit against actual figures rather "
                "than a projection, and it produced a refund. This is normal for someone "
                "leaving part-way through a year, when the projection assumed twelve "
                "months of income."
            )

    net = total_earnings - total_recoveries - tax

    if net < ZERO:
        notes.append(
            f"The recoveries exceed what is due, so this settlement is a demand for "
            f"{_money(-net)} rather than a payment. It is not floored at zero — the "
            "amount is genuinely owed to the company."
        )

    return SettlementResult(
        total_earnings=_money(total_earnings),
        total_recoveries=_money(total_recoveries),
        total_tax=_money(tax),
        net_amount=_money(net),
        lines=lines,
        notes=notes,
    )


def _missing_basis(label: str) -> str:
    """A line with no basis is worse than no line.

    Rather than rendering an empty cell on a statement the employee is about to
    scrutinise, this says plainly that the explanation is missing — which is a
    visible defect somebody will report, instead of an invisible one.
    """
    return (
        f"No computation basis was recorded for '{label}'. Ask HR for the working "
        "before accepting this line."
    )


def notice_shortfall_recovery(
    *,
    shortfall_days: int,
    monthly_wage: Decimal,
    days_in_month: int,
    wage_basis_label: str,
) -> tuple[Decimal, str]:
    """Recovery for notice not served, on the configured wage basis.

    Which wage — basic, basic plus DA, or gross — is a contractual term that
    differs per employer, so it is passed in as a figure with its label rather
    than derived here. Computing it on gross when the contract says basic
    overcharges the employee by a factor of two or more, and it is the single
    most disputed line on a settlement.
    """
    if not isinstance(monthly_wage, Decimal):
        raise SettlementError("The monthly wage must be a Decimal, not a float")
    if shortfall_days < 0:
        raise SettlementError("Notice shortfall days cannot be negative")
    if days_in_month <= 0:
        raise SettlementError("Days in month must be greater than zero")
    if shortfall_days == 0:
        return ZERO, "Notice was served in full."

    daily = monthly_wage / Decimal(days_in_month)
    amount = _money(daily * Decimal(shortfall_days))
    return amount, (
        f"{shortfall_days} day(s) of notice not served, at {_money(daily)} per day "
        f"({wage_basis_label} of {_money(monthly_wage)} over {days_in_month} days)."
    )
