"""Tags, talent pools and resume search.

What these hold:

- tags are normalised, filterable and counted only over candidates you can see;
- a pool admits only candidates who gave talent-pool consent, and shows a
  later withdrawal as withdrawn;
- search reads resume text by word, not just names;
- erasing a candidate takes their tags, pool places and emails with them.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.candidate_comms import CandidateMessage
from app.models.hiring import Candidate
from app.models.jobboard import CandidateConsent, ConsentPurpose
from app.models.talent_pools import TalentPoolMember
from app.schemas.hiring import CandidateCreate
from app.services.hiring import candidate_comms, careers, talent
from app.services.hiring import candidates as candidate_service
from tests.hiring.db_fixture import engine  # noqa: F401 - shadows the shared fixture
from tests.integration.conftest import requires_db
from tests.integration.test_hiring_portal_flow import _open_requisition, people  # noqa: F401

pytestmark = requires_db


async def _add(session, hr, requisition_id, name, email, *, resume: str | None = None):
    added = await candidate_service.create_candidate(
        session,
        hr,
        CandidateCreate(
            requisition_id=requisition_id,
            full_name=name,
            email=email,
            expected_ctc=Decimal("1500000"),
        ),
    )
    await session.commit()
    candidate = await session.get(Candidate, uuid.UUID(added["candidate_id"]))
    if resume is not None:
        candidate.parsed_resume = {"kind": "text_extraction", "status": "extracted", "text": resume}
        await session.commit()
    return candidate


def _consent(tenant, candidate, granted: bool, when: datetime) -> CandidateConsent:
    return CandidateConsent(
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        purpose=ConsentPurpose.TALENT_POOL_RETENTION,
        granted=granted,
        notice_text="Keep me on file for two years.",
        notice_version="v1",
        recorded_at=when,
    )


async def test_tags_are_normalised_filtered_and_counted(session, tenant, people) -> None:
    hr = people["hr"]
    requisition_id = await _open_requisition(session, tenant, people)
    one = await _add(session, hr, requisition_id, "Kavya Shah", "kavya@example.com")
    two = await _add(session, hr, requisition_id, "Dev Patel", "dev@example.com")

    assert await talent.set_tags(
        session, hr, candidate_id=one.id, tags=["  Java ", "C++", "java", "Campus 2026"]
    ) == ["java", "c++", "campus 2026"]
    await talent.set_tags(session, hr, candidate_id=two.id, tags=["java"])
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await talent.set_tags(session, hr, candidate_id=two.id, tags=["<script>"])
    assert exc.value.status_code == 422

    items, total = await candidate_service.list_candidates(session, hr, tag="Java")
    assert total == 2
    items, total = await candidate_service.list_candidates(session, hr, tag="c++")
    assert [i["full_name"] for i in items] == ["Kavya Shah"]
    assert items[0]["tags"] == ["java", "c++", "campus 2026"]

    counts = {row["tag"]: row["candidates"] for row in await talent.tag_counts(session, hr)}
    assert counts == {"java": 2, "c++": 1, "campus 2026": 1}
    # Somebody on no hiring team sees no tags at all.
    assert await talent.tag_counts(session, people["outsider"]) == []


async def test_search_reads_resumes_by_word(session, tenant, people) -> None:
    hr = people["hr"]
    requisition_id = await _open_requisition(session, tenant, people)
    await _add(
        session,
        hr,
        requisition_id,
        "Ishaan Mehta",
        "ishaan@example.com",
        resume="Built payment systems in Python. Tuned Postgres for a lending platform.",
    )
    await _add(
        session,
        hr,
        requisition_id,
        "Nisha Rao",
        "nisha@example.com",
        resume="Java and Spring Boot. Some Python scripting.",
    )
    found, _ = await candidate_service.list_candidates(session, hr, search="postgres python")
    assert [i["full_name"] for i in found] == ["Ishaan Mehta"], "both words, any order"
    found, _ = await candidate_service.list_candidates(session, hr, search="python")
    assert {i["full_name"] for i in found} == {"Ishaan Mehta", "Nisha Rao"}
    found, _ = await candidate_service.list_candidates(session, hr, search="nisha")
    assert [i["full_name"] for i in found] == ["Nisha Rao"], "names still match as typed"


async def test_a_pool_admits_only_those_who_agreed(session, tenant, people) -> None:
    hr = people["hr"]
    requisition_id = await _open_requisition(session, tenant, people)
    agreed = await _add(session, hr, requisition_id, "Aarav Jain", "aarav@example.com")
    never = await _add(session, hr, requisition_id, "Sara Khan", "sara@example.com")
    then = datetime.now(UTC) - timedelta(days=10)
    session.add(_consent(tenant, agreed, True, then))
    await session.commit()

    pool = await talent.create_pool(
        session, hr, name="Backend engineers", description="Near-misses worth a call"
    )
    await session.commit()
    with pytest.raises(HTTPException) as exc:
        await talent.create_pool(session, hr, name="backend ENGINEERS", description=None)
    assert exc.value.status_code == 409

    pool_id = uuid.UUID(pool["id"])
    result = await talent.add_members(
        session, hr, pool_id, candidate_ids=[agreed.id, never.id], note="Strong on systems"
    )
    await session.commit()
    assert result["added"] == [str(agreed.id)]
    [refusal] = result["refused"]
    assert refusal["candidate_id"] == str(never.id) and "talent-pool consent" in refusal["reason"]

    again = await talent.add_members(session, hr, pool_id, candidate_ids=[agreed.id], note=None)
    assert again["refused"][0]["reason"] == "Already in this pool."

    # A later withdrawal shows as withdrawn, not as a live prospect.
    session.add(_consent(tenant, agreed, False, datetime.now(UTC)))
    await session.commit()
    detail = await talent.pool_detail(session, hr, pool_id)
    assert detail["members"][0]["consent"] == "withdrawn"

    in_pool, _ = await candidate_service.list_candidates(session, hr, pool_id=pool_id)
    assert [i["full_name"] for i in in_pool] == ["Aarav Jain"]

    # Pools are HR's; a hiring manager cannot open one.
    with pytest.raises(HTTPException) as exc:
        await talent.list_pools(session, people["manager"])
    assert exc.value.status_code == 403

    await talent.remove_member(session, hr, pool_id, agreed.id)
    await session.commit()
    assert (await talent.pool_detail(session, hr, pool_id))["members"] == []


async def test_erasure_takes_tags_pools_and_emails(session, tenant, people) -> None:
    hr, admin = people["hr"], people["admin"]
    requisition_id = await _open_requisition(session, tenant, people)
    person = await _add(session, hr, requisition_id, "Rhea Kapoor", "rhea@example.com")
    session.add(_consent(tenant, person, True, datetime.now(UTC) - timedelta(days=1)))
    await session.commit()
    await talent.set_tags(session, hr, candidate_id=person.id, tags=["design"])
    pool = await talent.create_pool(session, hr, name="Designers", description=None)
    await talent.add_members(
        session, hr, uuid.UUID(pool["id"]), candidate_ids=[person.id], note=None
    )
    await candidate_comms.send_message(
        session,
        hr,
        candidate_id=person.id,
        subject="Hello Rhea",
        message="Are you free?",
        include_portal_link=False,
    )
    await session.commit()

    await careers.purge_on_request(session, admin, candidate_id=person.id, reason="Asked to")
    await session.commit()
    await session.refresh(person)
    assert person.tags == []
    assert (
        await session.scalar(
            select(TalentPoolMember).where(TalentPoolMember.candidate_id == person.id)
        )
        is None
    )
    [message] = (
        await session.scalars(
            select(CandidateMessage).where(CandidateMessage.candidate_id == person.id)
        )
    ).all()
    assert "Rhea" not in message.subject and "rhea@" not in message.to_address
    assert message.status.value == "cancelled"
