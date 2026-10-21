"""Emails to candidates: queued on hiring events, delivered with the link only in the email.

What these hold:

- a careers-page application is acknowledged by email, carrying the portal
  link — which is never readable in the database or in the message log;
- an interview booking, move and cancellation each reach the candidate;
- a rejection waits a day, and is withdrawn if the candidate is reopened first;
- a recruiter's own message is logged and can be withdrawn before it goes.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.candidate_comms import CandidateMessage, CandidateMessageStatus
from app.models.hiring import Candidate, CandidateStage
from app.models.interview import InterviewMode, InterviewStageType
from app.models.jobboard import CareersPageConfig
from app.schemas.hiring import CandidateCreate
from app.services.hiring import candidate_comms
from app.services.hiring import candidates as candidate_service
from app.services.interviews import service as interview_service
from app.services.notifications.adapters import ChannelAdapter, DeliveryResult
from tests.hiring.db_fixture import engine  # noqa: F401 - shadows the shared fixture
from tests.integration.conftest import requires_db
from tests.integration.test_hiring_portal_flow import (  # noqa: F401 - fixtures
    _auth,
    _open_requisition,
    client,
    people,
)

pytestmark = requires_db


class Outbox(ChannelAdapter):
    """Records what would have been sent."""

    def __init__(self) -> None:
        self.sent = []

    async def send(self, message):
        self.sent.append(message)
        return DeliveryResult(provider_message_id=f"test-{len(self.sent)}")


@pytest.fixture
def outbox(monkeypatch) -> Outbox:
    box = Outbox()

    async def adapter_for(_session, _tenant_id):
        return box, "test"

    monkeypatch.setattr(candidate_comms, "email_adapter_for", adapter_for)
    return box


async def _messages(session, candidate_id) -> list[CandidateMessage]:
    return list(
        (
            await session.scalars(
                select(CandidateMessage)
                .where(CandidateMessage.candidate_id == candidate_id)
                .order_by(CandidateMessage.created_at)
            )
        ).all()
    )


async def test_an_application_is_acknowledged_and_the_link_is_never_stored(
    client, session, tenant, people, outbox
) -> None:
    await _open_requisition(session, tenant, people)
    code = (await client.get("/api/v1/hiring/requisitions", headers=_auth(people["hr"]))).json()[
        "items"
    ][0]["code"]
    session.add(CareersPageConfig(tenant_id=tenant.id, slug="acme-jobs", is_published=True))
    await session.commit()

    applied = await client.post(
        f"/api/v1/public/careers/acme-jobs/jobs/{code}/apply",
        data={
            "full_name": "Meera Iyer",
            "email": "meera@example.com",
            "consent_application_processing": "true",
        },
    )
    assert applied.status_code == 201, applied.text
    token = applied.json()["portal_token"]
    candidate = await session.scalar(
        select(Candidate).where(Candidate.email == "meera@example.com")
    )

    [queued] = await _messages(session, candidate.id)
    assert queued.event_key == candidate_comms.APPLICATION_RECEIVED
    assert queued.subject == "We have your application for Senior Engineer"
    assert token not in queued.body and candidate_comms.LINK_SHOWN in queued.body
    assert queued.secret_link_encrypted and token not in queued.secret_link_encrypted

    outcome = await candidate_comms.deliver_due(session, people["admin"])
    assert outcome["sent"] == 1
    [email] = outbox.sent
    assert email.recipient_email == "meera@example.com"
    assert f"/candidate/{token}" in email.body, "the candidate gets the real link"

    await session.refresh(queued)
    assert queued.status is CandidateMessageStatus.SENT
    assert queued.secret_link_encrypted is None, "erased once sent"

    log = await client.get(
        f"/api/v1/hiring/candidates/{candidate.id}/messages", headers=_auth(people["hr"])
    )
    assert log.status_code == 200
    assert token not in log.text and log.json()[0]["status"] == "sent"


async def _candidate(session, people, requisition_id) -> uuid.UUID:
    added = await candidate_service.create_candidate(
        session,
        people["hr"],
        CandidateCreate(
            requisition_id=requisition_id,
            full_name="Arjun Rao",
            email="arjun@example.com",
            expected_ctc=Decimal("1800000"),
        ),
    )
    await session.commit()
    return uuid.UUID(added["candidate_id"])


async def test_interviews_reach_the_candidate(session, tenant, people, outbox) -> None:
    hr = people["hr"]
    requisition_id = await _open_requisition(session, tenant, people)
    candidate_id = await _candidate(session, people, requisition_id)
    stage = await interview_service.create_stage(
        session,
        hr,
        values={
            "requisition_id": requisition_id,
            "sequence": 1,
            "name": "Technical",
            "stage_type": InterviewStageType.TECHNICAL,
            "duration_minutes": 60,
            "mode": InterviewMode.VIDEO,
            "required_competencies": ["design"],
        },
    )
    await session.commit()
    start = datetime(2026, 11, 3, 10, 30)
    interview = await interview_service.schedule_interview(
        session,
        hr,
        candidate_id=candidate_id,
        stage_id=stage.id,
        start=start,
        panelist_user_ids=[people["alice"].id],
        location="https://meet.example/abc",
    )
    await session.commit()
    await interview_service.reschedule_interview(
        session,
        hr,
        interview_id=interview.id,
        start=start + timedelta(days=1),
        reason="Panel clash",
    )
    await session.commit()
    await interview_service.cancel_interview(
        session, hr, interview_id=interview.id, reason="Role on hold"
    )
    await session.commit()

    sent = [m for m in await _messages(session, candidate_id)]
    assert [m.event_key for m in sent] == [
        candidate_comms.INTERVIEW_SCHEDULED,
        candidate_comms.INTERVIEW_RESCHEDULED,
        candidate_comms.INTERVIEW_CANCELLED,
    ]
    booked = sent[0]
    assert "Tuesday 03 November 2026 at 10:30 AM (Asia/Kolkata)" in booked.body
    assert "Join here: https://meet.example/abc" in booked.body
    assert "Wednesday 04 November 2026" in sent[1].body

    assert (await candidate_comms.deliver_due(session, people["admin"]))["sent"] == 3
    assert outbox.sent[0].subject == "Your Technical interview with " + tenant.name


async def test_a_rejection_waits_and_is_withdrawn_if_reopened(
    session, tenant, people, outbox
) -> None:
    hr = people["hr"]
    requisition_id = await _open_requisition(session, tenant, people)
    candidate_id = await _candidate(session, people, requisition_id)

    await candidate_service.move_stage(
        session,
        hr,
        candidate_id=candidate_id,
        to_stage=CandidateStage.REJECTED,
        reason="Notice period too long",
    )
    await session.commit()
    [held] = await _messages(session, candidate_id)
    assert held.event_key == candidate_comms.REJECTED
    assert held.next_attempt_at > datetime.now(UTC) + timedelta(hours=23)

    assert (await candidate_comms.deliver_due(session, people["admin"]))["sent"] == 0
    assert outbox.sent == [], "not before the hold is up"

    await candidate_service.move_stage(
        session,
        hr,
        candidate_id=candidate_id,
        to_stage=CandidateStage.SCREENING,
        reason="They can join sooner after all",
    )
    await session.commit()
    await session.refresh(held)
    assert held.status is CandidateMessageStatus.CANCELLED

    # A rejection made without telling the candidate queues nothing.
    await candidate_service.move_stage(
        session,
        hr,
        candidate_id=candidate_id,
        to_stage=CandidateStage.REJECTED,
        reason="Duplicate profile",
        notify_candidate=False,
    )
    await session.commit()
    assert len(await _messages(session, candidate_id)) == 1


async def test_a_held_rejection_goes_after_a_day(session, tenant, people, outbox) -> None:
    requisition_id = await _open_requisition(session, tenant, people)
    candidate_id = await _candidate(session, people, requisition_id)
    await candidate_service.move_stage(
        session,
        people["hr"],
        candidate_id=candidate_id,
        to_stage=CandidateStage.REJECTED,
        reason="Skills gap in Postgres",
    )
    await session.commit()
    later = datetime.now(UTC) + timedelta(hours=25)
    assert (await candidate_comms.deliver_due(session, people["admin"], now=later))["sent"] == 1
    assert outbox.sent[0].subject == "Your application for Senior Engineer"
    assert "Skills gap" not in outbox.sent[0].body, "the internal reason stays internal"


async def test_a_recruiter_message_is_logged_and_can_be_withdrawn(
    client, session, tenant, people, outbox
) -> None:
    requisition_id = await _open_requisition(session, tenant, people)
    candidate_id = await _candidate(session, people, requisition_id)

    sent = await client.post(
        f"/api/v1/hiring/candidates/{candidate_id}/messages",
        json={
            "subject": "Documents for your interview",
            "message": "Please bring your last three payslips.",
            "include_portal_link": True,
        },
        headers=_auth(people["hr"]),
    )
    assert sent.status_code == 201, sent.text
    body = sent.json()
    assert body["sent_by"] == people["hr"].full_name
    assert candidate_comms.LINK_SHOWN in body["body"]

    withdrawn = await client.post(
        f"/api/v1/hiring/candidate-messages/{body['id']}/cancel", headers=_auth(people["hr"])
    )
    assert withdrawn.status_code == 200 and withdrawn.json()["status"] == "cancelled"
    assert (await candidate_comms.deliver_due(session, people["admin"]))["sent"] == 0

    again = await client.post(
        f"/api/v1/hiring/candidate-messages/{body['id']}/cancel", headers=_auth(people["hr"])
    )
    assert again.status_code == 409

    # Somebody outside the hiring team cannot read the log or write to the candidate.
    outsider = await client.get(
        f"/api/v1/hiring/candidates/{candidate_id}/messages", headers=_auth(people["outsider"])
    )
    assert outsider.status_code == 404


async def test_an_empty_message_is_refused(session, tenant, people) -> None:
    requisition_id = await _open_requisition(session, tenant, people)
    candidate_id = await _candidate(session, people, requisition_id)
    with pytest.raises(HTTPException) as exc:
        await candidate_comms.send_message(
            session,
            people["hr"],
            candidate_id=candidate_id,
            subject="  ",
            message="Hi",
            include_portal_link=False,
        )
    assert exc.value.status_code == 422


async def test_whatsapp_goes_only_to_a_candidate_who_opted_in(
    client, session, tenant, people, outbox, monkeypatch
) -> None:
    from app.models.notifications import (
        Channel,
        NotificationTemplate,
        WhatsAppTemplate,
        WhatsAppTemplateStatus,
    )

    whatsapp = Outbox()

    async def whatsapp_for(_session, _tenant_id):
        return whatsapp

    monkeypatch.setattr(candidate_comms, "whatsapp_adapter_for", whatsapp_for)
    meta = WhatsAppTemplate(
        tenant_id=tenant.id,
        meta_name="application_received",
        status=WhatsAppTemplateStatus.APPROVED,
        parameter_names=["first_name", "job_title", "link"],
    )
    session.add(meta)
    await session.flush()
    session.add(
        NotificationTemplate(
            tenant_id=tenant.id,
            event_key=candidate_comms.APPLICATION_RECEIVED,
            channel=Channel.WHATSAPP,
            body="Hi {first_name}, we have your application.",
            whatsapp_template_id=meta.id,
        )
    )
    await _open_requisition(session, tenant, people)
    code = (await client.get("/api/v1/hiring/requisitions", headers=_auth(people["hr"]))).json()[
        "items"
    ][0]["code"]
    session.add(CareersPageConfig(tenant_id=tenant.id, slug="acme-jobs", is_published=True))
    await session.commit()

    async def apply(email: str, phone: str, whatsapp_ok: bool) -> str:
        response = await client.post(
            f"/api/v1/public/careers/acme-jobs/jobs/{code}/apply",
            data={
                "full_name": "Meera Iyer",
                "email": email,
                "phone": phone,
                "consent_application_processing": "true",
                "consent_whatsapp": "true" if whatsapp_ok else "false",
            },
        )
        assert response.status_code == 201, response.text
        return response.json()["portal_token"]

    await apply("quiet@example.com", "98765 43210", whatsapp_ok=False)
    token = await apply("meera@example.com", "098765 43211", whatsapp_ok=True)
    candidate = await session.scalar(
        select(Candidate).where(Candidate.email == "meera@example.com")
    )
    queued = await _messages(session, candidate.id)
    assert sorted(m.channel for m in queued) == ["email", "whatsapp"]
    [wa] = [m for m in queued if m.channel == "whatsapp"]
    assert wa.to_address == "+919876543211"
    assert token not in str(wa.whatsapp_parameters), "the link is not readable at rest"

    outcome = await candidate_comms.deliver_due(session, people["admin"])
    assert outcome["sent"] == 3, "two emails and one WhatsApp"
    [sent] = whatsapp.sent
    assert sent.whatsapp_template_name == "application_received"
    assert sent.whatsapp_parameters[0] == "Meera" and token in sent.whatsapp_parameters[2]

    off = await client.post(f"/api/v1/public/candidate/{token}/whatsapp", json={"opt_in": False})
    assert off.status_code == 200 and off.json() == {"opted_in": False}
    await session.refresh(candidate)
    assert candidate_comms.whatsapp_on(candidate) is False
    await candidate_comms.on_portal_link(
        session,
        people["admin"],
        candidate=candidate,
        token="t",
        event_key=candidate_comms.APPLICATION_RECEIVED,
        payload={"job_title": "X"},
    )
    await session.commit()
    assert [m.channel for m in await _messages(session, candidate.id)].count("whatsapp") == 1


def test_whatsapp_numbers_are_read_as_indian_by_default() -> None:
    assert candidate_comms.whatsapp_number("98765 43210") == "+919876543210"
    assert candidate_comms.whatsapp_number("+44 20 7946 0958") == "+442079460958"
    assert candidate_comms.whatsapp_number("12345") is None
