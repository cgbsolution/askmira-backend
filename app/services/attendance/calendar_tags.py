"""The colour each day wears on the attendance calendar.

HR teams read a month at a glance by eight tags they know from older HRMS
calendars: Full day, Partial, Absent / missed punch, Week off, Holiday,
Optional holiday, Approved leave and Unapproved leave. This maps what
attendance already decided onto them, so the portal and the app colour a day
the same way and neither one guesses.

- A computed day wears its status. Present is Full day, half day is Partial,
  absent or a missed punch (needs regularisation) is Absent, approved leave —
  paid, or without pay (LOP) — is Approved leave, a holiday is Holiday or
  Optional holiday (one the person opted in to), a week off is Week off.
- An absent or missed-punch day with a leave request still waiting is
  Unapproved leave: the person has asked for it, a decision is due.
- Today is not over. It shows as absent only once HR has marked it so; until
  then it stays blank rather than red.
- A day with no computed row (today, the future, a locked month) shows only
  what is known in advance, in the order the computation applies it: holiday,
  then the week off of the shift in force that day, then approved leave, then
  pending leave. A working day nobody has judged yet has no tag.
- Days before the person joined have no tag.
"""

from __future__ import annotations

from datetime import date

from app.models.attendance import AttendanceStatus
from app.schemas.attendance import DayTag

__all__ = ["DayTag", "day_tag"]


_NOT_THERE = {AttendanceStatus.ABSENT, AttendanceStatus.NEEDS_REGULARISATION}


def day_tag(
    *,
    day: date,
    today: date,
    status: AttendanceStatus | None,
    is_regularised: bool = False,
    holiday_is_optional: bool | None = None,
    leave_approved: bool = False,
    leave_pending: bool = False,
    scheduled_off: bool = False,
    date_of_joining: date | None = None,
) -> DayTag | None:
    """The tag for one day, or None when there is nothing to say yet.

    `holiday_is_optional` is None when no holiday applies to the person that
    day, else whether the one that applies is an optional holiday they chose.
    `scheduled_off` is whether the shift (or roster) in force that day has it
    as a weekly off.
    """
    if date_of_joining is not None and day < date_of_joining:
        return None

    if status is not None:
        if status in _NOT_THERE:
            if leave_pending:
                return DayTag.UNAPPROVED_LEAVE
            if day >= today and not is_regularised:
                return None
            return DayTag.ABSENT
        if status is AttendanceStatus.PRESENT:
            return DayTag.FULL_DAY
        if status is AttendanceStatus.HALF_DAY:
            return DayTag.PARTIAL
        if status in {AttendanceStatus.ON_LEAVE, AttendanceStatus.LOP}:
            return DayTag.APPROVED_LEAVE
        if status is AttendanceStatus.HOLIDAY:
            return DayTag.OPTIONAL_HOLIDAY if holiday_is_optional else DayTag.HOLIDAY
        if status is AttendanceStatus.WEEK_OFF:
            return DayTag.WEEK_OFF
        return None

    if holiday_is_optional is not None:
        return DayTag.OPTIONAL_HOLIDAY if holiday_is_optional else DayTag.HOLIDAY
    if scheduled_off:
        return DayTag.WEEK_OFF
    if leave_approved:
        return DayTag.APPROVED_LEAVE
    if leave_pending:
        return DayTag.UNAPPROVED_LEAVE
    return None
