"""Which holidays apply to whom.

One resolver for every module that needs "is this date a holiday for this
person": attendance marks the day HOLIDAY, leave skips it when counting days,
the calendars show it. Before this existed attendance applied every holiday in
the tenant to everyone — a Karnataka-only holiday became a paid day off in
Maharashtra, and an optional holiday became a holiday for all.

A holiday applies to an employee when it is mandatory and either national
(no state, no location), for the state of their work location, or for their
work location itself. An optional (restricted) holiday applies only to the
people who opted in to it (`OptionalHolidaySelection`), and only where it would
otherwise apply to them.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.employee import Employee
from app.models.leave import Holiday, OptionalHolidaySelection
from app.models.org import Location
from app.models.user import User

__all__ = ["holiday_applies", "holidays_for_employees"]


def holiday_applies(
    holiday: Holiday,
    *,
    state_code: str | None,
    location_id: uuid.UUID | None,
    include_optional: bool = False,
) -> bool:
    """Whether a holiday covers someone at this state and location.

    Optional holidays never apply by place alone; pass `include_optional` to
    ask only "is this person's place in its scope" (to offer it, or to honour
    an opt-in).
    """
    if holiday.is_optional and not include_optional:
        return False
    if holiday.location_id is not None:
        return holiday.location_id == location_id
    if holiday.state_code is not None:
        return state_code is not None and holiday.state_code == state_code
    return True


async def holidays_for_employees(
    session: AsyncSession,
    user: User,
    employee_ids: list[uuid.UUID],
    start: date,
    end: date,
) -> dict[uuid.UUID, dict[date, str]]:
    """Holidays per employee over a date range, as `{id: {date: name}}`.

    Mandatory holidays for their place, plus the optional ones they opted in
    to. Three queries regardless of headcount: the employees' locations, the
    tenant's holidays in the range, and the opt-ins in the range.
    """
    if not employee_ids:
        return {}

    places = (
        await session.execute(
            tenant_scoped(
                select(Employee.id, Employee.location_id, Location.state_code)
                .outerjoin(Location, Location.id == Employee.location_id)
                .where(Employee.id.in_(employee_ids)),
                Employee,
                user,
            )
        )
    ).all()

    holidays = (
        await session.scalars(
            tenant_scoped(
                select(Holiday).where(
                    Holiday.holiday_date >= start,
                    Holiday.holiday_date <= end,
                ),
                Holiday,
                user,
            )
        )
    ).all()

    chosen: set[tuple[uuid.UUID, uuid.UUID]] = set()
    if any(h.is_optional for h in holidays):
        chosen = {
            (employee_id, holiday_id)
            for employee_id, holiday_id in (
                await session.execute(
                    tenant_scoped(
                        select(
                            OptionalHolidaySelection.employee_id,
                            OptionalHolidaySelection.holiday_id,
                        ).where(
                            OptionalHolidaySelection.employee_id.in_(employee_ids),
                            OptionalHolidaySelection.holiday_id.in_(
                                [h.id for h in holidays if h.is_optional]
                            ),
                        ),
                        OptionalHolidaySelection,
                        user,
                    )
                )
            ).all()
        }

    result: dict[uuid.UUID, dict[date, str]] = defaultdict(dict)
    for employee_id, location_id, state_code in places:
        for holiday in holidays:
            if holiday.is_optional:
                applies = (employee_id, holiday.id) in chosen and holiday_applies(
                    holiday,
                    state_code=state_code,
                    location_id=location_id,
                    include_optional=True,
                )
            else:
                applies = holiday_applies(holiday, state_code=state_code, location_id=location_id)
            if applies:
                result[employee_id][holiday.holiday_date] = holiday.name
    return {employee_id: dict(result.get(employee_id, {})) for employee_id in employee_ids}
