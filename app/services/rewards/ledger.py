"""The points ledger: insert-only, balance derived, liability at the issue rate.

═══════════════════════════════════════════════════════════════════════════════
THE BALANCE IS DERIVED. THE STORED ONE IS A TAMPER CHECK.
═══════════════════════════════════════════════════════════════════════════════

Every row carries `balance_after` as written at insert time, and nothing ever
updates it. That is not the balance the system reports — `derive_balance` sums
the transactions — it is a checkpoint, and `verify_chain` compares the two.

The distinction matters because the two failure modes are different. A mutable
balance column drifts silently and nobody finds out until finance reconciles. A
derived balance beside an immutable checkpoint means a divergence is *visible*:
either a row was inserted out of order or somebody wrote to the table directly,
and both are things you want to be told about rather than to average away.

═══════════════════════════════════════════════════════════════════════════════
LIABILITY IS VALUED AT THE RATE IN FORCE WHEN THE POINTS WERE AWARDED
═══════════════════════════════════════════════════════════════════════════════

Same rule as `PayrollRuleSet`, and here it is a balance-sheet number. A tenant
that awards 10,000 points at ₹0.50 and later revalues to ₹1.00 has not doubled
its liability for points already issued — the employee was awarded ₹5,000 of
value and that is what is owed. Revaluing retrospectively would restate a prior
period's accounts because somebody changed a setting.

Which means redemptions have to consume *lots*, oldest first. Without FIFO there
is no answer to "what is the remaining 4,000 points worth" once two rates exist,
and any answer picked at random is a number finance cannot reconcile.

Pure. `as_of` is always supplied.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

__all__ = [
    "AGEING_BUCKETS",
    "LedgerEntry",
    "LedgerError",
    "TransactionType",
    "ageing",
    "derive_balance",
    "expected_redemption_rate",
    "liability",
    "movement_report",
    "verify_chain",
]


class LedgerError(ValueError):
    """A ledger operation this module refuses."""


def money(value: Decimal | int | str) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class TransactionType(str, enum.Enum):
    """What one ledger row records.

    `REVERSED` rather than deleting the original: a mistaken award that
    disappears leaves an employee who saw 500 points and now sees none, with
    nothing on the statement to explain it. A reversal is visible and can be
    asked about.
    """

    AWARDED = "awarded"
    REDEEMED = "redeemed"
    EXPIRED = "expired"
    ADJUSTED = "adjusted"
    REVERSED = "reversed"

    @property
    def sign(self) -> int:
        """Whether this type adds to or removes from the balance.

        `ADJUSTED` is signed by its own points value — an adjustment can go
        either way and forcing it positive would make every correction an award.
        """
        return {
            TransactionType.AWARDED: 1,
            TransactionType.REDEEMED: -1,
            TransactionType.EXPIRED: -1,
            TransactionType.REVERSED: -1,
            TransactionType.ADJUSTED: 1,
        }[self]

    @property
    def increases_liability(self) -> bool:
        return self is TransactionType.AWARDED


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One immutable row.

    `points` is always positive except on an `ADJUSTED` row, which carries its
    own sign. `monetary_value` is the rupee value **at the rate in force when
    the row was written**, and it is never recomputed.
    """

    entry_id: str
    employee_id: str
    transaction_type: TransactionType
    points: int
    monetary_value: Decimal
    occurred_at: datetime
    source_type: str = ""
    source_id: str = ""
    balance_after: int = 0
    department_key: str = ""
    cost_centre: str = ""
    #: The valuation rate this row was written at, kept so a statement can show
    #: why two awards of the same size are worth different amounts.
    rate_per_point: Decimal = Decimal("0")
    #: When the row was written. Rows of one day share an `occurred_at`, so this
    #: is what keeps an award ahead of the redemption that spent it.
    recorded_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.transaction_type is not TransactionType.ADJUSTED and self.points < 0:
            raise LedgerError(
                f"A {self.transaction_type.value} row carries {self.points} "
                "points. Only an adjustment may be negative; a negative award "
                "is a reversal and should say so."
            )
        if self.points == 0:
            raise LedgerError("A ledger row of zero points records nothing.")

    @property
    def signed_points(self) -> int:
        if self.transaction_type is TransactionType.ADJUSTED:
            return self.points
        return self.points * self.transaction_type.sign


