"""Birthdays and work anniversaries: default sharing, the card, the email.

What these hold:

- by default everybody is celebrated in front of the whole workspace; a
  person's own choice (off, or department only) always wins;
- each birthday and each work anniversary (every year) is wished once, with a
  card in the template HR has in use, emailed to the person with the card in it;
- a person who keeps theirs private is still wished, privately;
- the card on the shared feed carries a photo only if its owner lets colleagues
  see their photo; the emailed card always carries it;
- a card on a post somebody may not see is a 404 to them.
"""

from __future__ import annotations

import io
from datetime import date

import pytest
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import select

from app.core.config import settings
from app.models.culture import CelebrationTemplate, MilestoneRule, Shoutout, ShoutoutVisibility
from app.models.culture_depth import EventPrivacyPreference
from app.models.dpdp import ConsentCapturedVia, DataPrincipalKind
from app.models.employee import Employee, EmployeeProfile, EmploymentStatus
from app.models.notifications import Channel, Notification
from app.models.org import Department
from app.models.user import User, UserRole
from app.services.culture import celebrations, feed
from app.services.culture.cards import CardDesign
from app.services.dpdp import service as dpdp_service
from app.services.notifications import dispatch
from app.services.notifications.adapters import ChannelAdapter, DeliveryResult, OutboundMessage
from app.services.storage import get_storage
from tests.integration.conftest import as_caller, requires_db

pytestmark = requires_db

TODAY = date(2026, 11, 12)


class Mail(ChannelAdapter):
    name = "test-mail"

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        self.sent.append(message)
        return DeliveryResult(provider_message_id="m-1")


@pytest.fixture(autouse=True)
def _storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "FILE_STORAGE_ROOT", str(tmp_path / "store"))


@pytest.fixture
def mail(monkeypatch) -> Mail:
    adapter = Mail()
    monkeypatch.setitem(dispatch._ADAPTERS, Channel.EMAIL, adapter)
    return adapter


async def _person(
    session,
    tenant,
    entity,
    location,
    code,
    *,
    born=None,
    joined=date(2023, 1, 1),
    department=None,
    login=False,
):
    employee = Employee(
        tenant_id=tenant.id,
        employee_code=code,
        first_name=code.title(),
        last_name="Test",
        work_email=f"{code.lower()}@acme-demo.com",
        legal_entity_id=entity.id,
        location_id=location.id,
        department_id=department.id if department else None,
        status=EmploymentStatus.ACTIVE,
        date_of_birth=born,
        date_of_joining=joined,
    )
    session.add(employee)
    await session.flush()
    user = None
    if login:
        user = User(
            tenant_id=tenant.id,
            email=f"{code.lower()}@acme-demo.com",
            password_hash="x",
            full_name=f"{code.title()} Test",
            role=UserRole.EMPLOYEE,
            is_active=True,
            employee_id=employee.id,
        )
        session.add(user)
    await session.commit()
    if user is not None:
        user = await as_caller(session, user)
    return employee, user


async def _posts(session) -> dict:
    return {
        (p.to_employee_id, p.celebration): p
        for p in (await session.scalars(select(Shoutout))).all()
    }


async def test_by_default_everybody_is_celebrated_company_wide_and_emailed(
    session, tenant, hr_user, legal_entity, location, mail
) -> None:
    asha, asha_login = await _person(
        session, tenant, legal_entity, location, "ASHA", born=date(1994, 11, 12), login=True
    )
    ravi, _ = await _person(
        session, tenant, legal_entity, location, "RAVI", joined=date(2023, 11, 12)
    )

    tally = await celebrations.run_daily(session, hr_user, today=TODAY)
    assert tally["birthdays"] == 1 and tally["anniversaries"] == 1
    assert tally["posted"] == 2 and tally["emailed"] == 2

    posts = await _posts(session)
    birthday = posts[(asha.id, "birthday")]
    anniversary = posts[(ravi.id, "work_anniversary")]
    assert birthday.visibility is ShoutoutVisibility.COMPANY
    assert anniversary.visibility is ShoutoutVisibility.COMPANY
    assert anniversary.milestone_years == 3 and anniversary.celebration_year == 2026
    assert birthday.card_key and anniversary.card_key

    card = await get_storage().get(birthday.card_key)
    assert Image.open(io.BytesIO(card)).size == (1080, 1080)

    by_address = {m.recipient_email: m for m in mail.sent}
    wish = by_address["asha@acme-demo.com"]
    assert wish.subject == "Happy birthday, Asha!"
    assert "cid:celebration-card" in (wish.html or "")
    assert any(
        i.cid == "celebration-card" and i.mime_type == "image/jpeg" for i in wish.inline_images
    )
    # Somebody with no login is still emailed, at their work address.
    assert by_address["ravi@acme-demo.com"].subject == "Happy 3rd work anniversary, Ravi!"

    notices = (
        await session.scalars(
            select(Notification).where(Notification.recipient_user_id == asha_login.id)
        )
    ).all()
    assert {n.event_key for n in notices} == {"culture.birthday"}

    again = await celebrations.run_daily(session, hr_user, today=TODAY)
    assert again["posted"] == 0 and again["emailed"] == 0
    assert len(mail.sent) == 2


