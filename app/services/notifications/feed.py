"""The in-app notification feed, as the live stream polls it."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.notifications import Channel, Notification
from app.models.user import User


async def new_in_app_since(
    session: AsyncSession, user: User, since: datetime
) -> list[dict[str, Any]]:
    """The person's in-app notifications created after `since`, oldest first.

    Returned as plain dicts so the caller can give the session back before it
    does anything slow with them.
    """
    rows = (
        await session.scalars(
            tenant_scoped(
                select(Notification)
                .where(
                    Notification.recipient_user_id == user.id,
                    Notification.channel == Channel.IN_APP,
                    Notification.created_at > since,
                )
                .order_by(Notification.created_at),
                Notification,
                user,
            )
        )
    ).all()
    return [
        {
            "id": str(row.id),
            "event_key": row.event_key,
            "subject": row.rendered_subject,
            "body": row.rendered_body,
            "is_urgent": row.is_urgent,
            "created_at": row.created_at,
        }
        for row in rows
    ]
