"""GSTR-2B reconciliation, duplicate detection, and the number that sells this.

═══════════════════════════════════════════════════════════════════════════════
THE CREDIT IS ONLY CLAIMABLE IF THE VENDOR ACTUALLY FILED
═══════════════════════════════════════════════════════════════════════════════

A perfect invoice with a valid GSTIN and an eligible category is still worth
nothing if the vendor did not report it. GSTR-2B is what the portal says the
company is entitled to, and the difference between "what we captured" and "what
the portal shows" is where the money is:

- **Matched** — captured and in 2B. Claimable.
- **In our register, not in 2B** — the vendor has not filed. This is the number.
  It is recoverable by chasing the vendor and it is invisible without this
  comparison.
- **In 2B, not in our register** — an invoice the company was billed for that
  never went through expenses. Either somebody paid outside the system or the
  credit is being left on the table.

All three are reported. A single net figure would let the second and third
cancel, which is the worst outcome: both are real and neither is visible.

═══════════════════════════════════════════════════════════════════════════════
MATCHING IS ON NORMALISED KEYS, AND NEAR-MISSES ARE REPORTED
═══════════════════════════════════════════════════════════════════════════════

Invoice numbers arrive as `INV/2026/0042`, `inv-2026-0042` and `INV 2026 0042`
for the same document, because one came from OCR and one from a portal download.
Matching on the raw string reports a vendor as unfiled when they filed. So the
key is normalised — case folded, separators stripped — and a match on the
normalised key with a different amount is reported as a **value mismatch**
rather than as two separate gaps.

Pure.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

__all__ = [
    "CapturedInvoice",
    "Gstr2bLine",
    "MatchState",
    "RegisterError",
    "duplicates",
    "normalise_invoice_number",
    "reconcile_2b",
    "value_dashboard",
]


class RegisterError(ValueError):
    """A register operation this module refuses."""


def money(value: Decimal | int | str) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def normalise_invoice_number(raw: str) -> str:
    """Fold an invoice number to a comparable key.

    `INV/2026/0042`, `inv-2026-0042` and `INV 2026 0042` are the same document
    arriving from OCR and from a portal download. Matching the raw strings
    reports a vendor as unfiled when they filed, and somebody then chases a
    vendor who did nothing wrong.

    Leading zeros inside a segment are **kept**: `INV-042` and `INV-42` are
    different documents often enough that collapsing them would create false
    matches, which is the more expensive error.
    """
    return re.sub(r"[^A-Z0-9]", "", (raw or "").upper())


class MatchState(str, enum.Enum):
    MATCHED = "matched"
    #: Captured by us, absent from 2B. The vendor has not filed. This is the
    #: recoverable money.
    MISSING_IN_2B = "missing_in_2b"
    #: In 2B, never captured. Paid outside the expense system, or credit left
    #: on the table.
    MISSING_IN_REGISTER = "missing_in_register"
    #: Same invoice, different tax. Worth resolving before either figure is
    #: used.
    VALUE_MISMATCH = "value_mismatch"


@dataclass(frozen=True, slots=True)
class CapturedInvoice:
    """One line from our own register."""

    credit_id: str
    vendor_gstin: str
    invoice_number: str
    invoice_date: date
    taxable_value: Decimal
    total_tax: Decimal
    eligibility: str = "eligible"
    employee_id: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (
            self.vendor_gstin.upper(),
            normalise_invoice_number(self.invoice_number),
        )


@dataclass(frozen=True, slots=True)
class Gstr2bLine:
    """One line as the portal reports it."""

    vendor_gstin: str
    invoice_number: str
    invoice_date: date
    taxable_value: Decimal
    total_tax: Decimal

    @property
    def key(self) -> tuple[str, str]:
        return (
            self.vendor_gstin.upper(),
            normalise_invoice_number(self.invoice_number),
        )


@dataclass(frozen=True, slots=True)
class MatchResult:
    """One line's standing against 2B."""

    state: MatchState
    vendor_gstin: str
    invoice_number: str
    credit_id: str
    our_tax: Decimal
    portal_tax: Decimal
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "vendor_gstin": self.vendor_gstin,
            "invoice_number": self.invoice_number,
            "credit_id": self.credit_id,
            "our_tax": str(self.our_tax),
            "portal_tax": str(self.portal_tax),
            "detail": self.detail,
        }


