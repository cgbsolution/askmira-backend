"""Who may work a shift: at its site, with the courses it requires.

A shift can belong to a plant or site; only people based there may be put on
it. And it can name courses — forklift safety, fire warden, first aid — that
anyone working it must have completed, with the completion still in date on
the day. Assigning the shift, a roster that contains it, or approving a swap
onto it is refused for somebody who does not meet both, naming what is
missing. The certificates come from Learning; nothing here decides competence
on its own.
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.attendance import Shift
from app.models.learning import AssignmentStatus, Course, CourseAssignment
from app.models.user import User

__all__ = ["check_course_ids", "missing_for", "require_qualified"]


async def check_course_ids(
    session: AsyncSession, user: User, course_ids: list[uuid.UUID]
) -> list[str]:
    """The ids as stored, after checking each is a course in this workspace."""
    if not course_ids:
        return []
    found = set(
        (
            await session.scalars(
                tenant_scoped(select(Course.id).where(Course.id.in_(course_ids)), Course, user)
            )
        ).all()
    )
    if set(course_ids) - found:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A required course is not one of this workspace's courses.",
        )
    return [str(c) for c in dict.fromkeys(course_ids)]


async def missing_for(
    session: AsyncSession, user: User, employee_id: uuid.UUID, shift: Shift, on: date
) -> list[str]:
    """Titles of the shift's required courses this person does not hold, valid on `on`."""
    required = [uuid.UUID(c) for c in (shift.required_course_ids or [])]
    if not required:
        return []
    held = set(
        (
            await session.scalars(
                tenant_scoped(
                    select(CourseAssignment.course_id).where(
                        CourseAssignment.employee_id == employee_id,
                        CourseAssignment.course_id.in_(required),
                        CourseAssignment.status == AssignmentStatus.COMPLETED,
                        or_(
                            CourseAssignment.expires_on.is_(None),
                            CourseAssignment.expires_on > on,
                        ),
                    ),
                    CourseAssignment,
                    user,
                )
            )
        ).all()
    )
    lacking = [c for c in required if c not in held]
    if not lacking:
        return []
    titles = dict(
        (
            await session.execute(
                tenant_scoped(
                    select(Course.id, Course.title).where(Course.id.in_(lacking)), Course, user
                )
            )
        ).all()
    )
    return [titles.get(c, "a required course") for c in lacking]


async def require_qualified(
    session: AsyncSession,
    user: User,
    employee_id: uuid.UUID,
    shifts: list[Shift],
    on: date,
    *,
    who: str,
) -> None:
    sited = [s for s in shifts if s.location_id is not None]
    if sited:
        from app.models.employee import Employee
        from app.models.org import Location

        based_at = await session.scalar(
            tenant_scoped(
                select(Employee.location_id).where(Employee.id == employee_id), Employee, user
            )
        )
        for shift in sited:
            if based_at != shift.location_id:
                names = dict(
                    (
                        await session.execute(
                            tenant_scoped(
                                select(Location.id, Location.name).where(
                                    Location.id.in_([x for x in (shift.location_id, based_at) if x])
                                ),
                                Location,
                                user,
                            )
                        )
                    ).all()
                )
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"{who} cannot work {shift.name}: it runs at "
                        f"{names.get(shift.location_id, 'another site')}, and they are based "
                        f"at {names.get(based_at, 'no location') if based_at else 'no location'}."
                    ),
                )
    for shift in shifts:
        missing = await missing_for(session, user, employee_id, shift, on)
        if missing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"{who} cannot work {shift.name}: it needs {', '.join(missing)}, completed "
                    f"and valid on {on:%d %b %Y}."
                ),
            )
