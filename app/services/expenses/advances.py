"""Travel advances: settlement against the trip, and the block finance cares about.

═══════════════════════════════════════════════════════════════════════════════
THE UNSETTLED ADVANCE IS THE WHOLE CONTROL
═══════════════════════════════════════════════════════════════════════════════

Everything else in an advance workflow is bookkeeping. The control finance
actually asks for is this: somebody took ₹40,000 for a trip in March, never
submitted the claims, and in June asks for another advance. If the system issues
it, the company is now ₹80,000 out to one person with no expenses against either
amount — and that is not a hypothetical, it is the single most common way money
leaks out of an expense process.

So `advance_block` refuses a new advance while an old one is unsettled past its
age, names the trip, and says what would clear it. Refusing without saying which
advance is blocking sends somebody to finance to ask, and finance says yes
because the person in front of them has a trip on Monday.

═══════════════════════════════════════════════════════════════════════════════
SETTLEMENT IS AGAINST THE TRIP, NOT THE PERSON
═══════════════════════════════════════════════════════════════════════════════

Claims offset the advance that funded *their* trip. Netting across trips would
let an under-spent trip in March quietly settle an over-spent one in June, and
the balance nobody would ever look at again is the one that mattered.

Pure. `today` is always supplied.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from app.services.expenses.policy import money

__all__ = [
    "AdvanceError",
    "AdvanceFacts",
    "BlockDecision",
    "SettlementOutcome",
    "SettlementResult",
    "advance_block",
    "settle",
]


class AdvanceError(ValueError):
    """An advance operation this module refuses."""


class SettlementOutcome(str, enum.Enum):
    """Where the money ends up after the trip."""

    #: Claims exactly matched the advance.
    SQUARE = "square"
    #: Claims exceeded the advance. The company owes the employee.
    REFUND_DUE_TO_EMPLOYEE = "refund_due_to_employee"
    #: Claims fell short. The employee owes the balance back, recovered
    #: through payroll unless they repay it directly.
    RECOVERY_DUE_FROM_EMPLOYEE = "recovery_due_from_employee"

    @property
    def needs_payroll_recovery(self) -> bool:
        return self is SettlementOutcome.RECOVERY_DUE_FROM_EMPLOYEE


@dataclass(frozen=True, slots=True)
class AdvanceFacts:
    """One issued advance and what has happened to it."""

    advance_id: str
    trip_id: str
    employee_id: str
    amount: Decimal
    issued_on: date
    #: Claims already settled against this advance.
    claimed_against: Decimal = Decimal("0")
    settled_on: date | None = None
    recovered: Decimal = Decimal("0")
    refunded: Decimal = Decimal("0")

    @property
    def is_settled(self) -> bool:
        return self.settled_on is not None

    @property
    def outstanding(self) -> Decimal:
        """What is still unaccounted for.

        Zero for a settled advance whatever the arithmetic says: once it is
        settled, the balance moved to a payroll recovery or a refund, and
        continuing to report it as outstanding would double-count it.
        """
        if self.is_settled:
            return Decimal("0")
        return money(max(Decimal("0"), self.amount - self.claimed_against))

    def age_days(self, today: date) -> int:
        return (today - self.issued_on).days


@dataclass(frozen=True, slots=True)
class SettlementResult:
    """What settling a trip's advance produces."""

    advance_id: str
    trip_id: str
    advance_amount: Decimal
    claims_total: Decimal
    outcome: SettlementOutcome
    #: Positive where the employee owes it back.
    recovery_amount: Decimal
    #: Positive where the company owes the employee.
    refund_amount: Decimal
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "advance_id": self.advance_id,
            "trip_id": self.trip_id,
            "advance_amount": str(self.advance_amount),
            "claims_total": str(self.claims_total),
            "outcome": self.outcome.value,
            "recovery_amount": str(self.recovery_amount),
            "refund_amount": str(self.refund_amount),
            "needs_payroll_recovery": self.outcome.needs_payroll_recovery,
            "message": self.message,
        }


def settle(
    advance: AdvanceFacts,
    *,
    claims_total: Decimal,
    trip_ids_of_claims: tuple[str, ...] = (),
) -> SettlementResult:
    """Offset a trip's claims against its advance.

    Refuses claims from a different trip. Netting across trips would let an
    under-spent March trip quietly settle an over-spent June one, and the
    balance nobody would look at again is the one that mattered.
    """
    if advance.is_settled:
        raise AdvanceError(
            f"Advance {advance.advance_id} was settled on "
            f"{advance.settled_on}. Settling it again would recover or refund "
            "the balance twice."
        )

    foreign = [t for t in trip_ids_of_claims if t != advance.trip_id]
    if foreign:
        raise AdvanceError(
            f"Claims from trip(s) {sorted(set(foreign))} cannot settle the "
            f"advance for trip {advance.trip_id}. An advance settles against "
            "the trip it funded — netting across trips hides the balance that "
            "matters."
        )

    total = money(claims_total)
    amount = money(advance.amount)

    if total == amount:
        return SettlementResult(
            advance_id=advance.advance_id,
            trip_id=advance.trip_id,
            advance_amount=amount,
            claims_total=total,
            outcome=SettlementOutcome.SQUARE,
            recovery_amount=Decimal("0.00"),
            refund_amount=Decimal("0.00"),
            message=f"Claims of ₹{total} exactly match the advance. Nothing to do.",
        )

    if total > amount:
        refund = money(total - amount)
        return SettlementResult(
            advance_id=advance.advance_id,
            trip_id=advance.trip_id,
            advance_amount=amount,
            claims_total=total,
            outcome=SettlementOutcome.REFUND_DUE_TO_EMPLOYEE,
            recovery_amount=Decimal("0.00"),
            refund_amount=refund,
            message=(
                f"Claims of ₹{total} exceed the ₹{amount} advance. ₹{refund} is "
                "due to the employee — they spent their own money on the "
                "company's trip and should not have to ask twice."
            ),
        )

    recovery = money(amount - total)
    return SettlementResult(
        advance_id=advance.advance_id,
        trip_id=advance.trip_id,
        advance_amount=amount,
        claims_total=total,
        outcome=SettlementOutcome.RECOVERY_DUE_FROM_EMPLOYEE,
        recovery_amount=recovery,
        refund_amount=Decimal("0.00"),
        message=(
            f"Claims of ₹{total} fall ₹{recovery} short of the ₹{amount} "
            "advance. The balance is recovered through payroll unless the "
            "employee repays it directly first."
        ),
    )