async def test_a_persons_own_choice_beats_the_default(
    session, tenant, hr_user, legal_entity, location, mail
) -> None:
    sales = Department(tenant_id=tenant.id, name="Sales", code="SAL")
    session.add(sales)
    await session.flush()
    quiet, _ = await _person(
        session, tenant, legal_entity, location, "QUIET", born=date(1990, 11, 12)
    )
    team, _ = await _person(
        session, tenant, legal_entity, location, "TEAM", born=date(1991, 11, 12), department=sales
    )
    session.add_all(
        [
            EventPrivacyPreference(tenant_id=tenant.id, employee_id=quiet.id),
            EventPrivacyPreference(
                tenant_id=tenant.id,
                employee_id=team.id,
                share_birthday=True,
                birthday_scope="team",
            ),
        ]
    )
    await session.commit()

    await celebrations.run_daily(session, hr_user, today=TODAY)
    posts = await _posts(session)
    assert posts[(quiet.id, "birthday")].visibility is ShoutoutVisibility.PRIVATE
    assert posts[(team.id, "birthday")].visibility is ShoutoutVisibility.DEPARTMENT
    # Private is still a wish: the email goes.
    assert {m.recipient_email for m in mail.sent} == {"quiet@acme-demo.com", "team@acme-demo.com"}


async def test_hr_can_make_the_workspace_default_private(
    session, tenant, hr_user, legal_entity, location, mail
) -> None:
    person, _ = await _person(
        session, tenant, legal_entity, location, "PRIV", born=date(1990, 11, 12)
    )
    await celebrations.save_settings(
        session, hr_user, celebrations.CelebrationSettings(share_by_default=False)
    )
    await session.commit()
    await celebrations.run_daily(session, hr_user, today=TODAY)
    assert (await _posts(session))[(person.id, "birthday")].visibility is ShoutoutVisibility.PRIVATE


async def test_switches_for_email_feed_and_each_occasion(
    session, tenant, hr_user, legal_entity, location, mail
) -> None:
    both, _ = await _person(
        session,
        tenant,
        legal_entity,
        location,
        "BOTH",
        born=date(1990, 11, 12),
        joined=date(2020, 11, 12),
    )
    await celebrations.save_settings(
        session,
        hr_user,
        celebrations.CelebrationSettings(birthdays=False, send_email=False, post_to_feed=False),
    )
    await session.commit()
    tally = await celebrations.run_daily(session, hr_user, today=TODAY)
    assert tally["birthdays"] == 0 and tally["anniversaries"] == 1 and tally["emailed"] == 0
    posts = await _posts(session)
    assert (both.id, "birthday") not in posts
    assert posts[(both.id, "work_anniversary")].visibility is ShoutoutVisibility.PRIVATE
    assert mail.sent == []


async def test_every_anniversary_is_wished_and_a_milestone_rule_adds_its_message(
    session, tenant, hr_user, legal_entity, location, mail
) -> None:
    second, _ = await _person(
        session, tenant, legal_entity, location, "TWO", joined=date(2024, 11, 12)
    )
    fifth, _ = await _person(
        session, tenant, legal_entity, location, "FIVE", joined=date(2021, 11, 12)
    )
    joiner, _ = await _person(
        session, tenant, legal_entity, location, "NEW", joined=date(2026, 11, 12)
    )
    session.add(MilestoneRule(tenant_id=tenant.id, years=5, message="Five years, {name}!"))
    await session.commit()

    await celebrations.run_daily(session, hr_user, today=TODAY)
    posts = await _posts(session)
    assert posts[(second.id, "work_anniversary")].milestone_years == 2
    assert posts[(fifth.id, "work_anniversary")].message == "Five years, Five!"
    # Joining today is not an anniversary.
    assert (joiner.id, "work_anniversary") not in posts


