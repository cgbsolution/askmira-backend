"""Leave arithmetic, with no database in sight.

Everything here is a pure function of its arguments, so the rules an employee
will dispute — how many days a request costs, how much has accrued, whether a
request is allowed — can be tested exhaustively without a session.

Days are `Decimal`, never `float`. A balance is not money, but it becomes money
the moment it is encashed at exit, and half-days make binary fractions real.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol, TypeVar

from dateutil.relativedelta import relativedelta

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


def days_text(value: Decimal) -> str:
    """A day quantity for a sentence: "8", "0.5", never "8.00"."""
    return format(value.normalize(), "f") if value else "0"


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ── Day counting ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DayCount:
    total: Decimal
    working_dates: tuple[date, ...]
    #: Weekly offs and holidays in the range that are *not* charged.
    weekly_offs: tuple[date, ...]
    holidays: tuple[tuple[date, str], ...]
    #: Offs and holidays charged anyway under the sandwich rule.
    sandwiched: tuple[date, ...] = ()


def count_leave_days(
    start: date,
    end: date,
    *,
    half_day: bool,
    weekly_off_days: Sequence[int] | Callable[[date], bool],
    holidays: Mapping[date, str],
    sandwich: bool = False,
) -> DayCount:
    """What a request from `start` to `end` costs.

    Weekly offs and holidays inside the range are not charged: taking Friday
    to Monday off is two days of leave, not four. The dates skipped are
    returned so the screen can say why the number is smaller than the range.

    Under the sandwich rule (a policy switch) an off or holiday that falls
    *between* two days of leave in the same request is charged: Friday to
    Monday is four days. One at either end of the range is still not charged —
    nothing sandwiches it.
    """
    if end < start:
        raise LeaveRuleError("The leave cannot end before it starts.")
    if half_day and start != end:
        raise LeaveRuleError("A half day covers a single date. Pick the same start and end.")

    # A weekday set (every week), or the person's own rule per date, which is
    # what an alternate-Saturday week needs.
    is_off: Callable[[date], bool] = (
        weekly_off_days
        if callable(weekly_off_days)
        else (lambda day, days=tuple(weekly_off_days): day.isoweekday() in days)
    )
    working: list[date] = []
    offs: list[date] = []
    skipped_holidays: list[tuple[date, str]] = []
    cursor = start
    while cursor <= end:
        if is_off(cursor):
            offs.append(cursor)
        elif cursor in holidays:
            skipped_holidays.append((cursor, holidays[cursor]))
        else:
            working.append(cursor)
        cursor += timedelta(days=1)

    sandwiched: list[date] = []
    if sandwich and not half_day and len(working) >= 2:
        first, last = working[0], working[-1]
        sandwiched = sorted(
            [d for d in offs if first < d < last]
            + [d for d, _ in skipped_holidays if first < d < last]
        )
        offs = [d for d in offs if not first < d < last]
        skipped_holidays = [(d, n) for d, n in skipped_holidays if not first < d < last]

    total = (HALF if working else ZERO) if half_day else Decimal(len(working) + len(sandwiched))
    return DayCount(
        total=total,
        working_dates=tuple(working),
        weekly_offs=tuple(offs),
        holidays=tuple(skipped_holidays),
        sandwiched=tuple(sandwiched),
    )


def overlapping_requests(
    *,
    start: date,
    end: date,
    half_day: bool,
    session: str | None,
    existing: Sequence[tuple[date, date, bool, str | None]],
) -> int:
    """How many open requests clash with this one.

    `existing` is `(start, end, is_half_day, half_day_session)` for each open
    request of the same person. Two half days on the same date do not clash
    when one is the first half and the other the second — that is a full day
    taken as two halves, often of two different leave types. A half day with no
    session (raised before sessions existed) clashes with any half day.
    """
    clashes = 0
    for other_start, other_end, other_half, other_session in existing:
        if other_start > end or other_end < start:
            continue
        halves_of_one_day = (
            half_day
            and other_half
            and start == end == other_start == other_end
            and session is not None
            and other_session is not None
            and session != other_session
        )
        if not halves_of_one_day:
            clashes += 1
    return clashes


# ── Accrual ─────────────────────────────────────────────────────────────────


def _service_span(
    year: int, joined_on: date | None, first: int = 1, last: int = MONTHS_IN_YEAR
) -> tuple[int, int] | None:
    """The months of `year`, within `first`..`last`, the person earns leave in.

    A joiner on or before the 15th earns their first month; after it, they
    start from the next. None when they earn nothing in the range.
    """
    start = first
    if joined_on is not None:
        if joined_on.year > year:
            return None
        if joined_on.year == year:
            joined_month = joined_on.month + (1 if joined_on.day > MID_MONTH_CUTOFF_DAY else 0)
            start = max(start, joined_month)
    return (start, last) if start <= last else None


def _months_credited(
    year: int, as_of: date, joined_on: date | None, first: int = 1, last: int = MONTHS_IN_YEAR
) -> int:
    """Months of `year` (within `first`..`last`) started by `as_of` that the person was here for."""
    if as_of.year < year:
        return 0
    started = MONTHS_IN_YEAR if as_of.year > year else as_of.month
    span = _service_span(year, joined_on, first, min(last, started))
    return 0 if span is None else span[1] - span[0] + 1


def accrued_to_date(
    *,
    annual_quota: Decimal,
    frequency: str,
    year: int,
    as_of: date,
    joined_on: date | None,
    months: tuple[int, int] = (1, MONTHS_IN_YEAR),
) -> Decimal:
    """Days earned in a leave year up to `as_of`.

    Credits land at the start of each period, which is how Indian policies are
    usually written ("1.5 days on the 1st of every month"). A mid-year joiner
    earns only the periods they were employed for; a joiner after the 15th
    starts from the following month.

    `months` limits the credit to the months a policy version governs (see
    `accrual_windows`): a policy that starts in November earns nothing for
    January to October. `joined_on` is the day the person starts earning —
    their joining date, or the end of probation when leave waits for it.
    """
    if frequency not in ACCRUAL_FREQUENCIES:
        raise LeaveRuleError(f"Unknown accrual frequency '{frequency}'.")
    quota = days(annual_quota)
    if quota <= ZERO or as_of.year < year:
        return ZERO
    first, last = months
    started = MONTHS_IN_YEAR if as_of.year > year else as_of.month

    if frequency == "on_joining":
        employed = joined_on is None or joined_on <= as_of
        return quota if employed and first <= started else ZERO

    if frequency == "monthly":
        return _quantize(
            quota * _months_credited(year, as_of, joined_on, first, last) / MONTHS_IN_YEAR
        )

    span = _service_span(year, joined_on, first, last)
    if span is None:
        return ZERO
    start, end = span

    if frequency == "quarterly":
        # Each started quarter credits a quarter of the quota, pro-rated by the
        # months of it the person is employed for (and the policy governs).
        total = ZERO
        for quarter_start in (1, 4, 7, 10):
            low, high = max(quarter_start, start), min(quarter_start + 2, end)
            if low > high:
                continue
            if low > started:
                break
            total += quota / 4 * (high - low + 1) / 3
        return _quantize(total)

    # annual: the whole period's credit up front, pro-rated for a mid-year
    # joiner — and not before the person starts earning (end of probation).
    if first > started or (joined_on is not None and joined_on > as_of):
        return ZERO
    return _quantize(quota * (end - start + 1) / MONTHS_IN_YEAR)


class AccruingPolicy(Protocol):
    location_id: uuid.UUID | None
    grade: str | None
    effective_from: date
    effective_to: date | None
    is_active: bool
    annual_quota_days: Any
    accrual_frequency: str


A = TypeVar("A", bound=AccruingPolicy)


def accrual_windows(
    policies: Sequence[A],
    *,
    year: int,
    location_id: uuid.UUID | None,
    grade: str | None,
    gender: str | None = None,
) -> list[tuple[A, int, int]]:
    """Which policy version earns each month of `year`: `(policy, first, last)` runs.

    A month belongs to the version in force on its 15th — the same cut-off a
    joiner gets — so a new version starting on 1 November earns November and
    December, the one it replaced earns January to October, and no month is
    counted twice or lost. Months no policy covers earn nothing.
    """
    windows: list[tuple[A, int, int]] = []
    for month in range(1, MONTHS_IN_YEAR + 1):
        policy = select_policy(
            policies,
            location_id=location_id,
            grade=grade,
            gender=gender,
            on=date(year, month, MID_MONTH_CUTOFF_DAY),
        )
        if policy is None:
            continue
        if windows and windows[-1][0] is policy and windows[-1][2] == month - 1:
            windows[-1] = (policy, windows[-1][1], month)
        else:
            windows.append((policy, month, month))
    return windows


def accrued_across(
    windows: Sequence[tuple[AccruingPolicy, int, int]],
    *,
    year: int,
    as_of: date,
    joined_on: date | None,
) -> Decimal:
    """Days earned in `year` up to `as_of` under every version that governed it.

    A one-off "on joining" credit is given once, however many versions there were.
    """
    total = ZERO
    joining_credited = False
    for policy, first, last in windows:
        if policy.accrual_frequency == "on_joining" and joining_credited:
            continue
        earned = accrued_to_date(
            annual_quota=days(policy.annual_quota_days),
            frequency=policy.accrual_frequency,
            year=year,
            as_of=as_of,
            joined_on=joined_on,
            months=(first, last),
        )
        if policy.accrual_frequency == "on_joining" and earned > ZERO:
            joining_credited = True
        total += earned
    return total


def accrual_start(
    date_of_joining: date | None, probation_months: int, *, exempt: bool = False
) -> date | None:
    """The day somebody starts earning leave.

    Their joining date, or — when the workspace holds leave back for probation —
    that many months after it. Statutory leave (maternity) is never held back:
    the law, not the company, decides who is entitled.
    """
    if date_of_joining is None or exempt or probation_months <= 0:
        return date_of_joining
    return date_of_joining + relativedelta(months=probation_months)


def on_probation_until(
    date_of_joining: date | None, probation_months: int, today: date
) -> date | None:
    """The day leave starts, while somebody is still on probation; None after it."""
    start = accrual_start(date_of_joining, probation_months)
    if start is None or probation_months <= 0 or start <= today:
        return None
    return start


def schedule_from(
    total_at: Callable[[date], Decimal], *, year: int, as_of: date, joined_on: date | None
) -> list[tuple[date, Decimal]]:
    """Each credit behind a running accrual total, dated, for the balance history.

    Built from the total at each month start, so the entries always add up to
    exactly what the balance says — the history and the card cannot disagree
    by a rounding paisa.
    """
    if as_of < date(year, 1, 1):
        return []
    points = [date(year, m, 1) for m in range(1, 13) if date(year, m, 1) <= as_of]
    # An up-front credit that waits for the start date (probation) lands on it.
    if joined_on is not None and joined_on.year == year and joined_on <= as_of:
        points = sorted({*points, joined_on})
    entries: list[tuple[date, Decimal]] = []
    running = ZERO
    for point in points:
        total = total_at(point)
        if total != running:
            shown = joined_on if joined_on is not None and joined_on > point else point
            entries.append((min(shown, as_of), total - running))
            running = total
    final = total_at(as_of)
    if final != running:
        entries.append((as_of, final - running))
    return entries


def accrual_schedule(
    *,
    annual_quota: Decimal,
    frequency: str,
    year: int,
    as_of: date,
    joined_on: date | None,
) -> list[tuple[date, Decimal]]:
    """Each credit behind `accrued_to_date` for one policy, dated."""
    if days(annual_quota) <= ZERO:
        return []
    return schedule_from(
        lambda on: accrued_to_date(
            annual_quota=annual_quota,
            frequency=frequency,
            year=year,
            as_of=on,
            joined_on=joined_on,
        ),
        year=year,
        as_of=as_of,
        joined_on=joined_on,
    )


# ── Balances ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Balance:
    opening: Decimal = ZERO
    accrued: Decimal = ZERO
    carried_forward: Decimal = ZERO
    used: Decimal = ZERO
    encashed: Decimal = ZERO
    #: HR credits and debits, comp-off credits and expiries, and the year-end
    #: carry-out and lapse — every movement in the adjustment ledger that is
    #: not opening balance, carry-in or encashment.
    adjusted: Decimal = ZERO
    #: Requested but not yet decided. Not deducted from `available`, but a new
    #: request is checked against what would remain if they were all approved.
    pending: Decimal = ZERO

    @property
    def available(self) -> Decimal:
        """Same formula as the encashment ledger reads, so the two cannot disagree."""
        return (
            self.opening
            + self.accrued
            + self.carried_forward
            + self.adjusted
            - self.used
            - self.encashed
        )

    @property
    def available_after_pending(self) -> Decimal:
        return self.available - self.pending


def year_end_split(available: Decimal, max_carry: Decimal | None) -> tuple[Decimal, Decimal]:
    """`(carried, lapsed)` when a leave year closes.

    Up to `max_carry` moves into the new year and the rest lapses. `None` means
    uncapped — comp-off, whose credits lapse by their own expiry date instead.
    A negative balance carries nothing and lapses nothing: the deficit stays on
    the year it was taken in.
    """
    if available <= ZERO:
        return ZERO, ZERO
    if max_carry is None:
        return available, ZERO
    carried = min(available, max(ZERO, days(max_carry)))
    return carried, available - carried


def comp_off_lapse(*, available: Decimal, expiring: Decimal, still_valid: Decimal) -> Decimal:
    """Comp-off days that lapse now, taking the oldest credit as used first.

    `available` is the comp-off balance today, `expiring` the credits whose
    expiry has passed and that have not been processed, `still_valid` the
    credits not yet expired. Whatever the balance holds beyond the valid
    credits must be left over from the expiring ones — up to their size.
    """
    if expiring <= ZERO:
        return ZERO
    return max(ZERO, min(expiring, available - still_valid))


# ── Policy ──────────────────────────────────────────────────────────────────


class PolicyLike(Protocol):
    location_id: uuid.UUID | None
    grade: str | None
    effective_from: date
    effective_to: date | None
    is_active: bool


P = TypeVar("P", bound=PolicyLike)


#: Genders a policy can be limited to. Anything else on an employee record
#: (non-binary, undisclosed, nothing) excludes them from neither.
BINARY_GENDERS = ("female", "male")


def gender_excludes(policy_gender: str | None, employee_gender: str | None) -> bool:
    """Whether a gender-limited policy shuts this person out.

    Only a recorded gender that is the *other* one excludes. A missing or
    undisclosed gender never does: a woman whose record is incomplete must not
    lose maternity leave to a data gap.
    """
    if policy_gender is None or employee_gender not in BINARY_GENDERS:
        return False
    return employee_gender != policy_gender


def select_policy(
    policies: Sequence[P],
    *,
    location_id: uuid.UUID | None,
    grade: str | None,
    on: date,
    gender: str | None = None,
) -> P | None:
    """The most specific active policy covering this person on this date.

    A policy naming both location and grade beats one naming either, which
    beats the catch-all. Among equals, the most recent version wins; on a tie
    of start date too, the later one in `policies` wins, so a caller passing
    them in creation order gets the correction rather than what it corrected.
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
        policy_gender = getattr(policy, "applies_to_gender", None)
        if gender_excludes(policy_gender, gender):
            continue
        specificity = (
            (policy.location_id is not None)
            + (policy.grade is not None)
            + (policy_gender is not None)
        )
        key = (specificity, policy.effective_from)
        if best_key is None or key >= best_key:
            best, best_key = policy, key
    return best