# ══════════════════════════════════════════════════════════════════════════════
# The block
# ══════════════════════════════════════════════════════════════════════════════

#: How long an advance may sit unsettled before it blocks a new one. Forty-five
#: days is roughly a trip plus a claim cycle plus a fortnight of forgetting;
#: shorter turns a normal delay into an obstruction, and longer means the
#: control fires after the second advance has already gone out.
DEFAULT_BLOCK_AFTER_DAYS = 45


@dataclass(frozen=True, slots=True)
class BlockDecision:
    """Whether this employee may take another advance, and why not."""

    permitted: bool
    blocking: tuple[AdvanceFacts, ...]
    total_outstanding: Decimal
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "permitted": self.permitted,
            "blocking_advances": [
                {
                    "advance_id": a.advance_id,
                    "trip_id": a.trip_id,
                    "amount": str(money(a.amount)),
                    "outstanding": str(a.outstanding),
                    "issued_on": a.issued_on.isoformat(),
                }
                for a in self.blocking
            ],
            "total_outstanding": str(self.total_outstanding),
            "message": self.message,
        }


def advance_block(
    advances: list[AdvanceFacts],
    *,
    today: date,
    block_after_days: int = DEFAULT_BLOCK_AFTER_DAYS,
) -> BlockDecision:
    """Whether a new advance may be issued to this employee.

    The message names the blocking trip and says what clears it. A refusal with
    no detail sends the employee to finance, and finance says yes — because the
    person in front of them has a trip on Monday and nobody can see which old
    advance is the problem.
    """
    if block_after_days < 1:
        raise AdvanceError(
            "A block threshold of zero days would block every employee who has "
            "ever taken an advance, including one issued this morning."
        )

    blocking = tuple(
        a
        for a in advances
        if not a.is_settled
        and a.outstanding > 0
        and a.age_days(today) > block_after_days
    )

    if not blocking:
        outstanding_now = money(
            sum((a.outstanding for a in advances if not a.is_settled), Decimal("0"))
        )
        return BlockDecision(
            permitted=True,
            blocking=(),
            total_outstanding=outstanding_now,
            message=(
                "No advance is unsettled past the threshold."
                + (
                    f" ₹{outstanding_now} is outstanding but still within "
                    f"{block_after_days} days."
                    if outstanding_now
                    else ""
                )
            ),
        )

    total = money(sum((a.outstanding for a in blocking), Decimal("0")))
    oldest = min(blocking, key=lambda a: a.issued_on)

    return BlockDecision(
        permitted=False,
        blocking=blocking,
        total_outstanding=total,
        message=(
            f"₹{total} is unsettled across {len(blocking)} advance(s), the "
            f"oldest issued on {oldest.issued_on.isoformat()} for trip "
            f"{oldest.trip_id} and now {oldest.age_days(today)} days old. "
            "Submit the claims for that trip, or repay the balance, and a new "
            "advance can be issued. This is not a judgement about the trip — it "
            "is the control that stops two advances being outstanding at once."
        ),
    )


def ageing_report(
    advances: list[AdvanceFacts],
    *,
    today: date,
    block_after_days: int = DEFAULT_BLOCK_AFTER_DAYS,
) -> dict[str, Any]:
    """Outstanding advances by age, for the person who chases them.

    Bucketed rather than listed by date, because the question is "how bad is
    this" and a list of forty rows sorted by date does not answer it.
    """
    live = [a for a in advances if not a.is_settled and a.outstanding > 0]
    buckets: dict[str, list[AdvanceFacts]] = {
        "0-30": [],
        "31-45": [],
        "46-90": [],
        "over_90": [],
    }
    for advance in live:
        age = advance.age_days(today)
        if age <= 30:
            buckets["0-30"].append(advance)
        elif age <= block_after_days:
            buckets["31-45"].append(advance)
        elif age <= 90:
            buckets["46-90"].append(advance)
        else:
            buckets["over_90"].append(advance)

    return {
        "as_of": today.isoformat(),
        "block_after_days": block_after_days,
        "total_outstanding": str(
            money(sum((a.outstanding for a in live), Decimal("0")))
        ),
        "buckets": {
            name: {
                "count": len(rows),
                "amount": str(
                    money(sum((a.outstanding for a in rows), Decimal("0")))
                ),
            }
            for name, rows in buckets.items()
        },
        "blocking_count": sum(
            1 for a in live if a.age_days(today) > block_after_days
        ),
    }
