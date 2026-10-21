"""Polls (one vote each, anonymous results) and team events (RSVPs, capacity)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee import Employee, EmploymentStatus
from app.models.engagement import RsvpResponse
from app.models.org import LegalEntity
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.engagement import EventCreate, PollCreate
from app.services.culture import engagement
from tests.integration.conftest import as_caller, requires_db

pytestmark = requires_db


async def _people(
    session: AsyncSession, tenant: Tenant, entity: LegalEntity, count: int
) -> list[User]:
    logins = []
    for n in range(count):
        person = Employee(
            tenant_id=tenant.id,
            employee_code=f"P{n}",
            first_name=f"Person{n}",
            work_email=f"p{n}@acme-demo.com",
            legal_entity_id=entity.id,
            status=EmploymentStatus.ACTIVE,
        )
        session.add(person)
        await session.flush()
        login = User(
            tenant_id=tenant.id,
            email=f"p{n}.login@acme-demo.com",
            password_hash="x",
            full_name=f"Person{n}",
            role=UserRole.EMPLOYEE,
            is_active=True,
            employee_id=person.id,
        )
        session.add(login)
        logins.append(login)
    await session.commit()
    return [await as_caller(session, login) for login in logins]


async def test_a_poll_shows_results_only_after_voting_and_never_who(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    people = await _people(session, tenant, legal_entity, 4)
    poll = await engagement.create_poll(
        session,
        hr_user,
        PollCreate(question="Where should the offsite be?", options=["Goa", "Coorg", "Ooty"]),
        request=None,
    )
    poll_id = uuid.UUID(poll["id"])
    with pytest.raises(HTTPException):
        await engagement.create_poll(
            session, people[0], PollCreate(question="Mine?", options=["a", "b"]), request=None
        )

    before = (await engagement.list_polls(session, people[0]))[0]
    assert before["results"] is None and before["results_note"] == "Vote to see the results."

    first = await engagement.vote(session, people[0], poll_id, 1)
    # One answer is too few to show a split, even to the voter.
    assert first["results"] is None and first["your_vote"] == 1
    await engagement.vote(session, people[1], poll_id, 1)
    third = await engagement.vote(session, people[2], poll_id, 0)
    assert [r["votes"] for r in third["results"]] == [1, 2, 0]
    # Changing a vote moves it, it does not add one.
    changed = await engagement.vote(session, people[2], poll_id, 2)
    assert changed["responses"] == 3
    assert all("employee" not in str(key) for key in changed)

    with pytest.raises(HTTPException):
        await engagement.vote(session, people[3], poll_id, 7)
    await engagement.close_poll(session, hr_user, poll_id, request=None)
    with pytest.raises(HTTPException) as closed:
        await engagement.vote(session, people[3], poll_id, 0)
    assert closed.value.status_code == 409
    after = (await engagement.list_polls(session, people[3]))[0]
    assert after["results"] is not None


async def test_a_full_event_refuses_another_going(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    people = await _people(session, tenant, legal_entity, 3)
    start = datetime.now(UTC) + timedelta(days=10)
    event = await engagement.create_event(
        session,
        hr_user,
        EventCreate(title="Diwali lunch", starts_at=start, venue="Cafeteria", capacity=2),
        request=None,
    )
    event_id = uuid.UUID(event["id"])

    await engagement.rsvp(session, people[0], event_id, RsvpResponse.GOING)
    await engagement.rsvp(session, people[1], event_id, RsvpResponse.GOING)
    with pytest.raises(HTTPException) as full:
        await engagement.rsvp(session, people[2], event_id, RsvpResponse.GOING)
    assert "full" in full.value.detail
    maybe = await engagement.rsvp(session, people[2], event_id, RsvpResponse.MAYBE)
    assert (maybe["going"], maybe["maybe"], maybe["is_full"]) == (2, 1, True)
    assert maybe["attendees"] is None

    # A place frees up when someone changes their answer.
    await engagement.rsvp(session, people[0], event_id, RsvpResponse.NOT_GOING)
    now_going = await engagement.rsvp(session, people[2], event_id, RsvpResponse.GOING)
    assert now_going["going"] == 2

    seen_by_hr = (await engagement.list_events(session, hr_user))[0]
    assert len(seen_by_hr["attendees"]) == 2

    await engagement.cancel_event(session, hr_user, event_id, request=None)
    with pytest.raises(HTTPException):
        await engagement.rsvp(session, people[1], event_id, RsvpResponse.MAYBE)