@dataclass(frozen=True)
class PolicyRules:
    min_notice_days: int = 0
    max_consecutive_days: int | None = None


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
    """Every rule a request is held to, in the order a person would fix them.

    Leave is applied only against a balance: every type, paid or unpaid, needs
    enough available days after pending requests, and none can go below zero.
    Somebody with nothing left cannot apply; a day they do not work is absent,
    which attendance and payroll treat as loss of pay.
    """
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
            "No leave policy covers you for this leave type, so there is no balance to apply "
            "against. Ask HR to assign one."
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

    if count.total > ZERO:
        left = balance.available_after_pending if balance is not None else ZERO
        if left <= ZERO:
            result.problems.append(
                "No balance is available for this leave type, so it cannot be applied. "
                "Ask HR if this looks wrong."
            )
        elif left < count.total:
            result.problems.append(
                f"You have {days_text(left)} days available after pending requests; "
                f"this request needs {days_text(count.total)}."
            )
    return result


# ── Approval chain ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Approver:
    #: "manager", "skip_level", "approver" or "hr". An "hr" step with no user can
    #: be decided by anybody in HR.
    role: str
    user_id: uuid.UUID | None
    employee_id: uuid.UUID | None
    name: str
    #: From the approval rule: hours before the step goes up a level.
    escalate_after_hours: int | None = None


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
            "escalate_after_hours": approver.escalate_after_hours,
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
