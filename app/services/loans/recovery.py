"""EMI recovery through payroll, and the net-pay floor.

═══════════════════════════════════════════════════════════════════════════════
NEVER RECOVER AN EMI THAT TAKES SOMEONE'S NET PAY TO ZERO
═══════════════════════════════════════════════════════════════════════════════

It is the obvious implementation — the EMI is due, so deduct it — and it is how
an employee who took two weeks of unpaid leave ends up receiving nothing at all
in a month they needed to eat. The loan is not more urgent than the salary.

So recovery is checked against a configured floor, and where it would breach it
the instalment is **skipped and carried forward with a reason**, not silently
reduced to whatever fits. A partial recovery that nobody recorded as partial is
how an amortisation ledger stops reconciling.

Partial recovery *is* supported, but only where configuration says so and the
partial amount is recorded as such, so the schedule can be rebuilt around it.
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

__all__ = [
    "RecoveryDecision",
    "RecoveryError",
    "plan_recovery",
]

ZERO = Decimal(0)
RUPEE = Decimal("0.01")


class RecoveryError(ValueError):
    """A recovery cannot be planned from what was supplied."""


def _money(value: Decimal) -> Decimal:
    return value.quantize(RUPEE, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    """What to deduct this month, and why that figure."""

    amount: Decimal
    is_full_emi: bool
    is_skipped: bool
    carried_forward: Decimal
    reason: str
    resulting_net: Decimal
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "amount": str(self.amount),
            "is_full_emi": self.is_full_emi,
            "is_skipped": self.is_skipped,
            "carried_forward": str(self.carried_forward),
            "reason": self.reason,
            "resulting_net": str(self.resulting_net),
            "notes": self.notes,
        }


def plan_recovery(
    *,
    emi: Decimal,
    net_pay_before_recovery: Decimal,
    minimum_net_pay: Decimal,
    outstanding_principal: Decimal,
    already_carried_forward: Decimal = ZERO,
    allow_partial: bool = False,
) -> RecoveryDecision:
    """Decide this month's loan deduction.

    Args:
        minimum_net_pay: the floor the employee must be left with. A tenant
            setting, because what counts as a liveable minimum is a policy
            decision and not something to hardcode at zero.
        already_carried_forward: instalments skipped in earlier months. They
            are attempted alongside this month's, so a good month clears a
            backlog rather than the backlog growing forever.
        allow_partial: when set, a recovery that does not fit is reduced to
            what fits rather than skipped entirely — and the shortfall is
            still carried forward.
    """
    for name, value in (
        ("The EMI", emi),
        ("Net pay", net_pay_before_recovery),
        ("The minimum net pay", minimum_net_pay),
        ("The outstanding principal", outstanding_principal),
        ("The carried-forward amount", already_carried_forward),
    ):
        if not isinstance(value, Decimal):
            raise RecoveryError(f"{name} must be a Decimal, not a float")
        if value < ZERO:
            raise RecoveryError(f"{name} cannot be negative")

    if emi == ZERO:
        raise RecoveryError("There is no instalment to recover.")

    notes: list[str] = []
    due = emi + already_carried_forward
    if already_carried_forward > ZERO:
        notes.append(
            f"{already_carried_forward} carried forward from earlier month(s) is "
            "attempted alongside this month's instalment."
        )

    # Never recover more than is owed, even when a backlog has built up.
    if due > outstanding_principal:
        due = outstanding_principal
        notes.append(
            f"Capped at the outstanding principal of {outstanding_principal} — the "
            "final recovery is what is left, not a full instalment."
        )

    headroom = net_pay_before_recovery - minimum_net_pay

    # ── It fits ─────────────────────────────────────────────────────────────
    if headroom >= due:
        return RecoveryDecision(
            amount=_money(due),
            is_full_emi=due >= emi,
            is_skipped=False,
            carried_forward=ZERO,
            reason=(
                f"Recovered in full. Net pay of {net_pay_before_recovery} leaves "
                f"{_money(net_pay_before_recovery - due)} after the deduction, above "
                f"the floor of {minimum_net_pay}."
            ),
            resulting_net=_money(net_pay_before_recovery - due),
            notes=notes,
        )

    # ── It does not fit ─────────────────────────────────────────────────────
    if headroom <= ZERO:
        return RecoveryDecision(
            amount=ZERO,
            is_full_emi=False,
            is_skipped=True,
            carried_forward=_money(due),
            reason=(
                f"Skipped and carried forward. Net pay of {net_pay_before_recovery} is "
                f"already at or below the minimum of {minimum_net_pay}, so recovering "
                "anything would leave this employee with less than the floor. The loan "
                "is not more urgent than the salary."
            ),
            resulting_net=_money(net_pay_before_recovery),
            notes=notes,
        )

    if not allow_partial:
        return RecoveryDecision(
            amount=ZERO,
            is_full_emi=False,
            is_skipped=True,
            carried_forward=_money(due),
            reason=(
                f"Skipped and carried forward. Only {_money(headroom)} of the "
                f"{_money(due)} due would fit above the minimum net pay of "
                f"{minimum_net_pay}, and partial recovery is not enabled for this "
                "scheme — a part-payment nobody recorded as partial is how a ledger "
                "stops reconciling."
            ),
            resulting_net=_money(net_pay_before_recovery),
            notes=notes,
        )

    partial = _money(headroom)
    return RecoveryDecision(
        amount=partial,
        is_full_emi=False,
        is_skipped=False,
        carried_forward=_money(due - partial),
        reason=(
            f"Partially recovered. {partial} of the {_money(due)} due fits above the "
            f"minimum net pay of {minimum_net_pay}; the remaining "
            f"{_money(due - partial)} is carried forward."
        ),
        resulting_net=_money(net_pay_before_recovery - partial),
        notes=notes,
    )