async def test_a_whole_name_in_the_first_name_field_is_greeted_by_its_first_word(
    session, tenant, hr_user, legal_entity, location, mail
) -> None:
    """The factoHR import stored "Roja Bhaskar Poojari" as the first name with no last name."""
    person, _ = await _person(
        session, tenant, legal_entity, location, "ROJA", born=date(1995, 11, 12)
    )
    person.first_name, person.last_name = "Roja Bhaskar Poojari", ""
    await session.commit()
    await celebrations.run_daily(session, hr_user, today=TODAY)
    assert mail.sent[0].subject == "Happy birthday, Roja!"
    assert "Roja!" in (await _posts(session))[(person.id, "birthday")].message


async def test_a_29_february_birthday_is_wished_on_the_28th(
    session, tenant, hr_user, legal_entity, location, mail
) -> None:
    leap, _ = await _person(session, tenant, legal_entity, location, "LEAP", born=date(1996, 2, 29))
    assert (await celebrations.run_daily(session, hr_user, today=date(2027, 2, 27)))["posted"] == 0
    assert (await celebrations.run_daily(session, hr_user, today=date(2027, 2, 28)))["posted"] == 1
    assert (leap.id, "birthday") in await _posts(session)


async def test_a_legacy_milestone_post_is_not_repeated(
    session, tenant, hr_user, legal_entity, location, mail
) -> None:
    person, _ = await _person(
        session, tenant, legal_entity, location, "OLD", joined=date(2021, 11, 12)
    )
    session.add(
        Shoutout(
            tenant_id=tenant.id,
            to_employee_id=person.id,
            message="Already marked",
            milestone_years=5,
        )
    )
    await session.commit()
    assert (await celebrations.run_daily(session, hr_user, today=TODAY))["posted"] == 0


async def test_the_shared_card_shows_a_photo_only_with_consent(
    session, tenant, hr_user, legal_entity, location, mail
) -> None:
    person, _ = await _person(
        session, tenant, legal_entity, location, "FACE", born=date(1990, 11, 12)
    )
    buffer = io.BytesIO()
    Image.new("RGB", (300, 300), (10, 120, 200)).save(buffer, "JPEG")
    storage = get_storage()
    key = storage.new_key(tenant.id, "photos", ".jpg")
    await storage.put(key, buffer.getvalue())
    session.add(EmployeeProfile(tenant_id=tenant.id, employee_id=person.id, photo_storage_key=key))
    await session.commit()

    await celebrations.run_daily(session, hr_user, today=TODAY)
    post = (await _posts(session))[(person.id, "birthday")]
    feed_card = await storage.get(post.card_key)
    emailed = next(i for i in mail.sent[0].inline_images if i.cid == "celebration-card").data
    assert feed_card != emailed, "The shared card showed a photo its owner keeps to HR."

    # With consent, the next card (next year) carries the same picture as the email.
    await dpdp_service.record_consent(
        session,
        hr_user,
        principal_id=person.id,
        principal_kind=DataPrincipalKind.EMPLOYEE,
        purpose_key="directory_visibility",
        granted=True,
        captured_via=ConsentCapturedVia.SELF_SERVICE,
    )
    await celebrations.run_daily(session, hr_user, today=date(2027, 11, 12))
    rows = (
        await session.scalars(
            select(Shoutout).where(
                Shoutout.to_employee_id == person.id, Shoutout.celebration_year == 2027
            )
        )
    ).all()
    later = await storage.get(rows[0].card_key)
    emailed_later = next(i for i in mail.sent[-1].inline_images if i.cid == "celebration-card").data
    assert later == emailed_later


