"""Statutory notices: published by the platform, seen per workspace, drafted, alerted once."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.org import LegalEntity, Location
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services import statutory_watch
from app.services.statutory_watch import NoticeIn
from tests.integration.conftest import requires_db

pytestmark = requires_db


async def _platform(session: AsyncSession, tenant: Tenant) -> User:
    user = User(
        tenant_id=tenant.id, email="platform@askmira-demo.com", password_hash="x",
        full_name="Platform", role=UserRole.SUPER_ADMIN, is_active=True,
    )
    session.add(user)
    await session.commit()
    return user


async def test_a_notice_reaches_the_affected_workspace_and_is_drafted(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    location: Location,
) -> None:
    platform = await _platform(session, tenant)
    soon = date.today() + timedelta(days=20)
    with pytest.raises(HTTPException):
        await statutory_watch.publish(
            session, hr_user,
            NoticeIn(rule_type="professional_tax", jurisdiction="KA", effective_from=soon,
                     title="Karnataka PT slab change", summary="New slabs from the notification.",
                     source_reference="Notification 1/2026"),
            request=None,
        )
    reviewed = await statutory_watch.publish(
        session, platform,
        NoticeIn(rule_type="professional_tax", jurisdiction="KA", effective_from=soon,
                 title="Karnataka PT slab change", summary="New slabs from the notification.",
                 source_reference="Notification 1/2026",
                 suggested_rules={"slabs": [{"min": "0", "max": "24999", "amount": "0"}]}),
        request=None,
    )
    await statutory_watch.publish(
        session, platform,
        NoticeIn(rule_type="professional_tax", jurisdiction="MH", effective_from=soon,
                 title="Maharashtra PT change", summary="Not this workspace's state.",
                 source_reference="Notification 2/2026"),
        request=None,
    )

    seen = await statutory_watch.notices_for(session, hr_user)
    assert [n["jurisdiction"] for n in seen] == ["KA"]
    assert seen[0]["status"] == "action_needed"

    alerted = await statutory_watch.send_alerts(session, hr_user, today=date.today())
    again = await statutory_watch.send_alerts(session, hr_user, today=date.today())
    assert (alerted["alerted"], again["alerted"]) == (1, 0)

    drafted = await statutory_watch.draft_from_notice(
        session, hr_user, uuid.UUID(reviewed["id"]), request=None
    )
    assert drafted["is_approved"] is False
    assert (await statutory_watch.notices_for(session, hr_user))[0]["status"] == "drafted"

    coverage = await statutory_watch.coverage(session, hr_user)
    states = {row["jurisdiction"] for row in coverage}
    assert states == {"IN", "KA"}
    assert all(not rule["in_force"] for row in coverage for rule in row["rules"])
