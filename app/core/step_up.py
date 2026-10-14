"""Fresh proof of identity before an action, asked for at the right moment.

A route passes `confirmation(session, user, action)` to the service as its
`confirm` callback. The service calls it after its own checks — so somebody who
may not approve this run at all is told so, rather than asked for their
password first — and before it changes anything. Until the caller has satisfied
a step-up challenge for the action within the window, it raises 428 with an
`X-Step-Up-Action` header, which the web app answers by asking and retrying.

It is passed from the route, not built into the service, because it is about
who is at the keyboard; a scheduled job or a test calling the service has none.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User

__all__ = ["Confirm", "confirmation"]

Confirm = Callable[[], Awaitable[None]]


def confirmation(session: AsyncSession, user: User, action_key: str) -> Confirm:
    async def confirm() -> None:
        from app.services.opsec import service as opsec

        await opsec.require_fresh_step_up(session, user, action_key)

    return confirm