async def test_the_template_in_use_is_the_one_drawn(
    session, tenant, hr_user, legal_entity, location, mail
) -> None:
    first = await celebrations.create_template(
        session, hr_user, kind="birthday", name="Confetti", preset_key="birthday_confetti"
    )
    second = await celebrations.create_template(
        session,
        hr_user,
        kind="birthday",
        name="Ours",
        design=CardDesign(message="Cheers {first_name}, from {company}!"),
    )
    await session.commit()
    assert first["in_use"] is True and second["in_use"] is False

    await celebrations.set_in_use(session, hr_user, uuid_of(second))
    await session.commit()
    listed = await celebrations.list_templates(session, hr_user)
    in_use = [t["name"] for t in listed["templates"] if t["in_use"]]
    assert in_use == ["Ours"]

    person, _ = await _person(
        session, tenant, legal_entity, location, "CUSTOM", born=date(1990, 11, 12)
    )
    await celebrations.run_daily(session, hr_user, today=TODAY)
    post = (await _posts(session))[(person.id, "birthday")]
    assert post.message == "Cheers Custom, from Acme Technologies Pvt Ltd!"

    # Removing the one in use hands over to the other.
    await celebrations.delete_template(session, hr_user, uuid_of(second))
    await session.commit()
    rows = (await session.scalars(select(CelebrationTemplate))).all()
    assert [(r.name, r.is_default) for r in rows] == [("Confetti", True)]


async def test_a_preset_for_the_other_occasion_is_refused(session, hr_user) -> None:
    with pytest.raises(HTTPException) as exc:
        await celebrations.create_template(
            session, hr_user, kind="birthday", name="X", preset_key="anniversary_ice"
        )
    assert exc.value.status_code == 422


async def test_templates_are_hr_only_and_tenant_scoped(
    session, tenant, hr_user, other_hr_user, legal_entity, location
) -> None:
    _, worker = await _person(session, tenant, legal_entity, location, "WORK", login=True)
    with pytest.raises(HTTPException) as exc:
        await celebrations.list_templates(session, worker)
    assert exc.value.status_code == 403
    made = await celebrations.create_template(session, hr_user, kind="birthday", name="Ours")
    await session.commit()
    with pytest.raises(HTTPException) as exc:
        await celebrations.update_template(session, other_hr_user, uuid_of(made), name="Taken")
    assert exc.value.status_code == 404


async def test_a_private_card_is_a_404_to_colleagues_and_the_feed_says_birthday(
    session, tenant, hr_user, legal_entity, location, mail
) -> None:
    quiet, quiet_login = await _person(
        session, tenant, legal_entity, location, "SHY", born=date(1990, 11, 12), login=True
    )
    open_, _ = await _person(
        session, tenant, legal_entity, location, "OPEN", born=date(1990, 11, 12)
    )
    _, colleague = await _person(session, tenant, legal_entity, location, "PEER", login=True)
    session.add(EventPrivacyPreference(tenant_id=tenant.id, employee_id=quiet.id))
    await session.commit()
    await celebrations.run_daily(session, hr_user, today=TODAY)
    posts = await _posts(session)

    with pytest.raises(HTTPException) as exc:
        await celebrations.card_image(session, colleague, posts[(quiet.id, "birthday")].id)
    assert exc.value.status_code == 404
    assert await celebrations.card_image(session, quiet_login, posts[(quiet.id, "birthday")].id)
    assert await celebrations.card_image(session, colleague, posts[(open_.id, "birthday")].id)

    items, _ = await feed.list_feed(session, colleague, page=1, page_size=20)
    shown = [i for i in items if i.to_employee_id == open_.id]
    assert shown and shown[0].kind == "birthday"
    assert shown[0].card_url == f"/api/v1/culture/shoutouts/{shown[0].id}/card"
    assert all(i.to_employee_id != quiet.id for i in items)


async def test_my_celebrations_shows_the_workspace_default_until_i_choose(
    session, tenant, hr_user, legal_entity, location
) -> None:
    from app.services.culture import comms_portal

    _, me = await _person(session, tenant, legal_entity, location, "SELF", login=True)
    mine = await comms_portal.get_my_event_privacy(session, me)
    assert mine.share_birthday is True and mine.share_work_anniversary is True
    assert mine.birthday_scope == "company" and mine.configured is False


async def test_the_test_email_reaches_the_hr_person(session, tenant, hr_user, mail) -> None:
    result = await celebrations.send_test(
        session, hr_user, kind="work_anniversary", design=CardDesign()
    )
    assert result["sent_to"] == "hr@acme.test"
    assert mail.sent[0].subject.startswith("[Test] Happy 3rd work anniversary")
    assert any(i.cid == "celebration-card" for i in mail.sent[0].inline_images)


async def test_preview_draws_the_card_for_a_sample_when_hr_has_no_record(session, hr_user) -> None:
    data = await celebrations.preview(session, hr_user, kind="birthday", design=CardDesign())
    assert Image.open(io.BytesIO(data)).size == (1080, 1080)


def uuid_of(row: dict):
    import uuid

    return uuid.UUID(row["id"])