def reconcile_2b(
    *,
    captured: list[CapturedInvoice],
    portal: list[Gstr2bLine],
    period_label: str,
    tolerance: Decimal = Decimal("1.00"),
) -> dict[str, Any]:
    """Match our register against GSTR-2B and report the rupee gap.

    Only lines we consider eligible are counted toward the at-risk figure: a
    blocked category missing from 2B is not money we were going to claim, and
    including it would inflate the number that sells this feature — which is
    exactly the way to make a finance team stop believing it.
    """
    if tolerance < 0:
        raise RegisterError("A matching tolerance cannot be negative.")

    ours = {inv.key: inv for inv in captured}
    theirs: dict[tuple[str, str], Gstr2bLine] = {}
    for line in portal:
        if line.key in theirs:
            existing = theirs[line.key]
            theirs[line.key] = Gstr2bLine(
                vendor_gstin=line.vendor_gstin,
                invoice_number=line.invoice_number,
                invoice_date=line.invoice_date,
                taxable_value=existing.taxable_value + line.taxable_value,
                total_tax=existing.total_tax + line.total_tax,
            )
        else:
            theirs[line.key] = line

    results: list[MatchResult] = []

    for key, invoice in sorted(ours.items()):
        portal_line = theirs.get(key)
        if portal_line is None:
            results.append(
                MatchResult(
                    state=MatchState.MISSING_IN_2B,
                    vendor_gstin=invoice.vendor_gstin,
                    invoice_number=invoice.invoice_number,
                    credit_id=invoice.credit_id,
                    our_tax=money(invoice.total_tax),
                    portal_tax=Decimal("0.00"),
                    detail=(
                        "Captured here and absent from GSTR-2B: the vendor has "
                        "not filed. The credit is not claimable until they do, "
                        "and chasing them is how it is recovered."
                    ),
                )
            )
            continue

        if abs(money(invoice.total_tax) - money(portal_line.total_tax)) > tolerance:
            results.append(
                MatchResult(
                    state=MatchState.VALUE_MISMATCH,
                    vendor_gstin=invoice.vendor_gstin,
                    invoice_number=invoice.invoice_number,
                    credit_id=invoice.credit_id,
                    our_tax=money(invoice.total_tax),
                    portal_tax=money(portal_line.total_tax),
                    detail=(
                        "The invoice matched but the tax does not. Claiming "
                        "either figure without resolving it invites a notice - "
                        "the portal's number is what the department will "
                        "compare against."
                    ),
                )
            )
            continue

        results.append(
            MatchResult(
                state=MatchState.MATCHED,
                vendor_gstin=invoice.vendor_gstin,
                invoice_number=invoice.invoice_number,
                credit_id=invoice.credit_id,
                our_tax=money(invoice.total_tax),
                portal_tax=money(portal_line.total_tax),
                detail="Matched to GSTR-2B and claimable.",
            )
        )

    for key, line in sorted(theirs.items()):
        if key in ours:
            continue
        results.append(
            MatchResult(
                state=MatchState.MISSING_IN_REGISTER,
                vendor_gstin=line.vendor_gstin,
                invoice_number=line.invoice_number,
                credit_id="",
                our_tax=Decimal("0.00"),
                portal_tax=money(line.total_tax),
                detail=(
                    "In GSTR-2B and never captured as an expense. Either it was "
                    "paid outside the system or the credit is being left on the "
                    "table."
                ),
            )
        )

    def total(state: MatchState, use_portal: bool = False) -> Decimal:
        return money(
            sum(
                (
                    (r.portal_tax if use_portal else r.our_tax)
                    for r in results
                    if r.state is state
                ),
                Decimal("0"),
            )
        )

    eligible_keys = {inv.key for inv in captured if inv.eligibility == "eligible"}
    at_risk = money(
        sum(
            (
                r.our_tax
                for r in results
                if r.state is MatchState.MISSING_IN_2B
                and (r.vendor_gstin.upper(), normalise_invoice_number(r.invoice_number))
                in eligible_keys
            ),
            Decimal("0"),
        )
    )

    counts = {state.value: 0 for state in MatchState}
    for result in results:
        counts[result.state.value] += 1

    return {
        "period": period_label,
        "captured_lines": len(captured),
        "portal_lines": len(portal),
        "counts": counts,
        "matched_tax": str(total(MatchState.MATCHED)),
        "missing_in_2b_tax": str(total(MatchState.MISSING_IN_2B)),
        "missing_in_register_tax": str(
            total(MatchState.MISSING_IN_REGISTER, use_portal=True)
        ),
        "mismatched_our_tax": str(total(MatchState.VALUE_MISMATCH)),
        "mismatched_portal_tax": str(
            total(MatchState.VALUE_MISMATCH, use_portal=True)
        ),
        "at_risk_from_unfiled_vendors": str(at_risk),
        "lines": [r.as_dict() for r in results],
        "note": (
            f"Rs {at_risk} of eligible credit is captured here and not in "
            "GSTR-2B, which means the vendor has not filed. That is the "
            "recoverable number, and it counts only lines we consider eligible "
            "- inflating it with blocked categories is how a finance team stops "
            "believing the figure."
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Duplicates
# ══════════════════════════════════════════════════════════════════════════════


def duplicates(captured: list[CapturedInvoice]) -> list[dict[str, Any]]:
    """The same vendor invoice claimed more than once, across the whole tenant.

    Across the tenant, not per employee, which is the point: two people
    submitting the same restaurant bill is the case that matters and a
    per-employee check cannot see it. Whether it is an error or fraud is not
    something this decides — it reports the fact and names both claims.
    """
    seen: dict[tuple[str, str], list[CapturedInvoice]] = {}
    for invoice in captured:
        seen.setdefault(invoice.key, []).append(invoice)

    out = []
    for (gstin, number), group in sorted(seen.items()):
        if len(group) < 2:
            continue
        employees = sorted({inv.employee_id for inv in group if inv.employee_id})
        out.append(
            {
                "vendor_gstin": gstin,
                "normalised_invoice_number": number,
                "claim_count": len(group),
                "credit_ids": sorted(inv.credit_id for inv in group),
                "employee_ids": employees,
                "total_tax": str(
                    money(sum((inv.total_tax for inv in group), Decimal("0")))
                ),
                "across_employees": len(employees) > 1,
                "detail": (
                    f"The same invoice was claimed by {len(employees)} different "
                    "people. That is the case a per-employee check cannot see."
                    if len(employees) > 1
                    else "The same invoice was claimed more than once by one "
                    "person, most often a resubmission after a rejection."
                ),
            }
        )
    return out


# ══════════════════════════════════════════════════════════════════════════════
# The dashboard that sells this
# ══════════════════════════════════════════════════════════════════════════════


def value_dashboard(
    *,
    captured: list[CapturedInvoice],
    reconciliation: dict[str, Any] | None = None,
    period_label: str = "",
) -> dict[str, Any]:
    """Captured, claimable and at-risk credit in rupees.

    Stated plainly and without rounding up. In a client conversation this is
    what converts the expense module from a convenience into a line item with a
    return — and a number that turns out to be optimistic destroys that
    conversation permanently, so `at_risk` counts only eligible lines and
    `uncertain` is reported separately rather than being counted as either.
    """
    eligible = [c for c in captured if c.eligibility == "eligible"]
    ineligible = [c for c in captured if c.eligibility == "ineligible"]
    uncertain = [c for c in captured if c.eligibility == "uncertain"]

    def total(rows: list[CapturedInvoice]) -> Decimal:
        return money(sum((r.total_tax for r in rows), Decimal("0")))

    at_risk = (
        Decimal(reconciliation["at_risk_from_unfiled_vendors"])
        if reconciliation
        else Decimal("0.00")
    )
    matched = (
        Decimal(reconciliation["matched_tax"]) if reconciliation else Decimal("0.00")
    )

    return {
        "period": period_label,
        "captured_tax": str(total(captured)),
        "eligible_tax": str(total(eligible)),
        "ineligible_tax": str(total(ineligible)),
        "uncertain_tax": str(total(uncertain)),
        "uncertain_lines": len(uncertain),
        "confirmed_claimable_tax": str(money(matched)),
        "at_risk_from_unfiled_vendors": str(money(at_risk)),
        "reconciled": reconciliation is not None,
        "note": (
            f"Rs {total(eligible)} of input credit was captured and considered "
            f"eligible. Rs {money(matched)} is confirmed against GSTR-2B and "
            f"Rs {money(at_risk)} is at risk because the vendor has not filed. "
            f"Rs {total(uncertain)} across {len(uncertain)} line(s) is awaiting "
            "a finance decision and is counted as neither."
            if reconciliation
            else f"Rs {total(eligible)} of input credit was captured and "
            "considered eligible. **Nothing has been reconciled against "
            "GSTR-2B**, so none of it is confirmed claimable - import the 2B "
            "before quoting this figure to anybody."
        ),
    }
