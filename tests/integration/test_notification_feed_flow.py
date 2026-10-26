"""What the live notification stream reads: my in-app notifications, newer than
the last one sent, and nobody else's — not a colleague's, not another tenant's."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models import UserRole
from app.models.notifications import Channel, Notification, NotificationStatus
from app.services.notifications import feed
from tests.integration.conftest import requires_db
from tests.integration.platform_support import make_person

pytestmark = requires_db


def _note(tenant, user, body, *, channel=Channel.IN_APP, at=None):
    row = Notification(
        tenant_id=tenant.id,
        recipient_user_id=user.id,
        event_key="system.announcement",
        channel=channel,
        rendered_body=body,
        status=NotificationStatus.DELIVERED,
    )
    if at is not None:
        row.created_at = at
    return row


async def test_the_feed_is_mine_new_and_in_app_only(session, tenant, other_tenant):
    me = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Meera Nair")
    colleague = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Sana Iqbal")
    stranger = await make_person(session, other_tenant, role=UserRole.EMPLOYEE, name="Ravi Rao")
    since = datetime.now(UTC) - timedelta(minutes=5)

    session.add_all(
        [
            _note(tenant, me, "old", at=since - timedelta(minutes=1)),
            _note(tenant, me, "first", at=since + timedelta(seconds=1)),
            _note(tenant, me, "second", at=since + timedelta(seconds=2)),
            _note(tenant, me, "by email", channel=Channel.EMAIL, at=since + timedelta(seconds=3)),
            _note(tenant, colleague, "colleague's", at=since + timedelta(seconds=4)),
            _note(other_tenant, stranger, "other tenant's", at=since + timedelta(seconds=5)),
        ]
    )
    await session.commit()

    rows = await feed.new_in_app_since(session, me, since)

    assert [r["body"] for r in rows] == ["first", "second"]
    assert all(
        set(r) == {"id", "event_key", "subject", "body", "is_urgent", "created_at"} for r in rows
    )

    # Only what came after the last one already sent.
    assert await feed.new_in_app_since(session, me, rows[-1]["created_at"]) == []
