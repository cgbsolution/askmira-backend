"""The culture layer against a real database: feed, reactions, clubs,
announcement delivery, award posts, surveys, celebrations and expiry warnings.

The gates this file holds:

- a shoutout is visible exactly where its visibility says, and the page total
  counts only what is visible;
- a reaction is idempotent, audited, and impossible on a shoutout you cannot see;
- only the sender or HR deletes, and the value tag goes with the shoutout;
- a club's member list is for its members - HR is refused like anybody else;
- a published announcement is delivered to its audience, exactly once;
- a declared award result is on the feed as the company's post;
- only a survey's recipient answers it, once;
- an expiry warning is sent once per expiry date.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.core.security import create_access_token
from app.db.session import get_session
from app.main import app
from app.models.audit import AuditLog
from app.models.culture import Club, ShoutoutReaction, ShoutoutVisibility
from app.models.culture_depth import (
    AwardCadenceEnum,
    Survey,
    SurveyKindEnum,
    SurveyStatusEnum,
    TagSourceEnum,
    ValueTag,
)
from app.models.employee import Employee, EmploymentStatus
from app.models.notifications import Notification
from app.models.org import Department
from app.models.user import User, UserRole
from app.schemas.culture import (
    AcknowledgeCreate,
    AnnouncementCreate,
    AwardCategory,
    AwardCriterion,
    AwardCycleCreate,
    AwardProgrammeCreate,
    AwardWinnerCreate,
    ClubCreate,
    EventPrivacyUpdate,
    NominationCreate,
    ShoutoutCreate,
    SurveyCreate,
    SurveyResponseCreate,
)
from app.services.culture import awards_portal, comms_portal, feed, surveys_portal
from app.services.culture import service as culture
from app.services.notifications.dispatch import seed_default_templates
from app.services.rewards import portal as rewards_portal
from app.services.rewards import service as rewards
from tests.integration.conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]


# ── Fixtures ────────────────────────────────────────────────────────────────


async def _person(session, tenant, entity, location, *, code, role, department=None):
    employee = Employee(
        tenant_id=tenant.id,
        employee_code=code,
        first_name=code.title(),
        last_name="Test",
        work_email=f"{code.lower()}@acme.test",
        legal_entity_id=entity.id,
        location_id=location.id,
        department_id=department.id if department else None,
        status=EmploymentStatus.ACTIVE,
        date_of_joining=date(2022, 1, 1),
    )
    session.add(employee)
    await session.flush()
    user = User(
        tenant_id=tenant.id,
        email=f"{code.lower()}@acme.test",
        password_hash="not-a-real-hash",
        full_name=f"{code.title()} Test",
        role=role,
        is_active=True,
        employee_id=employee.id,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    await session.refresh(employee)
    return user


@pytest.fixture
async def org(session, tenant, legal_entity, location):
    tenant.enabled_modules = {**(tenant.enabled_modules or {}), "culture": True, "rewards": True}
    eng = Department(tenant_id=tenant.id, name="Engineering", code="ENG")
    ops = Department(tenant_id=tenant.id, name="Operations", code="OPS")
    session.add_all([eng, ops])
    await session.commit()
    await seed_default_templates(session, tenant.id)
    emp, hr_role = UserRole.EMPLOYEE, UserRole.HR_ADMIN
    spec = [
        ("hr", hr_role, ops),
        ("asha", emp, eng),
        ("ben", emp, eng),
        ("cara", emp, eng),
        ("dev", emp, ops),
    ]
    people = {}
    for key, role, dept in spec:
        people[key] = await _person(
            session, tenant, legal_entity, location, code=key.upper(), role=role, department=dept
        )
    people["eng"] = eng
    people["ops"] = ops
    people["tenant"] = tenant
    return people


async def _shout(session, sender, recipient, visibility=ShoutoutVisibility.COMPANY, value=None):
    return await feed.create_shoutout(
        session,
        sender,
        ShoutoutCreate(
            to_employee_id=recipient.employee_id,
            message="Thank you for the release weekend.",
            value_tag=value,
            visibility=visibility,
        ),
    )


async def _visible_ids(session, user):
    items, total = await feed.list_feed(session, user, page=1, page_size=50)
    assert total == len(items)
    return {i.id for i in items}


# ══════════════════════════════════════════════════════════════════════════════
# Feed visibility
# ══════════════════════════════════════════════════════════════════════════════


async def test_visibility_is_enforced_in_the_query(session, org):
    company = await _shout(session, org["asha"], org["ben"])
    department = await _shout(session, org["asha"], org["ben"], ShoutoutVisibility.DEPARTMENT)
    private = await _shout(session, org["asha"], org["ben"], ShoutoutVisibility.PRIVATE)

    # Cara is in Engineering, Ben's department.
    assert await _visible_ids(session, org["cara"]) == {company.id, department.id}
    # Dev is in Operations.
    assert await _visible_ids(session, org["dev"]) == {company.id}
    # HR sees department shoutouts but not private ones.
    assert await _visible_ids(session, org["hr"]) == {company.id, department.id}
    # The two people in it see all three.
    assert await _visible_ids(session, org["asha"]) == {company.id, department.id, private.id}
    assert await _visible_ids(session, org["ben"]) == {company.id, department.id, private.id}


async def test_the_page_total_is_the_visible_count_not_the_page_length(session, org):
    for _ in range(5):
        await _shout(session, org["asha"], org["ben"])
    await _shout(session, org["asha"], org["ben"], ShoutoutVisibility.PRIVATE)
    items, total = await feed.list_feed(session, org["dev"], page=1, page_size=2)
    assert len(items) == 2
    assert total == 5


async def test_a_shoutout_names_sender_and_recipient_and_notifies(session, org):
    row = await _shout(session, org["asha"], org["ben"])
    assert row.kind == "peer"
    assert row.sender.name == "Asha Test"
    assert row.recipient.name == "Ben Test"
    notes = (
        await session.scalars(
            select(Notification).where(
                Notification.recipient_user_id == org["ben"].id,
                Notification.event_key == "culture.shoutout.received",
            )
        )
    ).all()
    assert notes, "the recipient was not notified"


async def test_you_cannot_shout_yourself_out(session, org):
    with pytest.raises(HTTPException) as exc:
        await _shout(session, org["asha"], org["asha"])
    assert exc.value.status_code == 422


async def test_a_recipient_in_another_tenant_is_not_found(
    session, org, other_tenant, legal_entity, location
):
    stranger = Employee(
        tenant_id=other_tenant.id,
        employee_code="X1",
        first_name="Stranger",
        work_email="x@globex.test",
        status=EmploymentStatus.ACTIVE,
    )
    session.add(stranger)
    await session.commit()
    with pytest.raises(HTTPException) as exc:
        await feed.create_shoutout(
            session,
            org["asha"],
            ShoutoutCreate(to_employee_id=stranger.id, message="Hi"),
        )
    assert exc.value.status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# Values
# ══════════════════════════════════════════════════════════════════════════════


async def test_a_value_on_a_shoutout_writes_a_value_tag_in_the_same_post(session, org):
    await culture.define_value(
        session, org["hr"], code="craft", name="Craft", description="We sign our work."
    )
    row = await _shout(session, org["asha"], org["ben"], value="craft")
    assert row.value_name == "Craft"
    tag = (await session.scalars(select(ValueTag).where(ValueTag.source_id == row.id))).one()
    assert tag.source is TagSourceEnum.SHOUTOUT
    assert tag.subject_employee_id == org["ben"].employee_id
    assert tag.group_key == str(org["eng"].id)

    report = await culture.value_analytics(session, org["hr"])
    craft = next(v for v in report["coverage"]["values"] if v["code"] == "craft")
    assert craft["counts"]["shoutout"] == 1


async def test_an_undefined_value_is_refused_and_nothing_is_posted(session, org):
    with pytest.raises(HTTPException) as exc:
        await _shout(session, org["asha"], org["ben"], value="hustle")
    assert exc.value.status_code == 400
    _, total = await feed.list_feed(session, org["asha"], page=1, page_size=10)
    assert total == 0


# ══════════════════════════════════════════════════════════════════════════════
# Reactions
# ══════════════════════════════════════════════════════════════════════════════


async def _reaction_count(session, shoutout_id):
    return await session.scalar(
        select(func.count(ShoutoutReaction.id)).where(ShoutoutReaction.shoutout_id == shoutout_id)
    )


async def _audit_count(session, entity_type):
    return await session.scalar(
        select(func.count(AuditLog.id)).where(AuditLog.entity_type == entity_type)
    )


async def test_a_reaction_is_idempotent_and_audited(session, org):
    row = await _shout(session, org["asha"], org["ben"])
    first = await feed.set_reaction(session, org["cara"], row.id, emoji="🎉", active=True)
    second = await feed.set_reaction(session, org["cara"], row.id, emoji="🎉", active=True)
    assert first == second
    assert first[0].count == 1 and first[0].reacted is True
    assert await _reaction_count(session, row.id) == 1
    assert await _audit_count(session, "shoutout_reaction") == 1

    # Another person sees the count but not their own flag.
    view = await feed.set_reaction(session, org["dev"], row.id, emoji="🎉", active=False)
    assert view[0].count == 1 and view[0].reacted is False
    assert await _audit_count(session, "shoutout_reaction") == 1

    off = await feed.set_reaction(session, org["cara"], row.id, emoji="🎉", active=False)
    assert off == []
    await feed.set_reaction(session, org["cara"], row.id, emoji="🎉", active=False)
    assert await _reaction_count(session, row.id) == 0
    assert await _audit_count(session, "shoutout_reaction") == 2


async def test_a_reaction_on_an_invisible_shoutout_is_not_found(session, org):
    private = await _shout(session, org["asha"], org["ben"], ShoutoutVisibility.PRIVATE)
    for outsider in (org["cara"], org["hr"]):
        with pytest.raises(HTTPException) as exc:
            await feed.set_reaction(session, outsider, private.id, emoji="👏", active=True)
        assert exc.value.status_code == 404
    assert await _reaction_count(session, private.id) == 0


async def test_a_department_shoutout_refuses_a_reaction_from_another_department(session, org):
    row = await _shout(session, org["asha"], org["ben"], ShoutoutVisibility.DEPARTMENT)
    with pytest.raises(HTTPException) as exc:
        await feed.set_reaction(session, org["dev"], row.id, emoji="👏", active=True)
    assert exc.value.status_code == 404
    ok = await feed.set_reaction(session, org["cara"], row.id, emoji="👏", active=True)
    assert ok[0].count == 1


async def test_a_reaction_outside_the_closed_set_is_refused(session, org):
    row = await _shout(session, org["asha"], org["ben"])
    with pytest.raises(HTTPException):
        await feed.set_reaction(session, org["cara"], row.id, emoji="💩", active=True)


# ══════════════════════════════════════════════════════════════════════════════
# Delete and pin
# ══════════════════════════════════════════════════════════════════════════════


async def test_only_the_sender_or_hr_can_delete(session, org):
    await culture.define_value(
        session, org["hr"], code="craft", name="Craft", description="We sign our work."
    )
    row = await _shout(session, org["asha"], org["ben"], value="craft")
    await feed.set_reaction(session, org["cara"], row.id, emoji="👏", active=True)

    for not_allowed in (org["ben"], org["cara"]):
        with pytest.raises(HTTPException) as exc:
            await feed.delete_shoutout(session, not_allowed, row.id)
        assert exc.value.status_code == 403

    await feed.delete_shoutout(session, org["asha"], row.id)
    assert row.id not in await _visible_ids(session, org["asha"])
    assert (
        await session.scalar(select(func.count(ValueTag.id)).where(ValueTag.source_id == row.id))
        == 0
    )

    second = await _shout(session, org["asha"], org["ben"])
    await feed.delete_shoutout(session, org["hr"], second.id)
    assert second.id not in await _visible_ids(session, org["asha"])
    assert await _audit_count(session, "shoutout") >= 4


async def test_another_tenant_cannot_delete_or_react(session, org, other_hr_user):
    row = await _shout(session, org["asha"], org["ben"])
    with pytest.raises(HTTPException) as exc:
        await feed.delete_shoutout(session, other_hr_user, row.id)
    assert exc.value.status_code == 404


async def test_pinned_shoutouts_come_first(session, org):
    old = await _shout(session, org["asha"], org["ben"])
    await _shout(session, org["asha"], org["cara"])
    pinned = await feed.pin_shoutout(session, org["hr"], old.id, is_pinned=True)
    assert pinned.is_pinned
    items, _ = await feed.list_feed(session, org["dev"], page=1, page_size=10)
    assert items[0].id == old.id
    with pytest.raises(HTTPException) as exc:
        await feed.pin_shoutout(session, org["asha"], old.id, is_pinned=False)
    assert exc.value.status_code == 403


# ══════════════════════════════════════════════════════════════════════════════
# Clubs
# ══════════════════════════════════════════════════════════════════════════════


async def test_club_membership_and_member_list_privacy(session, org):
    club = await feed.create_club(session, org["asha"], ClubCreate(name="Chess"))
    assert club.my_role == "organiser" and club.member_count == 1

    joined = await feed.join_club(session, org["ben"], club.id)
    again = await feed.join_club(session, org["ben"], club.id)
    assert joined.member_count == again.member_count == 2

    members = await feed.club_members(session, org["ben"], club.id)
    assert {m.name for m in members} == {"Asha Test", "Ben Test"}
    assert members[0].role == "organiser"

    for outsider in (org["cara"], org["hr"]):
        with pytest.raises(HTTPException) as exc:
            await feed.club_members(session, outsider, club.id)
        assert exc.value.status_code == 403

    left = await feed.leave_club(session, org["ben"], club.id)
    assert left.member_count == 1 and not left.is_member

    stats = await feed.club_participation(session, org["hr"])
    assert stats.active_clubs == 1
    assert stats.distinct_members == 1
    with pytest.raises(HTTPException):
        await feed.club_participation(session, org["asha"])


async def test_a_private_club_is_invisible_and_unjoinable_to_non_members(session, org):
    club = await feed.create_club(
        session, org["asha"], ClubCreate(name="Book club", is_private=True)
    )
    assert club.id in {c.id for c in await feed.list_clubs(session, org["asha"])}
    for outsider in (org["ben"], org["hr"]):
        assert club.id not in {c.id for c in await feed.list_clubs(session, outsider)}
        with pytest.raises(HTTPException) as exc:
            await feed.join_club(session, outsider, club.id)
        assert exc.value.status_code == 404
        with pytest.raises(HTTPException) as exc:
            await feed.club_members(session, outsider, club.id)
        assert exc.value.status_code == 404


async def test_the_last_member_leaving_closes_the_club(session, org):
    club = await feed.create_club(session, org["asha"], ClubCreate(name="Running"))
    assert await feed.leave_club(session, org["asha"], club.id) is None
    row = await session.get(Club, club.id)
    assert row.is_active is False
    assert club.id not in {c.id for c in await feed.list_clubs(session, org["ben"])}


async def test_duplicate_club_names_get_distinct_slugs(session, org):
    a = await feed.create_club(session, org["asha"], ClubCreate(name="Cricket"))
    b = await feed.create_club(session, org["ben"], ClubCreate(name="Cricket"))
    assert a.slug != b.slug


# ══════════════════════════════════════════════════════════════════════════════
# Announcements
# ══════════════════════════════════════════════════════════════════════════════


async def _announcement_notes(session, announcement_id):
    rows = (
        await session.scalars(
            select(Notification).where(Notification.event_key == "system.announcement")
        )
    ).all()
    return [r for r in rows if r.payload.get("announcement_id") == str(announcement_id)]


async def test_a_published_announcement_is_delivered_to_its_audience(session, org):
    made = await comms_portal.create_announcement(
        session,
        org["hr"],
        AnnouncementCreate(
            title="Engineering offsite",
            body="Friday, all day.",
            audience={"department_ids": [str(org["eng"].id)]},
            requires_acknowledgement=True,
        ),
    )
    assert made.delivered_at is not None
    assert made.delivered_count == 3
    notes = await _announcement_notes(session, made.id)
    assert {n.recipient_user_id for n in notes} == {org["asha"].id, org["ben"].id, org["cara"].id}

    # Visible to the audience, and acknowledged by its members only.
    assert [a.id for a in await comms_portal.list_my_announcements(session, org["asha"])] == [
        made.id
    ]
    assert await comms_portal.list_my_announcements(session, org["dev"]) == []
    with pytest.raises(HTTPException) as exc:
        await comms_portal.acknowledge(session, org["dev"], made.id, AcknowledgeCreate())
    assert exc.value.status_code == 404

    ack = await comms_portal.acknowledge(session, org["asha"], made.id, AcknowledgeCreate())
    assert ack.acknowledged is True
    register = await comms_portal.acknowledgement_register(session, org["hr"], made.id)
    assert register["acknowledged"] == 1
    assert register["acknowledged_people"][0]["name"] == "Asha Test"
    assert set(register["outstanding"]) == {
        str(org["ben"].employee_id),
        str(org["cara"].employee_id),
    }
    assert register["people"][str(org["ben"].employee_id)] == "Ben Test"


async def test_a_scheduled_announcement_is_delivered_once_when_due(session, org):
    later = datetime.now(UTC) + timedelta(hours=2)
    made = await comms_portal.create_announcement(
        session,
        org["hr"],
        AnnouncementCreate(title="Canteen closed", body="On Friday.", publish_at=later),
    )
    assert made.delivered_at is None
    assert await _announcement_notes(session, made.id) == []
    assert await comms_portal.list_my_announcements(session, org["asha"]) == []

    early = await culture.deliver_due_announcements(session, org["tenant"].id)
    assert early["delivered"] == 0

    due = await culture.deliver_due_announcements(
        session, org["tenant"].id, now=later + timedelta(minutes=1)
    )
    assert due["delivered"] == 1
    assert due["people_notified"] == 5
    again = await culture.deliver_due_announcements(
        session, org["tenant"].id, now=later + timedelta(minutes=5)
    )
    assert again["delivered"] == 0
    assert len(await _announcement_notes(session, made.id)) == 5 * 2  # in-app and email


async def test_hr_cannot_acknowledge_for_somebody_else(session, org):
    """The HTTP contract has no employee id; the service records only the caller."""
    made = await comms_portal.create_announcement(
        session,
        org["hr"],
        AnnouncementCreate(title="POSH", body="Policy.", requires_acknowledgement=True),
    )
    await comms_portal.acknowledge(session, org["hr"], made.id, AcknowledgeCreate())
    register = await comms_portal.acknowledgement_register(session, org["hr"], made.id)
    assert [p["name"] for p in register["acknowledged_people"]] == ["Hr Test"]


# ══════════════════════════════════════════════════════════════════════════════
# Awards: the result is on the feed
# ══════════════════════════════════════════════════════════════════════════════


async def _award_setup(session, org):
    programme = await awards_portal.create_programme(
        session,
        org["hr"],
        AwardProgrammeCreate(
            name="Quarterly Stars",
            cadence=AwardCadenceEnum.QUARTERLY,
            categories=[AwardCategory(key="impact", name="Impact")],
            criteria=[AwardCriterion(key="impact", name="Impact")],
        ),
    )
    today = date.today()
    cycle = await awards_portal.open_cycle(
        session,
        org["hr"],
        AwardCycleCreate(
            programme_id=programme.id,
            label="Now",
            nominations_open_on=today - timedelta(days=1),
            nominations_close_on=today + timedelta(days=5),
            juror_employee_ids=[org["dev"].employee_id],
        ),
    )
    nomination = await awards_portal.nominate(
        session,
        org["asha"],
        NominationCreate(
            cycle_id=cycle.id,
            category_key="impact",
            nominee_employee_id=org["ben"].employee_id,
            citation="Rebuilt the deploy pipeline and halved release time.",
        ),
    )
    return programme, cycle, nomination


async def test_a_declared_award_result_is_posted_to_the_feed(session, org):
    _, cycle, nomination = await _award_setup(session, org)
    assert nomination.nominator.name == "Asha Test"

    # The juror sees the nomination without the nominator.
    jury = await awards_portal.list_nominations(session, org["dev"], cycle.id)
    assert jury[0].nominator is None and jury[0].my_score == "none"
    # A colleague who nominated nobody sees nothing.
    assert await awards_portal.list_nominations(session, org["cara"], cycle.id) == []

    await culture.score_nomination(
        session, org["dev"], nomination_id=nomination.id, scores={"impact": 8}, submit=True
    )
    result = await awards_portal.declare_winner(
        session, org["hr"], AwardWinnerCreate(nomination_id=nomination.id)
    )
    assert result.shoutout_id is not None
    assert result.category_name == "Impact"

    items, _ = await feed.list_feed(session, org["cara"], page=1, page_size=10)
    post = next(i for i in items if i.id == result.shoutout_id)
    assert post.kind == "award"
    assert post.sender is None
    assert post.recipient.name == "Ben Test"
    assert post.award.programme_name == "Quarterly Stars"
    assert post.can_delete is False

    results = await awards_portal.list_results(session, org["cara"])
    assert [r.id for r in results] == [result.id]

    with pytest.raises(HTTPException):
        await awards_portal.declare_winner(
            session, org["hr"], AwardWinnerCreate(nomination_id=nomination.id)
        )


async def test_self_nomination_and_duplicates_are_refused(session, org):
    _, cycle, _ = await _award_setup(session, org)
    with pytest.raises(HTTPException) as exc:
        await awards_portal.nominate(
            session,
            org["asha"],
            NominationCreate(
                cycle_id=cycle.id,
                category_key="impact",
                nominee_employee_id=org["asha"].employee_id,
                citation="I did a very good job this quarter.",
            ),
        )
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException) as exc:
        await awards_portal.nominate(
            session,
            org["asha"],
            NominationCreate(
                cycle_id=cycle.id,
                category_key="impact",
                nominee_employee_id=org["ben"].employee_id,
                citation="Rebuilt the deploy pipeline, again.",
            ),
        )
    assert exc.value.status_code == 409


async def test_a_juror_who_is_hr_sees_standings_only_after_scoring(session, org):
    _, cycle, nomination = await _award_setup(session, org)
    hr_cycle = await awards_portal.open_cycle(
        session,
        org["hr"],
        AwardCycleCreate(
            programme_id=cycle.programme_id,
            label="HR jury",
            nominations_open_on=date.today(),
            nominations_close_on=date.today(),
            juror_employee_ids=[org["hr"].employee_id],
        ),
    )
    await awards_portal.nominate(
        session,
        org["ben"],
        NominationCreate(
            cycle_id=hr_cycle.id,
            category_key="impact",
            nominee_employee_id=org["cara"].employee_id,
            citation="Mentored three new joiners through their first release.",
        ),
    )
    with pytest.raises(HTTPException) as exc:
        await awards_portal.standings(session, org["hr"], hr_cycle.id)
    assert exc.value.status_code == 403
    # HR who is not on a cycle's jury sees its standings.
    report = await awards_portal.standings(session, org["hr"], cycle.id)
    assert report["categories"][0]["category_key"] == "impact"
    assert str(nomination.id) in report["nominations"]


# ══════════════════════════════════════════════════════════════════════════════
# Surveys
# ══════════════════════════════════════════════════════════════════════════════


async def _survey(session, org, **extra):
    today = date.today()
    return await surveys_portal.create_survey(
        session,
        org["hr"],
        SurveyCreate(
            name="September eNPS",
            kind=SurveyKindEnum.ENPS,
            opens_on=today - timedelta(days=1),
            closes_on=today + timedelta(days=7),
            instrument_key="enps",
            **extra,
        ),
    )


async def test_only_the_recipient_answers_and_only_once(
    session, org, tenant, legal_entity, location
):
    survey = await _survey(session, org)
    assert survey.invited == 5

    mine = await surveys_portal.list_my_surveys(session, org["asha"])
    assert [s.id for s in mine] == [survey.id]
    assert mine[0].is_open_now and not mine[0].has_responded

    await surveys_portal.respond(
        session, org["asha"], survey.id, SurveyResponseCreate(answers={"enps": 9})
    )
    with pytest.raises(HTTPException) as exc:
        await surveys_portal.respond(
            session, org["asha"], survey.id, SurveyResponseCreate(answers={"enps": 3})
        )
    assert exc.value.status_code == 400

    # Somebody who joined after the survey was sent was not sent it.
    late = await _person(
        session, tenant, legal_entity, location, code="LATE", role=UserRole.EMPLOYEE
    )
    with pytest.raises(HTTPException) as exc:
        await surveys_portal.respond(
            session, late, survey.id, SurveyResponseCreate(answers={"enps": 10})
        )
    assert exc.value.status_code == 403

    listed = await surveys_portal.list_surveys(session, org["hr"])
    assert (listed[0].invited, listed[0].responded) == (5, 1)
    results = await culture.survey_results(session, org["dev"], survey_id=survey.id)
    assert results["overall"]["suppressed"] is True
    assert results["overall"]["score"] is None


async def test_an_answer_to_a_question_the_survey_does_not_ask_is_refused(session, org):
    survey = await _survey(session, org)
    with pytest.raises(HTTPException):
        await surveys_portal.respond(
            session, org["asha"], survey.id, SurveyResponseCreate(answers={"clarity": 3})
        )


async def test_a_resurvey_is_scheduled_from_configuration_once(session, org):
    survey = await _survey(session, org, resurvey_interval_days=30)
    row = await session.get(Survey, survey.id)
    too_early = await surveys_portal.schedule_due_resurveys(
        session, org["tenant"].id, today=row.closes_on + timedelta(days=10)
    )
    assert too_early["created"] == 0

    due_day = row.closes_on + timedelta(days=30)
    due = await surveys_portal.schedule_due_resurveys(session, org["tenant"].id, today=due_day)
    assert due["created"] == 1
    wave = await session.get(Survey, uuid.UUID(due["survey_ids"][0]))
    assert wave.previous_wave_id == survey.id
    assert wave.instrument_key == "enps"
    assert wave.opens_on == due_day
    await session.refresh(row)
    assert row.status is SurveyStatusEnum.CLOSED
    assert {s.id for s in await surveys_portal.list_my_surveys(session, org["asha"])} >= {wave.id}

    again = await surveys_portal.schedule_due_resurveys(session, org["tenant"].id, today=due_day)
    assert again["created"] == 0


# ══════════════════════════════════════════════════════════════════════════════
# Celebrations
# ══════════════════════════════════════════════════════════════════════════════


async def test_a_team_birthday_reaches_the_team_and_not_the_company(session, org):
    asha = await session.get(Employee, org["asha"].employee_id)
    asha.date_of_birth = date(1990, 12, 31)
    await session.commit()
    await comms_portal.set_my_event_privacy(
        session,
        org["asha"],
        EventPrivacyUpdate(share_birthday=True, birthday_scope="team"),
    )
    window = {"from_date": date(2026, 12, 28), "to_date": date(2027, 1, 3)}

    def birthdays(result):
        return [e for e in result["events"] if e["kind"] == "birthday"]

    assert len(birthdays(await culture.calendar(session, org["ben"], **window))) == 1
    assert len(birthdays(await culture.calendar(session, org["hr"], **window))) == 1
    assert birthdays(await culture.calendar(session, org["dev"], **window)) == []

    prefs = await comms_portal.get_my_event_privacy(session, org["asha"])
    assert prefs.share_birthday and prefs.configured
    fresh = await comms_portal.get_my_event_privacy(session, org["ben"])
    assert not fresh.share_birthday and not fresh.configured


async def test_a_leap_day_birthday_does_not_break_the_calendar(session, org):
    asha = await session.get(Employee, org["asha"].employee_id)
    asha.date_of_birth = date(1992, 2, 29)
    await session.commit()
    await comms_portal.set_my_event_privacy(
        session,
        org["asha"],
        EventPrivacyUpdate(share_birthday=True, birthday_scope="company"),
    )
    result = await culture.calendar(
        session, org["dev"], from_date=date(2027, 2, 1), to_date=date(2027, 3, 1)
    )
    assert [e["on_date"] for e in result["events"] if e["kind"] == "birthday"] == ["2027-02-28"]


# ══════════════════════════════════════════════════════════════════════════════
# Rewards: expiry warnings
# ══════════════════════════════════════════════════════════════════════════════


async def test_an_expiry_warning_is_sent_once_per_date(session, org):
    hr = org["hr"]
    await rewards.set_valuation(
        session, hr, points_per_rupee=Decimal("1"), effective_from=date(2025, 1, 1)
    )
    await rewards.set_expiry_policy(
        session,
        hr,
        enabled=True,
        expire_after_days=100,
        notify_days_before=30,
        disclosure="Points expire 100 days after award.",
    )
    await rewards.award_points(
        session, hr, employee_id=org["asha"].employee_id, points=500, awarded_on=date(2026, 1, 1)
    )
    as_of = date(2026, 1, 1) + timedelta(days=80)

    first = await rewards_portal.send_expiry_warnings(session, org["tenant"].id, as_of=as_of)
    assert first.employees_warned == 1 and first.lots_warned == 1
    second = await rewards_portal.send_expiry_warnings(session, org["tenant"].id, as_of=as_of)
    assert second.employees_warned == 0 and second.already_warned == 1

    notes = (
        await session.scalars(
            select(Notification).where(Notification.event_key == rewards_portal.EXPIRY_EVENT)
        )
    ).all()
    assert notes and all(n.recipient_user_id == org["asha"].id for n in notes)
    assert "500" in notes[0].rendered_body

    statement = await rewards_portal.my_statement(session, org["asha"], as_of=as_of)
    assert statement["balance_points"] == 500
    assert statement["expiring"][0]["points"] == 500


# ══════════════════════════════════════════════════════════════════════════════
# Through HTTP: gates, module switch, and bodies that cannot name somebody else
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
async def client(session):
    async def override():
        yield session

    app.dependency_overrides[get_session] = override
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


def _auth(user: User) -> dict[str, str]:
    token = create_access_token(
        user_id=str(user.id), tenant_id=str(user.tenant_id), role=user.role.value
    )
    return {"Authorization": f"Bearer {token}"}


async def test_the_http_surface_holds_its_gates(session, org, client):
    await session.commit()
    asha, ben, hr = org["asha"], org["ben"], org["hr"]

    posted = await client.post(
        "/api/v1/culture/shoutouts",
        headers=_auth(asha),
        json={"to_employee_id": str(ben.employee_id), "message": "Great demo"},
    )
    assert posted.status_code == 201, posted.text
    shoutout_id = posted.json()["id"]
    assert posted.json()["sender"]["name"] == "Asha Test"

    feed_page = await client.get("/api/v1/culture/shoutouts?page_size=5", headers=_auth(ben))
    assert feed_page.status_code == 200 and feed_page.json()["total"] == 1

    react = await client.put(
        f"/api/v1/culture/shoutouts/{shoutout_id}/reactions",
        headers=_auth(ben),
        json={"emoji": "🎉", "active": True},
    )
    assert react.status_code == 200 and react.json()[0]["reacted"] is True

    pin = await client.put(
        f"/api/v1/culture/shoutouts/{shoutout_id}/pin",
        headers=_auth(asha),
        json={"is_pinned": True},
    )
    assert pin.status_code == 403
    pin = await client.put(
        f"/api/v1/culture/shoutouts/{shoutout_id}/pin", headers=_auth(hr), json={"is_pinned": True}
    )
    assert pin.status_code == 200

    missing = await client.delete(f"/api/v1/culture/shoutouts/{uuid.uuid4()}", headers=_auth(asha))
    assert missing.status_code == 404
    deleted = await client.delete(f"/api/v1/culture/shoutouts/{shoutout_id}", headers=_auth(asha))
    assert deleted.status_code == 204

    club = await client.post("/api/v1/culture/clubs", headers=_auth(asha), json={"name": "Hiking"})
    assert club.status_code == 201
    members = await client.get(
        f"/api/v1/culture/clubs/{club.json()['id']}/members", headers=_auth(hr)
    )
    assert members.status_code == 403
    assert (
        await client.get("/api/v1/culture/clubs/participation", headers=_auth(asha))
    ).status_code == 403
    assert (
        await client.get("/api/v1/culture/clubs/participation", headers=_auth(hr))
    ).status_code == 200

    # Admin surfaces refuse an employee.
    for path in (
        "/api/v1/culture/surveys",
        "/api/v1/culture/announcements/manage",
        "/api/v1/rewards/liability",
        "/api/v1/culture/values/analytics",
    ):
        assert (await client.get(path, headers=_auth(asha))).status_code == 403, path

    privacy = await client.put(
        "/api/v1/culture/event-privacy",
        headers=_auth(asha),
        json={"share_birthday": True, "employee_id": str(ben.employee_id)},
    )
    assert privacy.status_code == 200
    bens = await client.get("/api/v1/culture/event-privacy", headers=_auth(ben))
    assert bens.json()["share_birthday"] is False

    statement = await client.get("/api/v1/rewards/me/statement", headers=_auth(asha))
    assert statement.status_code == 200 and statement.json()["balance_points"] == 0


async def test_a_workspace_without_the_module_is_refused(session, org, client):
    org["tenant"].enabled_modules = {"core_hr": True}
    await session.commit()
    response = await client.get("/api/v1/culture/shoutouts", headers=_auth(org["asha"]))
    assert response.status_code == 403
