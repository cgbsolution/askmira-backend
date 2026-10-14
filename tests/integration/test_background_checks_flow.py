"""Background verification: consent before anything starts, outcomes check by check.

What these hold:

- a request emails the candidate and waits; it cannot be settled before they agree;
- agreeing needs the candidate's own name, and records when and from where;
- a declined request is closed, with the candidate's answer as the reason;
- outcomes settle the check — a discrepancy needs a note, and outranks the rest;
- the candidate's portal shows the request, never the outcome.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.candidate_comms import CandidateMessage
from app.schemas.hiring import CandidateCreate
from app.services.hiring import background_checks as bgv
from app.services.hiring import candidates as candidate_service
from app.services.hiring import careers
from tests.hiring.db_fixture import engine  # noqa: F401 - shadows the shared fixture
from tests.integration.conftest import requires_db
from tests.integration.test_hiring_portal_flow import _open_requisition, people  # noqa: F401

pytestmark = requires_db


async def _candidate(session, people, tenant):
    requisition_id = await _open_requisition(session, tenant, people)
    added = await candidate_service.create_candidate(
        session, people["hr"],
        CandidateCreate(
            requisition_id=requisition_id, full_name="Tanvi Desai", email="tanvi@example.com",
            expected_ctc=Decimal("1600000"),
        ),
    )
    await session.commit()
    return uuid.UUID(added["candidate_id"])


async def _portal_token(session, hr, candidate_id) -> str:
    _row, token = await careers.issue_portal_token(session, hr, candidate_id=candidate_id)
    await session.commit()
    return token


async def test_consent_comes_first_and_outcomes_settle_it(session, tenant, people) -> None:
    hr = people["hr"]
    candidate_id = await _candidate(session, people, tenant)

    check = await bgv.request_check(
        session, hr, candidate_id=candidate_id, check_types=["employment", "education"],
        agency_name="Acme Verify",
    )
    await session.commit()
    assert check["status"] == "awaiting_consent"
    assert "Acme Verify" in check["consent_notice"], "the agency is named before consent"
    email = await session.scalar(
        select(CandidateMessage).where(CandidateMessage.candidate_id == candidate_id)
    )
    assert email.event_key == bgv.BGV_CONSENT and "Acme Verify" in email.body

    # Nothing can be recorded, and no second request opened, before they answer.
    with pytest.raises(HTTPException) as exc:
        await bgv.record_results(
            session, hr, uuid.UUID(check["id"]),
            results=[{"type": "employment", "status": "clear"}], summary=None,
        )
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        await bgv.request_check(
            session, hr, candidate_id=candidate_id, check_types=["identity"],
            agency_name="Other",
        )
    assert exc.value.status_code == 409

    token = await _portal_token(session, hr, candidate_id)
    [seen] = await bgv.portal_checks(session, token)
    assert seen["status"] == "awaiting_your_answer"
    with pytest.raises(HTTPException) as exc:
        await bgv.respond_to_check(
            session, token, uuid.UUID(check["id"]), agree=True, typed_name="Someone Else",
            ip_address="203.0.113.9", user_agent="test",
        )
    assert exc.value.status_code == 422

    agreed = await bgv.respond_to_check(
        session, token, uuid.UUID(check["id"]), agree=True, typed_name="  tanvi   DESAI ",
        ip_address="203.0.113.9", user_agent="test",
    )
    await session.commit()
    assert agreed["status"] == "agreed"

    # A discrepancy needs a note; with one, it settles the check as a discrepancy.
    with pytest.raises(HTTPException) as exc:
        await bgv.record_results(
            session, hr, uuid.UUID(check["id"]),
            results=[{"type": "employment", "status": "discrepancy"}], summary=None,
        )
    assert exc.value.status_code == 422
    partial = await bgv.record_results(
        session, hr, uuid.UUID(check["id"]),
        results=[{"type": "education", "status": "clear"}], summary=None,
    )
    assert partial["status"] == "in_progress", "one check still pending"
    settled = await bgv.record_results(
        session, hr, uuid.UUID(check["id"]),
        results=[{
            "type": "employment", "status": "discrepancy",
            "note": "Last day at previous employer was March, not June.",
        }],
        summary="Tenure overstated by three months.",
    )
    await session.commit()
    assert settled["status"] == "discrepancy" and settled["completed_at"]
    assert settled["checks"][0]["label"] == "Previous employment"

    # The candidate sees that they agreed — not what was found.
    [seen] = await bgv.portal_checks(session, token)
    assert seen["status"] == "agreed" and "discrepancy" not in str(seen)

    # HR only.
    with pytest.raises(HTTPException) as exc:
        await bgv.list_for_candidate(session, people["manager"], candidate_id)
    assert exc.value.status_code == 403


async def test_a_declined_request_is_closed(session, tenant, people) -> None:
    hr = people["hr"]
    candidate_id = await _candidate(session, people, tenant)
    check = await bgv.request_check(
        session, hr, candidate_id=candidate_id, check_types=["criminal"], agency_name="Acme",
    )
    await session.commit()
    token = await _portal_token(session, hr, candidate_id)
    declined = await bgv.respond_to_check(
        session, token, uuid.UUID(check["id"]), agree=False, typed_name=None,
        ip_address=None, user_agent=None,
    )
    await session.commit()
    assert declined["status"] == "declined"
    [row] = await bgv.list_for_candidate(session, hr, candidate_id)
    assert row["status"] == "cancelled" and "declined" in row["cancelled_reason"]

    with pytest.raises(HTTPException) as exc:
        await bgv.respond_to_check(
            session, token, uuid.UUID(check["id"]), agree=True, typed_name="Tanvi Desai",
            ip_address=None, user_agent=None,
        )
    assert exc.value.status_code == 409, "an answer is final"


async def test_unknown_checks_and_a_missing_agency_are_refused(session, tenant, people) -> None:
    candidate_id = await _candidate(session, people, tenant)
    with pytest.raises(HTTPException) as exc:
        await bgv.request_check(
            session, people["hr"], candidate_id=candidate_id, check_types=["horoscope"],
            agency_name="Acme",
        )
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException) as exc:
        await bgv.request_check(
            session, people["hr"], candidate_id=candidate_id, check_types=["identity"],
            agency_name="  ",
        )
    assert exc.value.status_code == 422