def chronological(entries: list[LedgerEntry]) -> list[LedgerEntry]:
    """Rows in the order they happened, then the order they were written."""
    return sorted(
        entries, key=lambda e: (e.occurred_at, e.recorded_at or e.occurred_at, e.entry_id)
    )


def derive_balance(entries: list[LedgerEntry]) -> int:
    """The balance, summed from the transactions.

    This is what the product reports. `balance_after` on a row is a checkpoint
    written at insert time, not the source of truth — see `verify_chain`.
    """
    return sum(e.signed_points for e in entries)


def verify_chain(entries: list[LedgerEntry]) -> dict[str, Any]:
    """Compare the derived running balance against the stored checkpoints.

    Each row's checkpoint is its employee's balance after it, so the running
    balance is kept per employee: a tenant-wide report mixes everyone's rows,
    and one shared total would call every second person's first award a break.

    A divergence means a row was inserted out of order or the table was written
    to directly. Both are worth being told about; neither should be silently
    reconciled by preferring one number.
    """
    ordered = chronological(entries)
    running: dict[str, int] = {}
    breaks: list[dict[str, Any]] = []

    for entry in ordered:
        balance = running.get(entry.employee_id, 0) + entry.signed_points
        running[entry.employee_id] = balance
        if entry.balance_after != balance:
            breaks.append(
                {
                    "entry_id": entry.entry_id,
                    "employee_id": entry.employee_id,
                    "occurred_at": entry.occurred_at.isoformat(),
                    "stored_balance_after": entry.balance_after,
                    "derived_balance_after": balance,
                    "difference": entry.balance_after - balance,
                }
            )

    return {
        "entries": len(ordered),
        "derived_balance": sum(running.values()),
        "intact": not breaks,
        "breaks": breaks,
        "note": (
            "The derived balance and the stored checkpoints agree."
            if not breaks
            else f"{len(breaks)} row(s) disagree with the derived running "
            "balance. Either a row was inserted out of order or the table was "
            "written to outside the service - do not reconcile by picking one."
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Liability, valued at the issue rate
# ══════════════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class _Lot:
    """One award, and how much of it is left."""

    occurred_at: datetime
    remaining: int
    rate_per_point: Decimal


@dataclass(frozen=True, slots=True)
class Liability:
    """What is owed, and how it was arrived at."""

    balance_points: int
    rupees: Decimal
    lots: tuple[dict[str, Any], ...] = ()
    unmatched_consumption: int = 0
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "balance_points": self.balance_points,
            "rupees": str(self.rupees),
            "lots": list(self.lots),
            "unmatched_consumption": self.unmatched_consumption,
            "note": self.note,
        }


def liability(entries: list[LedgerEntry]) -> Liability:
    """Outstanding points valued at the rate each award was issued at.

    Redemptions consume the oldest award first. Without FIFO there is no answer
    to "what are the remaining 4,000 points worth" once two rates exist, and a
    number picked any other way is one finance cannot reconcile.

    `unmatched_consumption` is not silently absorbed: redemption beyond the
    awards on record means the ledger is incomplete, and reporting a tidy zero
    would hide it.
    """
    ordered = chronological(entries)
    lots: list[_Lot] = []
    unmatched = 0

    def consume(count: int) -> None:
        nonlocal unmatched
        remaining = count
        for lot in lots:
            if remaining <= 0:
                break
            take = min(lot.remaining, remaining)
            lot.remaining -= take
            remaining -= take
        if remaining > 0:
            unmatched += remaining

    for entry in ordered:
        if entry.transaction_type is TransactionType.AWARDED:
            lots.append(
                _Lot(
                    occurred_at=entry.occurred_at,
                    remaining=entry.points,
                    rate_per_point=entry.rate_per_point,
                )
            )
        elif entry.transaction_type is TransactionType.ADJUSTED:
            if entry.points > 0:
                lots.append(
                    _Lot(
                        occurred_at=entry.occurred_at,
                        remaining=entry.points,
                        rate_per_point=entry.rate_per_point,
                    )
                )
            else:
                consume(-entry.points)
        else:
            consume(entry.points)

    open_lots = [lot for lot in lots if lot.remaining > 0]
    rupees = money(
        sum(
            (Decimal(lot.remaining) * lot.rate_per_point for lot in open_lots),
            Decimal("0"),
        )
    )

    return Liability(
        balance_points=sum(lot.remaining for lot in open_lots),
        rupees=rupees,
        lots=tuple(
            {
                "awarded_at": lot.occurred_at.isoformat(),
                "remaining_points": lot.remaining,
                "rate_per_point": str(lot.rate_per_point),
                "rupees": str(money(Decimal(lot.remaining) * lot.rate_per_point)),
            }
            for lot in open_lots
        ),
        unmatched_consumption=unmatched,
        note=(
            "Valued at the rate each award was issued at, oldest consumed "
            "first. A later rate change does not restate this."
            if not unmatched
            else f"{unmatched} point(s) were redeemed or expired with no award "
            "on record to consume. The ledger is incomplete - this is reported "
            "rather than absorbed, because a tidy total would hide it."
        ),
    )


# ══════════════════════════════════════════════════════════════════════════════
# Movement, in the shape finance reconciles
# ══════════════════════════════════════════════════════════════════════════════


def movement_report(
    entries: list[LedgerEntry],
    *,
    period_start: date,
    period_end: date,
) -> dict[str, Any]:
    """Opening, awarded, redeemed, expired, adjusted, closing.

    The standard accounting shape, and the arithmetic is asserted rather than
    assumed: if opening plus movement does not equal closing, this raises. A
    movement report that does not reconcile is worse than none, because it is
    signed off.
    """
    if period_end < period_start:
        raise LedgerError("A movement period cannot end before it starts.")

    def in_period(e: LedgerEntry) -> bool:
        return period_start <= e.occurred_at.date() <= period_end

    opening = sum(
        e.signed_points for e in entries if e.occurred_at.date() < period_start
    )
    during = [e for e in entries if in_period(e)]

    awarded = sum(
        e.points for e in during if e.transaction_type is TransactionType.AWARDED
    )
    redeemed = sum(
        e.points for e in during if e.transaction_type is TransactionType.REDEEMED
    )
    expired = sum(
        e.points for e in during if e.transaction_type is TransactionType.EXPIRED
    )
    reversed_points = sum(
        e.points for e in during if e.transaction_type is TransactionType.REVERSED
    )
    adjusted = sum(
        e.points for e in during if e.transaction_type is TransactionType.ADJUSTED
    )

    closing = opening + awarded - redeemed - expired - reversed_points + adjusted
    derived_closing = opening + sum(e.signed_points for e in during)

    if closing != derived_closing:
        raise LedgerError(
            f"The movement report does not reconcile: opening {opening} plus "
            f"movement gives {closing}, the ledger gives {derived_closing}. A "
            "movement report that does not add up is worse than none, because "
            "somebody signs it off."
        )

    return {
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "opening": opening,
        "awarded": awarded,
        "redeemed": redeemed,
        "expired": expired,
        "reversed": reversed_points,
        "adjusted": adjusted,
        "closing": closing,
        "reconciles": True,
        "note": (
            "Opening + awarded - redeemed - expired - reversed + adjusted = "
            "closing, checked against the ledger rather than asserted."
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Ageing and provisioning
# ══════════════════════════════════════════════════════════════════════════════

#: Points issued but unredeemed, bucketed. Aligned to a financial year at the
#: top end because that is the boundary a provision is reviewed against.
AGEING_BUCKETS: tuple[tuple[str, int, int], ...] = (
    ("0-90", 0, 90),
    ("91-180", 91, 180),
    ("181-365", 181, 365),
    ("over_365", 366, 10_000),
)


def ageing(
    entries: list[LedgerEntry], *, as_of: date
) -> dict[str, Any]:
    """Unredeemed points by the age of the award that issued them.

    Bucketed rather than listed: the question is "how much of this is about to
    expire or has been sitting for a year", and a list of four hundred awards
    sorted by date does not answer it.
    """
    outstanding = liability(entries)
    buckets: dict[str, dict[str, Any]] = {
        name: {"points": 0, "rupees": Decimal("0")} for name, _, _ in AGEING_BUCKETS
    }

    for lot in outstanding.lots:
        awarded_on = datetime.fromisoformat(lot["awarded_at"]).date()
        age = (as_of - awarded_on).days
        for name, low, high in AGEING_BUCKETS:
            if low <= age <= high:
                buckets[name]["points"] += lot["remaining_points"]
                buckets[name]["rupees"] += Decimal(lot["rupees"])
                break

    return {
        "as_of": as_of.isoformat(),
        "total_points": outstanding.balance_points,
        "total_rupees": str(outstanding.rupees),
        "buckets": {
            name: {"points": v["points"], "rupees": str(money(v["rupees"]))}
            for name, v in buckets.items()
        },
    }


#: Below this many awarded points, a redemption rate is not a rate. A single
#: enthusiastic team redeeming everything would otherwise set the provision for
#: the whole company.
MIN_POINTS_FOR_REDEMPTION_RATE = 1_000


def expected_redemption_rate(
    entries: list[LedgerEntry],
    *,
    minimum_points: int = MIN_POINTS_FOR_REDEMPTION_RATE,
) -> dict[str, Any]:
    """What share of awarded points has historically been redeemed.

    Used for provisioning: a company where 40% of points are never redeemed does
    not owe the full balance, and provisioning at 100% overstates the liability.
    But the rate has to come from enough history to mean something, and it is
    returned as `None` rather than as a confident number below that.

    Expiry is deliberately excluded from the numerator. Expired points were not
    redeemed; counting them as consumption would make an unpopular programme
    look like a well-used one.
    """
    awarded = sum(
        e.points for e in entries if e.transaction_type is TransactionType.AWARDED
    )
    redeemed = sum(
        e.points for e in entries if e.transaction_type is TransactionType.REDEEMED
    )
    expired = sum(
        e.points for e in entries if e.transaction_type is TransactionType.EXPIRED
    )

    if awarded < minimum_points:
        return {
            "awarded": awarded,
            "redeemed": redeemed,
            "expired": expired,
            "rate": None,
            "note": (
                f"{awarded} points awarded, below the {minimum_points} needed "
                "for a redemption rate. Provision at the full balance until "
                "there is enough history - an optimistic rate from a small "
                "sample understates a real liability."
            ),
        }

    rate = round(redeemed / awarded, 3)
    return {
        "awarded": awarded,
        "redeemed": redeemed,
        "expired": expired,
        "rate": rate,
        "note": (
            f"{rate:.1%} of awarded points have been redeemed. Expired points "
            "are excluded from the numerator: they were not redeemed, and "
            "counting them would make an unused programme look healthy."
        ),
    }


def by_group(
    entries: list[LedgerEntry], *, key: str = "department_key"
) -> dict[str, Any]:
    """Outstanding liability per department or cost centre.

    Finance allocates the provision, so the split has to exist. Grouped on the
    key captured on the row at award time rather than resolved live: a
    reorganisation in November must not move last March's liability into a cost
    centre that did not incur it.
    """
    if key not in {"department_key", "cost_centre"}:
        raise LedgerError(
            "Liability splits by department or cost centre. Any finer and the "
            "provision is being allocated to individuals."
        )

    groups: dict[str, list[LedgerEntry]] = {}
    for entry in entries:
        groups.setdefault(getattr(entry, key) or "unassigned", []).append(entry)

    rows = []
    for name, group in sorted(groups.items()):
        result = liability(group)
        rows.append(
            {
                key: name,
                "balance_points": result.balance_points,
                "rupees": str(result.rupees),
            }
        )

    total = liability(entries)
    return {
        "grouped_by": key,
        "groups": rows,
        "total_points": total.balance_points,
        "total_rupees": str(total.rupees),
    }


@dataclass(frozen=True, slots=True)
class ExpiryPolicy:
    """When points expire, and what the employee was told.

    `disclosure` is not decorative and is not defaulted. A programme that awards
    points and quietly expires them in a year has taught its employees that the
    recognition was not real, which costs more than the liability it removed.
    """

    expire_after_days: int
    notify_days_before: int = 30
    disclosure: str = ""
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.enabled and self.expire_after_days < 90:
            raise LedgerError(
                f"An expiry of {self.expire_after_days} days is short enough "
                "that people lose points they were still choosing how to spend. "
                "If that is genuinely the policy, it has to be a deliberate "
                "configuration rather than something this module accepts "
                "quietly."
            )
        if self.enabled and not self.disclosure.strip():
            raise LedgerError(
                "An expiry policy needs a disclosure line shown at the point of "
                "award. Points that quietly vanish teach people the recognition "
                "was not real."
            )
        if self.enabled and self.notify_days_before < 7:
            raise LedgerError(
                "A warning less than a week before expiry is not a warning."
            )


def expiring_soon(
    entries: list[LedgerEntry],
    *,
    policy: ExpiryPolicy,
    as_of: date,
) -> list[dict[str, Any]]:
    """Award lots inside the notification window, so people can be told.

    Named lots rather than a total, because "you have 300 points expiring" and
    "you have 300 points, 40 of which expire on Friday" produce different
    behaviour.
    """
    if not policy.enabled:
        return []

    result = liability(entries)
    out = []
    for lot in result.lots:
        awarded_on = datetime.fromisoformat(lot["awarded_at"]).date()
        age = (as_of - awarded_on).days
        days_left = policy.expire_after_days - age
        if 0 <= days_left <= policy.notify_days_before:
            out.append(
                {
                    "awarded_at": lot["awarded_at"],
                    "points": lot["remaining_points"],
                    "rupees": lot["rupees"],
                    "days_left": days_left,
                    "expires_on": (
                        awarded_on.toordinal() + policy.expire_after_days
                    ),
                }
            )
    for row in out:
        row["expires_on"] = date.fromordinal(row["expires_on"]).isoformat()
    return sorted(out, key=lambda r: r["days_left"])


def expirable(
    entries: list[LedgerEntry], *, policy: ExpiryPolicy, as_of: date
) -> list[dict[str, Any]]:
    """Award lots past the expiry period, for an EXPIRED row to be written.

    Returns what *should* be expired. Nothing here writes or deletes anything —
    expiry is an insert like everything else, and an expired point that vanished
    from the ledger is a liability reduction nobody can audit.
    """
    if not policy.enabled:
        return []

    result = liability(entries)
    out = []
    for lot in result.lots:
        awarded_on = datetime.fromisoformat(lot["awarded_at"]).date()
        if (as_of - awarded_on).days > policy.expire_after_days:
            out.append(
                {
                    "awarded_at": lot["awarded_at"],
                    "points": lot["remaining_points"],
                    "rupees": lot["rupees"],
                    "age_days": (as_of - awarded_on).days,
                }
            )
    return out


@dataclass(frozen=True, slots=True)
class Statement:
    """What one employee sees. Ledger rows, not a single number."""

    employee_id: str
    balance_points: int
    balance_rupees: Decimal
    rows: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    expiring: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "employee_id": self.employee_id,
            "balance_points": self.balance_points,
            "balance_rupees": str(self.balance_rupees),
            "rows": list(self.rows),
            "expiring": list(self.expiring),
            "note": self.note,
        }
