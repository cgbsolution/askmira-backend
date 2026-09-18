"""Leave arithmetic, with no database in sight.

Everything here is a pure function of its arguments, so the rules an employee
will dispute — how many days a request costs, how much has accrued, whether a
request is allowed — can be tested exhaustively without a session.

Days are `Decimal`, never `float`. A balance is not money, but it becomes money
the moment it is encashed at exit, and half-days make binary fractions real.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol, TypeVar

ZERO = Decimal("0")
HALF = Decimal("0.5")
MONTHS_IN_YEAR = 12

#: ISO weekdays (Monday = 1). Used only when the employee has no shift, which
#: is the common case for an office that has never configured attendance.
DEFAULT_WEEKLY_OFF_DAYS: tuple[int, ...] = (6, 7)

#: A joiner on or before this day of the month earns that month's credit.
MID_MONTH_CUTOFF_DAY = 15

ACCRUAL_FREQUENCIES = ("monthly", "quarterly", "annual", "on_joining")


class LeaveRuleError(ValueError):
    """A request that cannot be expressed at all, as opposed to one policy refuses."""


def days(value: Any) -> Decimal:
    """A day quantity from a Numeric column, a string or an int — never a float."""
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ── Day counting ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DayCount:
    total: Decimal
    working_dates: tuple[date, ...]
    weekly_offs: tuple[date, ...]
    holidays: tuple[tuple[date, str], ...]


def count_leave_days(
    start: date,
    end: date,
    *,
    half_day: bool,
    weekly_off_days: Sequence[int],
    holidays: Mapping[date, str],
) -> DayCount:
    """What a request from `start` to `end` costs.

    Weekly offs and holidays inside the range are not charged: taking Friday
    to Monday off is two days of leave, not four. The dates skipped are
    returned so the screen can say why the number is smaller than the range.
    """
    if end < start:
        raise LeaveRuleError("The leave cannot end before it starts.")
    if half_day and start != end:
        raise LeaveRuleError("A half day covers a single date. Pick the same start and end.")

    working: list[date] = []
    offs: list[date] = []
    skipped_holidays: list[tuple[date, str]] = []
    cursor = start
    while cursor <= end:
        if cursor.isoweekday() in weekly_off_days:
            offs.append(cursor)
        elif cursor in holidays:
            skipped_holidays.append((cursor, holidays[cursor]))
        else:
            working.append(cursor)
        cursor += timedelta(days=1)

    total = (HALF if working else ZERO) if half_day else Decimal(len(working))
    return DayCount(
        total=total,
        working_dates=tuple(working),
        weekly_offs=tuple(offs),
        holidays=tuple(skipped_holidays),
    )


# ── Accrual ─────────────────────────────────────────────────────────────────


def _months_credited(year: int, as_of: date, joined_on: date | None) -> int:
    """Months of `year` that have started by `as_of` and that the person was here for."""
    if as_of.year < year:
        return 0
    last_month = MONTHS_IN_YEAR if as_of.year > year else as_of.month

    first_month = 1
    if joined_on is not None:
        if joined_on.year > year:
            return 0
        if joined_on.year == year:
            first_month = joined_on.month
            if joined_on.day > MID_MONTH_CUTOFF_DAY:
                first_month += 1
    return max(0, last_month - first_month + 1)


def accrued_to_date(
    *,
    annual_quota: Decimal,
    frequency: str,
    year: int,
    as_of: date,
    joined_on: date | None,
) -> Decimal:
    """Days earned in a leave year up to `as_of`.

    Credits land at the start of each period, which is how Indian policies are
    usually written ("1.5 days on the 1st of every month"). A mid-year joiner
    earns only the periods they were employed for; a joiner after the 15th
    starts from the following month.
    """
    if frequency not in ACCRUAL_FREQUENCIES:
        raise LeaveRuleError(f"Unknown accrual frequency '{frequency}'.")
    quota = days(annual_quota)
    if quota <= ZERO:
        return ZERO

    if frequency == "on_joining":
        employed = joined_on is None or joined_on <= as_of
        return quota if as_of.year >= year and employed else ZERO

    months = _months_credited(year, as_of, joined_on)
    if frequency == "monthly":
        return _quantize(quota * months / MONTHS_IN_YEAR)

    if frequency == "quarterly":
        if months == 0:
            return ZERO
        # Each started quarter credits a quarter of the quota, pro-rated by the
        # months of it the person is employed for.
        last_month = MONTHS_IN_YEAR if as_of.year > year else as_of.month
        first_month = last_month - months + 1
        total = ZERO
        for quarter_start in (1, 4, 7, 10):
            if quarter_start > last_month:
                break
            quarter_end = quarter_start + 2
            served = max(0, quarter_end - max(quarter_start, first_month) + 1)
            total += quota / 4 * served / 3
        return _quantize(total)

    # annual: the whole year's credit on day one, pro-rated for a mid-year joiner.
    if as_of.year < year:
        return ZERO
    service_months = _months_credited(year, date(year, 12, 31), joined_on)
    return _quantize(quota * service_months / MONTHS_IN_YEAR)


# ── Balances ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Balance:
    opening: Decimal = ZERO
    accrued: Decimal = ZERO
    carried_forward: Decimal = ZERO
    used: Decimal = ZERO
    encashed: Decimal = ZERO
    #: Requested but not yet decided. Not deducted from `available`, but a new
    #: request is checked against what would remain if they were all approved.
    pending: Decimal = ZERO

    @property
    def available(self) -> Decimal:
        """Same formula as the encashment ledger reads, so the two cannot disagree."""
        return self.opening + self.accrued + self.carried_forward - self.used - self.encashed

    @property
    def available_after_pending(self) -> Decimal:
        return self.available - self.pending


# ── Policy ──────────────────────────────────────────────────────────────────


class PolicyLike(Protocol):
    location_id: uuid.UUID | None
    grade: str | None
    effective_from: date
    effective_to: date | None
    is_active: bool


P = TypeVar("P", bound=PolicyLike)


def select_policy(
    policies: Sequence[P],
    *,
    location_id: uuid.UUID | None,
    grade: str | None,
    on: date,
) -> P | None:
    """The most specific active policy covering this person on this date.

    A policy naming both location and grade beats one naming either, which
    beats the catch-all. Among equals, the most recent version wins.
    """
    best: P | None = None
    best_key: tuple[int, date] | None = None
    for policy in policies:
        if not policy.is_active or policy.effective_from > on:
            continue
        if policy.effective_to is not None and policy.effective_to < on:
            continue
        if policy.location_id is not None and policy.location_id != location_id:
            continue
        if policy.grade is not None and policy.grade != grade:
            continue
        specificity = (policy.location_id is not None) + (policy.grade is not None)
        key = (specificity, policy.effective_from)
        if best_key is None or key > best_key:
            best, best_key = policy, key
    return best


@dataclass(frozen=True)
class PolicyRules:
    min_notice_days: int = 0
    max_consecutive_days: int | None = None
    allows_negative_balance: bool = False


@dataclass
class Assessment:
    """Whether a request may be submitted, and what the person should know first."""

    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return not self.problems


def assess_request(
    *,
    count: DayCount,
    start: date,
    today: date,
    half_day: bool,
    allows_half_day: bool,
    requires_document: bool,
    has_document: bool,
    rules: PolicyRules | None,
    balance: Balance | None,
    overlapping: int,
) -> Assessment:
    """Every rule a request is held to, in the order a person would fix them."""
    result = Assessment()

    if half_day and not allows_half_day:
        result.problems.append("This leave type cannot be taken as a half day.")
    if count.total == ZERO:
        result.problems.append(
            "Those dates are all weekly offs or holidays, so there is nothing to request."
        )
    if overlapping:
        result.problems.append(
            "You already have a pending or approved request covering some of these dates."
        )
    if requires_document and not has_document:
        result.problems.append("This leave type needs a supporting document.")

    if rules is None:
        result.problems.append(
            "No leave policy covers you for this leave type yet. Ask HR to assign one."
        )
        return result

    if rules.min_notice_days and start >= today:
        notice = (start - today).days
        if notice < rules.min_notice_days:
            result.problems.append(
                f"This leave type needs {rules.min_notice_days} days' notice; "
                f"this request gives {notice}."
            )
    if start < today:
        result.warnings.append("These dates are in the past. Your approver will see that.")

    if rules.max_consecutive_days is not None and count.total > rules.max_consecutive_days:
        result.problems.append(
            f"At most {rules.max_consecutive_days} consecutive days can be taken at once."
        )

    if balance is not None and count.total > ZERO:
        remaining = balance.available_after_pending - count.total
        if remaining < ZERO:
            if rules.allows_negative_balance:
                result.warnings.append(
                    f"This takes your balance to {remaining} days. Your policy allows that."
                )
            else:
                result.problems.append(
                    f"You have {balance.available_after_pending} days available after "
                    f"pending requests; this request needs {count.total}."
                )
    return result


# ── Approval chain ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Approver:
    #: "manager" or "hr". An "hr" step with no user can be decided by anybody in HR.
    role: str
    user_id: uuid.UUID | None
    employee_id: uuid.UUID | None
    name: str


def build_chain(approvers: Sequence[Approver]) -> list[dict[str, Any]]:
    """The chain snapshotted onto the request at submission.

    Stored as plain JSON so a later reorg cannot change who was asked.
    """
    if not approvers:
        raise LeaveRuleError("A leave request needs at least one approver.")
    return [
        {
            "step": index + 1,
            "role": approver.role,
            "approver_user_id": str(approver.user_id) if approver.user_id else None,
            "approver_employee_id": str(approver.employee_id) if approver.employee_id else None,
            "approver_name": approver.name,
            "status": "pending",
            "decided_by_user_id": None,
            "decided_by_name": None,
            "decided_at": None,
            "note": None,
        }
        for index, approver in enumerate(approvers)
    ]


def current_step(chain: Sequence[Mapping[str, Any]]) -> int | None:
    """Index of the first undecided step, or None when the chain is finished."""
    for index, step in enumerate(chain):
        if step.get("status") == "pending":
            return index
    return None


def apply_decision(
    chain: Sequence[Mapping[str, Any]],
    *,
    approve: bool,
    actor_user_id: uuid.UUID,
    actor_name: str,
    note: str | None,
    at: datetime,
) -> tuple[list[dict[str, Any]], str]:
    """Record a decision on the current step.

    Returns the new chain and the outcome: "advanced" when a later step is
    still waiting, "approved" when the last step approved, "rejected" when any
    step declined. A rejection closes the chain; later steps are marked
    "skipped" rather than left pending, so nobody is asked to decide a request
    that is already over.
    """
    index = current_step(chain)
    if index is None:
        raise LeaveRuleError("This request has already been decided.")

    updated = [dict(step) for step in chain]
    updated[index].update(
        status="approved" if approve else "rejected",
        decided_by_user_id=str(actor_user_id),
        decided_by_name=actor_name,
        decided_at=at.isoformat(),
        note=note,
    )
    if not approve:
        for later in updated[index + 1 :]:
            later["status"] = "skipped"
        return updated, "rejected"
    if current_step(updated) is not None:
        return updated, "advanced"
    return updated, "approved"
