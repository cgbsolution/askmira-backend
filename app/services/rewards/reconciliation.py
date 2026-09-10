"""Monthly reconciliation: points redeemed, orders fulfilled, cost invoiced.

═══════════════════════════════════════════════════════════════════════════════
A GAP IN EITHER DIRECTION IS A DIFFERENT PROBLEM, AND BOTH MATTER
═══════════════════════════════════════════════════════════════════════════════

Three numbers should agree every month:

- points redeemed (ours),
- orders fulfilled by the merchandise partner (theirs),
- cost invoiced (theirs, in rupees).

Redeemed without fulfilled is an **employee** problem: somebody spent their
points and never received the thing. They will not chase it twice, and the
programme loses them.

Fulfilled without redeemed is a **billing** problem: we are being invoiced for
something no employee asked for, or a redemption was lost from our side.

Invoiced without agreeing on value is a **pricing** problem, and it compounds
silently across months.

So this reports all three separately rather than a net figure. A single
"variance" number lets a fulfilment failure and a billing error cancel each
other out and show as zero, which is the worst possible outcome: both are real
and neither is visible.

Pure. All periods and amounts are supplied.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

__all__ = [
    "FulfilmentRecord",
    "GapKind",
    "InvoiceLine",
    "ReconciliationError",
    "RedemptionRecord",
    "reconcile",
]


class ReconciliationError(ValueError):
    """A reconciliation this module refuses to produce."""


def money(value: Decimal | int | str) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class GapKind(str, enum.Enum):
    """What kind of problem a mismatch is."""

    #: Points spent, nothing shipped. The employee is out of pocket in points
    #: and has nothing to show for it.
    UNFULFILLED_REDEMPTION = "unfulfilled_redemption"
    #: Something shipped with no redemption behind it. Either we lost the
    #: redemption or we are being invoiced for somebody else's order.
    ORPHAN_FULFILMENT = "orphan_fulfilment"
    #: Fulfilled but not invoiced. A cost that will arrive later and land in
    #: the wrong month.
    UNINVOICED_FULFILMENT = "uninvoiced_fulfilment"
    #: Invoiced with nothing fulfilled against it.
    ORPHAN_INVOICE = "orphan_invoice"
    #: All three present, values disagree.
    VALUE_MISMATCH = "value_mismatch"


@dataclass(frozen=True, slots=True)
class RedemptionRecord:
    """One redemption on our side."""

    redemption_id: str
    employee_id: str
    points: int
    rupee_value: Decimal
    item_sku: str = ""


@dataclass(frozen=True, slots=True)
class FulfilmentRecord:
    """One order the merchandise partner says it shipped."""

    redemption_id: str
    order_id: str
    item_sku: str = ""
    status: str = "fulfilled"

    @property
    def is_fulfilled(self) -> bool:
        return self.status.lower() in {"fulfilled", "delivered", "shipped"}


@dataclass(frozen=True, slots=True)
class InvoiceLine:
    """One line the partner billed us for."""

    order_id: str
    amount: Decimal
    item_sku: str = ""


@dataclass(frozen=True, slots=True)
class Gap:
    """One discrepancy, with enough detail to act on it."""

    kind: GapKind
    reference: str
    detail: str
    employee_id: str = ""
    rupee_value: Decimal = Decimal("0")

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "reference": self.reference,
            "detail": self.detail,
            "employee_id": self.employee_id,
            "rupee_value": str(money(self.rupee_value)),
        }


def reconcile(
    *,
    redemptions: list[RedemptionRecord],
    fulfilments: list[FulfilmentRecord],
    invoices: list[InvoiceLine],
    period_label: str,
    tolerance: Decimal = Decimal("1.00"),
) -> dict[str, Any]:
    """Match the three sets and report each kind of gap separately.

    Deliberately not a net variance. A fulfilment failure and a billing error
    that happen to be the same size would cancel to zero, and both are real.
    """
    if tolerance < 0:
        raise ReconciliationError("A tolerance cannot be negative.")

    by_redemption = {r.redemption_id: r for r in redemptions}
    fulfilled = {f.redemption_id: f for f in fulfilments if f.is_fulfilled}
    unfulfilled_status = [f for f in fulfilments if not f.is_fulfilled]
    by_order = {f.order_id: f for f in fulfilled.values()}
    invoice_by_order: dict[str, Decimal] = {}
    for line in invoices:
        invoice_by_order[line.order_id] = invoice_by_order.get(
            line.order_id, Decimal("0")
        ) + line.amount

    gaps: list[Gap] = []

    for redemption_id, record in sorted(by_redemption.items()):
        if redemption_id not in fulfilled:
            pending = next(
                (f for f in unfulfilled_status if f.redemption_id == redemption_id),
                None,
            )
            gaps.append(
                Gap(
                    kind=GapKind.UNFULFILLED_REDEMPTION,
                    reference=redemption_id,
                    employee_id=record.employee_id,
                    rupee_value=record.rupee_value,
                    detail=(
                        f"{record.points} points were spent and the order is "
                        f"'{pending.status}'. The employee has paid and has "
                        "nothing; they will not chase it twice."
                        if pending
                        else f"{record.points} points were spent and there is no "
                        "order at all. The employee has paid and has nothing."
                    ),
                )
            )

    for redemption_id, fulfilment in sorted(fulfilled.items()):
        if redemption_id not in by_redemption:
            gaps.append(
                Gap(
                    kind=GapKind.ORPHAN_FULFILMENT,
                    reference=fulfilment.order_id,
                    detail=(
                        f"Order {fulfilment.order_id} was fulfilled against "
                        f"redemption {redemption_id}, which is not in our "
                        "ledger. Either we lost the redemption or we are being "
                        "billed for somebody else's order."
                    ),
                )
            )
            continue

        record = by_redemption[redemption_id]
        invoiced = invoice_by_order.get(fulfilment.order_id)
        if invoiced is None:
            gaps.append(
                Gap(
                    kind=GapKind.UNINVOICED_FULFILMENT,
                    reference=fulfilment.order_id,
                    employee_id=record.employee_id,
                    rupee_value=record.rupee_value,
                    detail=(
                        "Fulfilled and not invoiced. The cost arrives in a "
                        "later month and lands in the wrong period."
                    ),
                )
            )
        elif abs(money(invoiced) - money(record.rupee_value)) > tolerance:
            gaps.append(
                Gap(
                    kind=GapKind.VALUE_MISMATCH,
                    reference=fulfilment.order_id,
                    employee_id=record.employee_id,
                    rupee_value=money(invoiced) - money(record.rupee_value),
                    detail=(
                        f"Redeemed at Rs {money(record.rupee_value)}, invoiced "
                        f"at Rs {money(invoiced)}. A pricing difference "
                        "compounds silently across months."
                    ),
                )
            )

    for order_id, amount in sorted(invoice_by_order.items()):
        if order_id not in by_order:
            gaps.append(
                Gap(
                    kind=GapKind.ORPHAN_INVOICE,
                    reference=order_id,
                    rupee_value=amount,
                    detail=(
                        f"Invoiced Rs {money(amount)} for order {order_id} with "
                        "no fulfilment on record."
                    ),
                )
            )

    by_kind: dict[str, int] = {}
    for gap in gaps:
        by_kind[gap.kind.value] = by_kind.get(gap.kind.value, 0) + 1

    redeemed_value = money(sum((r.rupee_value for r in redemptions), Decimal("0")))
    invoiced_value = money(sum(invoice_by_order.values(), Decimal("0")))

    return {
        "period": period_label,
        "redemptions": len(redemptions),
        "fulfilments": len(fulfilled),
        "invoice_lines": len(invoices),
        "redeemed_value": str(redeemed_value),
        "invoiced_value": str(invoiced_value),
        "clean": not gaps,
        "gap_count": len(gaps),
        "gaps_by_kind": by_kind,
        "gaps": [g.as_dict() for g in gaps],
        "note": (
            "Every kind of gap is reported separately rather than netted. A "
            "fulfilment failure and a billing error of the same size would "
            "cancel to zero in a variance figure, and both are real."
        ),
    }
