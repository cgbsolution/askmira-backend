"""Performance notifications: the event keys, their starter wording, and delivery.

Every stage transition, reminder and escalation in the performance module goes
through `send`, which hands the event to `app.services.notifications.dispatch`
inside the caller's transaction (`commit=False`). The mutation, its audit row
and the notification row are durable together or not at all.

The dispatch pipeline suppresses an event that has no template for a channel.
A workspace that never seeded performance wording would therefore silently get
nothing, so `ensure_templates` installs the starter wording the first time an
event fires — once, and never over wording HR has already edited.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_filter, tenant_scoped
from app.models.notifications import Channel, NotificationTemplate
from app.models.user import User
from app.services.notifications import dispatch

__all__ = [
    "EVENTS",
    "ensure_templates",
    "hr_user_ids",
    "send",
    "user_ids_for_employees",
]

#: event key -> (subject, body). Placeholders are filled from the payload.
EVENTS: dict[str, tuple[str, str]] = {
    "performance.review.launched": (
        "Your {cycle_name} review has started",
        "Your {cycle_name} review is open. The first step is {stage_label}.",
    ),
    "performance.review.stage_open": (
        "{stage_label} is waiting for you",
        "The {stage_label} step of {employee_name}'s {cycle_name} review is now with you.",
    ),
    "performance.review.returned": (
        "A review was sent back to {stage_label}",
        "{employee_name}'s {cycle_name} review was returned to {stage_label}: {reason}",
    ),
    "performance.review.released": (
        "Your {cycle_name} review is ready",
        "Your {cycle_name} review has been released. Read it and acknowledge it.",
    ),
    "performance.review.objection": (
        "An objection was recorded on a review",
        "{employee_name} acknowledged their {cycle_name} review with a written objection.",
    ),
    "performance.review.reminder": (
        "{count} review step(s) waiting for you",
        "You have {count} review step(s) waiting at {stage_label} in {cycle_name}. "
        "Due: {due_date}.",
    ),
    "performance.review.escalated": (
        "A review has been escalated to you",
        "{employee_name}'s {stage_label} in {cycle_name} is overdue by {days} day(s) and "
        "has been escalated to you.",
    ),
    "performance.goal.assigned": (
        "A goal was set for you",
        "{actor_name} set a goal for you: {title}.",
    ),
    "performance.pip.opened": (
        "An improvement plan has been opened",
        "An improvement plan running {start_date} to {end_date} has been opened. You can "
        "add your own account to it at any time.",
    ),
    "performance.probation.due": (
        "Probation review due for {employee_name}",
        "{employee_name}'s probation ends on {probation_end_date}. The review is due "
        "{review_due_date}.",
    ),
}


async def ensure_templates(
    session: AsyncSession, tenant_id: uuid.UUID, event_keys: Iterable[str]
) -> None:
    """Install starter wording for these events where the tenant has none."""
    for event_key in set(event_keys):
        subject, body = EVENTS[event_key]
        for channel in (Channel.IN_APP, Channel.EMAIL):
            existing = await session.scalar(
                select(NotificationTemplate.id).where(
                    tenant_filter(NotificationTemplate, tenant_id),
                    NotificationTemplate.event_key == event_key,
                    NotificationTemplate.channel == channel,
                )
            )
            if existing is None:
                session.add(
                    NotificationTemplate(
                        tenant_id=tenant_id,
                        event_key=event_key,
                        channel=channel,
                        subject=subject,
                        body=body,
                    )
                )
    await session.flush()


async def send(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    event_key: str,
    recipient_user_ids: Iterable[uuid.UUID | None],
    payload: dict[str, Any],
) -> dispatch.DispatchOutcome | None:
    """Raise one event for these recipients, inside the caller's transaction."""
    recipients = list(dict.fromkeys(uid for uid in recipient_user_ids if uid is not None))
    if not recipients:
        return None
    await ensure_templates(session, tenant_id, [event_key])
    return await dispatch.notify(
        session,
        tenant_id=tenant_id,
        event_key=event_key,
        recipient_user_ids=recipients,
        payload={key: ("" if value is None else value) for key, value in payload.items()},
        commit=False,
    )


async def user_ids_for_employees(
    session: AsyncSession, user: User, employee_ids: Iterable[uuid.UUID | None]
) -> dict[uuid.UUID, uuid.UUID]:
    """The active login for each employee that has one."""
    wanted = {eid for eid in employee_ids if eid is not None}
    if not wanted:
        return {}
    rows = (
        await session.execute(
            tenant_scoped(
                select(User.employee_id, User.id).where(
                    User.employee_id.in_(wanted), User.is_active.is_(True)
                ),
                User,
                user,
            )
        )
    ).all()
    return {employee_id: user_id for employee_id, user_id in rows}


async def hr_user_ids(session: AsyncSession, user: User) -> list[uuid.UUID]:
    """Everybody in HR, for events that route to HR rather than to a person."""
    return list(
        (
            await session.scalars(
                tenant_scoped(
                    select(User.id).where(
                        User.role.in_(permissions.HR_ROLES), User.is_active.is_(True)
                    ),
                    User,
                    user,
                )
            )
        ).all()
    )
