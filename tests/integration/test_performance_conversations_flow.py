"""Goals that count, check-ins, 1:1s, 360 feedback and letter acknowledgement — over HTTP.

Each test drives the portal's own endpoints as the people involved, so the
"who sees what" rules are exercised the way a browser exercises them: the
employee's goal reaching the cycle score, the manager answering a check-in,
a 1:1 nobody else can open, and a 360 whose writer the subject never learns.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import (
    AppraisalCycle,
    AuditLog,
    CycleStatus,
    CycleType,
    LetterAcknowledgement,
    Notification,
    Tenant,
    User,
    UserRole,
)
from app.models.documents import FieldSource, LetterCategory, LetterTemplate, LetterTemplateField
from app.models.employee import Employee
from app.services.performance import feedback as feedback_service
from app.services.scheduler.jobs import system_actor
from tests.integration import platform_support
from tests.integration.conftest import requires_db
from tests.integration.platform_support import bearer, make_person

pytestmark = requires_db

client = platform_support.client


@pytest.fixture(autouse=True)
def _storage_root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "FILE_STORAGE_ROOT", str(tmp_path / "store"))
    return tmp_path / "store"


@pytest.fixture
async def team(session: AsyncSession, tenant: Tenant) -> dict[str, User]:
    """A manager with one report, four of the report's peers, and HR."""
    maya = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Maya Menon")
    manager_employee = await session.get(Employee, maya.employee_id)
    people = {
        "maya": maya,
        "ravi": await make_person(
            session, tenant, role=UserRole.EMPLOYEE, name="Ravi Kumar", manager=manager_employee
        ),
        "hr": await make_person(session, tenant, role=UserRole.ADMIN, name="Asha Admin"),
    }
    for index, name in enumerate(["Deepa Nair", "Farhan Ali", "Gita Rao", "Hari Shah"]):
        people[f"p{index + 1}"] = await make_person(
            session, tenant, role=UserRole.EMPLOYEE, name=name
        )
    return people


async def _events(session: AsyncSession, key: str) -> set[uuid.UUID]:
    rows = await session.scalars(
        select(Notification.recipient_user_id).where(Notification.event_key == key)
    )
    return set(rows.all())


async def _cycle(session: AsyncSession, tenant: Tenant) -> AppraisalCycle:
    today = datetime.now(UTC).date()
    cycle = AppraisalCycle(
        tenant_id=tenant.id,
        name="FY27 H1",
        cycle_type=CycleType.HALF_YEARLY,
        status=CycleStatus.ACTIVE,
        period_start=today - timedelta(days=60),
        period_end=today + timedelta(days=120),
    )
    session.add(cycle)
    await session.commit()
    await session.refresh(cycle)
    return cycle


# ═══════════════════════════════════════════════════════════════════════════
# Goals: an employee's own goal counts, a check-in is answered
# ═══════════════════════════════════════════════════════════════════════════


async def test_employee_goal_counts_and_its_check_in_is_answered(
    client, session, tenant, team
) -> None:
    ravi, maya, hr, peer = team["ravi"], team["maya"], team["hr"], team["p1"]
    cycle = await _cycle(session, tenant)

    # The manager-only cycle list is still manager-only; the goal picker is not.
    assert (await client.get("/api/v1/appraisal/cycles", headers=bearer(ravi))).status_code == 403
    options = await client.get("/api/v1/goals/cycles", headers=bearer(ravi))
    assert options.status_code == 200, options.text
    assert [c["id"] for c in options.json()] == [str(cycle.id)]

    # Due after the review period ends, so it counts only because it is linked.
    created = await client.post(
        "/api/v1/goals",
        headers=bearer(ravi),
        json={
            "title": "Close 10 enterprise renewals",
            "weight": "100",
            "due_date": (cycle.period_end + timedelta(days=30)).isoformat(),
            "appraisal_cycle_id": str(cycle.id),
            "key_results": [{"title": "Renewals closed", "start_value": "0", "target_value": "10"}],
        },
    )
    assert created.status_code == 201, created.text
    goal = created.json()
    assert goal["latest_check_in"] is None
    kr = goal["key_results"][0]["id"]

    checked = await client.post(
        f"/api/v1/goals/{goal['id']}/check-ins",
        headers=bearer(ravi),
        json={
            "key_result_id": kr,
            "value": "5",
            "confidence": "at_risk",
            "note": "Two renewals waiting on legal",
        },
    )
    assert checked.status_code == 201, checked.text
    assert checked.json()["progress_percent"] == "50.00"

    mine = (await client.get("/api/v1/goals?scope=mine", headers=bearer(ravi))).json()
    assert mine[0]["latest_check_in"]["confidence"] == "at_risk"
    assert mine[0]["latest_check_in"]["note"] == "Two renewals waiting on legal"

    score = await client.get(
        f"/api/v1/goals/score?employee_id={ravi.employee_id}&cycle_id={cycle.id}",
        headers=bearer(ravi),
    )
    assert score.status_code == 200, score.text
    assert score.json()["score"] == "50.00"
    assert [g["goal_id"] for g in score.json()["contributing"]] == [goal["id"]]
    # The manager reads the same number for their report.
    assert (
        await client.get(
            f"/api/v1/goals/score?employee_id={ravi.employee_id}&cycle_id={cycle.id}",
            headers=bearer(maya),
        )
    ).json()["score"] == "50.00"

    # The manager answers the check-in; the employee hears about it.
    history = (
        await client.get(f"/api/v1/goals/{goal['id']}/check-ins", headers=bearer(maya))
    ).json()
    assert history[0]["can_comment"] is True and history[0]["comments"] == []
    reply = await client.post(
        f"/api/v1/goals/{goal['id']}/check-ins/{history[0]['id']}/comments",
        headers=bearer(maya),
        json={"body": "I'll chase legal on Monday."},
    )
    assert reply.status_code == 201, reply.text
    assert ravi.id in await _events(session, "performance.goal.check_in_commented")
    assert maya.id not in await _events(session, "performance.goal.check_in_commented")
    history = (
        await client.get(f"/api/v1/goals/{goal['id']}/check-ins", headers=bearer(ravi))
    ).json()
    assert history[0]["comments"][0]["body"] == "I'll chase legal on Monday."
    assert history[0]["comments"][0]["author_name"] == "Maya Menon"

    # A colleague outside the line can neither read nor answer it.
    outsider = await client.post(
        f"/api/v1/goals/{goal['id']}/check-ins/{history[0]['id']}/comments",
        headers=bearer(peer),
        json={"body": "Nice"},
    )
    assert outsider.status_code == 403

    # A status-only check-in says "done" and completes the goal.
    done = await client.post(
        f"/api/v1/goals/{goal['id']}/check-ins",
        headers=bearer(ravi),
        json={"confidence": "done", "note": "All ten closed."},
    )
    assert done.status_code == 201, done.text
    assert done.json()["status"] == "completed"
    assert done.json()["latest_check_in"]["confidence"] == "done"
    empty = await client.post(
        f"/api/v1/goals/{goal['id']}/check-ins", headers=bearer(ravi), json={}
    )
    assert empty.status_code == 422

    # Calibration moves the cycle into "in calibration" and back out.
    created_session = await client.post(
        "/api/v1/calibration/sessions",
        headers=bearer(hr),
        json={
            "cycle_id": str(cycle.id),
            "name": "Sales calibration",
            "scope": "company",
            "scope_value": "all",
        },
    )
    assert created_session.status_code == 201, created_session.text
    sid = created_session.json()["id"]
    started = await client.post(f"/api/v1/calibration/sessions/{sid}/start", headers=bearer(hr))
    assert started.status_code == 200, started.text
    await session.refresh(cycle)
    assert cycle.status is CycleStatus.IN_CALIBRATION
    cancelled = await client.post(f"/api/v1/calibration/sessions/{sid}/cancel", headers=bearer(hr))
    assert cancelled.status_code == 200, cancelled.text
    await session.refresh(cycle)
    assert cycle.status is CycleStatus.ACTIVE


# ═══════════════════════════════════════════════════════════════════════════
# 1:1s
# ═══════════════════════════════════════════════════════════════════════════


async def test_one_on_one_with_action_items_and_private_notes(client, session, team) -> None:
    ravi, maya, hr, peer = team["ravi"], team["maya"], team["hr"], team["p1"]
    today = datetime.now(UTC).date()

    scheduled = await client.post(
        "/api/v1/one-on-ones",
        headers=bearer(maya),
        json={
            "with_employee_id": str(ravi.employee_id),
            "scheduled_on": (today + timedelta(days=2)).isoformat(),
            "start_time": "10:30",
            "agenda": ["Renewal pipeline", "  ", "Training budget"],
        },
    )
    assert scheduled.status_code == 201, scheduled.text
    meeting = scheduled.json()
    assert meeting["my_side"] == "manager" and meeting["with_name"] == "Ravi Kumar"
    assert [i["body"] for i in meeting["items"]] == ["Renewal pipeline", "Training budget"]
    assert ravi.id in await _events(session, "performance.one_on_one.scheduled")
    mid = meeting["id"]

    # The report adds a talking point and an action item for the manager.
    await client.post(
        f"/api/v1/one-on-ones/{mid}/items",
        headers=bearer(ravi),
        json={"body": "Can I shadow the Pune deal?"},
    )
    action = await client.post(
        f"/api/v1/one-on-ones/{mid}/items",
        headers=bearer(ravi),
        json={
            "kind": "action_item",
            "body": "Share last year's renewal deck",
            "owner_employee_id": str(maya.employee_id),
            "due_on": (today + timedelta(days=5)).isoformat(),
        },
    )
    assert action.status_code == 201, action.text
    detail = action.json()
    assert detail["my_side"] == "report" and detail["open_action_items"] == 1
    action_id = next(i["id"] for i in detail["items"] if i["kind"] == "action_item")
    # An action item belongs to one of the two people in the room.
    stray = await client.post(
        f"/api/v1/one-on-ones/{mid}/items",
        headers=bearer(ravi),
        json={"kind": "action_item", "body": "x", "owner_employee_id": str(peer.employee_id)},
    )
    assert stray.status_code == 422

    # Private notes are the author's alone.
    noted = await client.put(
        f"/api/v1/one-on-ones/{mid}/my-note",
        headers=bearer(maya),
        json={"body": "Ravi seems stretched; ask about workload."},
    )
    assert noted.json()["my_note"] == "Ravi seems stretched; ask about workload."
    theirs = (await client.get(f"/api/v1/one-on-ones/{mid}", headers=bearer(ravi))).json()
    assert theirs["my_note"] is None
    note_audit = await session.scalar(
        select(AuditLog).where(AuditLog.entity_type == "one_on_one_private_note")
    )
    assert "stretched" not in str(note_audit.changes)

    # Nobody outside the meeting can open it — HR included.
    for outsider in (peer, hr):
        denied = await client.get(f"/api/v1/one-on-ones/{mid}", headers=bearer(outsider))
        assert denied.status_code == 404
    assert (await client.get("/api/v1/one-on-ones", headers=bearer(peer))).json() == []

    # The manager's per-report view.
    reports = (await client.get("/api/v1/one-on-ones/reports", headers=bearer(maya))).json()
    assert [r["name"] for r in reports] == ["Ravi Kumar"]
    assert reports[0]["next_one_on_one_id"] == mid and reports[0]["open_action_items"] == 1

    held = await client.post(
        f"/api/v1/one-on-ones/{mid}/complete",
        headers=bearer(maya),
        json={"summary": "Agreed Ravi shadows the Pune renewal."},
    )
    assert held.status_code == 200 and held.json()["status"] == "completed"
    closed_agenda = await client.post(
        f"/api/v1/one-on-ones/{mid}/items", headers=bearer(ravi), json={"body": "late"}
    )
    assert closed_agenda.status_code == 409

    # The report books the next one with their own manager; the open action
    # item carries over, and ticking it from the new meeting closes it.
    booked = await client.post(
        "/api/v1/one-on-ones",
        headers=bearer(ravi),
        json={
            "with_employee_id": str(maya.employee_id),
            "scheduled_on": (today + timedelta(days=16)).isoformat(),
        },
    )
    assert booked.status_code == 201, booked.text
    second = booked.json()
    assert second["my_side"] == "report"
    assert [i["id"] for i in second["carried_over"]] == [action_id]
    ticked = await client.patch(
        f"/api/v1/one-on-ones/{second['id']}/items/{action_id}",
        headers=bearer(maya),
        json={"done": True},
    )
    assert ticked.status_code == 200, ticked.text
    assert ticked.json()["carried_over"] == []

    # Not with a peer: a 1:1 is with your line or your own manager.
    refused = await client.post(
        "/api/v1/one-on-ones",
        headers=bearer(ravi),
        json={"with_employee_id": str(peer.employee_id), "scheduled_on": today.isoformat()},
    )
    assert refused.status_code == 403


# ═══════════════════════════════════════════════════════════════════════════
# 360 feedback and private feedback
# ═══════════════════════════════════════════════════════════════════════════


async def test_feedback_request_responses_and_anonymised_summary(
    client, session, tenant, team
) -> None:
    ravi, maya, hr = team["ravi"], team["maya"], team["hr"]
    peers = [team[f"p{i}"] for i in range(1, 5)]
    due = datetime.now(UTC).date() + timedelta(days=1)

    self_review = await client.post(
        "/api/v1/feedback/requests",
        headers=bearer(maya),
        json={
            "subject_employee_id": str(ravi.employee_id),
            "reviewer_employee_ids": [str(ravi.employee_id)],
            "due_on": due.isoformat(),
        },
    )
    assert self_review.status_code == 422

    created = await client.post(
        "/api/v1/feedback/requests",
        headers=bearer(maya),
        json={
            "subject_employee_id": str(ravi.employee_id),
            "reviewer_employee_ids": [str(p.employee_id) for p in peers],
            "message": "The Q2 renewals push",
            "due_on": due.isoformat(),
        },
    )
    assert created.status_code == 201, created.text
    request = created.json()
    assert request["reviewers_total"] == 4 and request["my_role"] == "manager"
    assert await _events(session, "performance.feedback.requested") == {p.id for p in peers}
    rid = request["id"]

    # "Feedback to give" in each reviewer's approvals inbox.
    inbox = (await client.get("/api/v1/approvals", headers=bearer(peers[0]))).json()
    mine = [i for i in inbox["items"] if i["source"] == "feedback_request"]
    assert [i["title"] for i in mine] == ["Feedback on Ravi Kumar"]
    reviewer_id = mine[0]["id"]
    form = (
        await client.get(f"/api/v1/feedback/responses/{reviewer_id}", headers=bearer(peers[0]))
    ).json()
    assert "anonymised summary" in form["visibility_statement"]
    # Nobody else can open that form.
    assert (
        await client.get(f"/api/v1/feedback/responses/{reviewer_id}", headers=bearer(peers[1]))
    ).status_code == 404

    async def answer(person: User, strengths: str, rating: str) -> None:
        items = (await client.get("/api/v1/feedback/inbox", headers=bearer(person))).json()
        submitted = await client.post(
            f"/api/v1/feedback/responses/{items[0]['reviewer_id']}/submit",
            headers=bearer(person),
            json={
                "strengths": strengths,
                "improvements": "Delegate more",
                "overall_rating": rating,
            },
        )
        assert submitted.status_code == 200, submitted.text

    await answer(peers[0], "Unblocks people", "4")
    await answer(peers[1], "Calm with customers", "5")

    # Two answers: the subject sees counts only, the manager sees names.
    subject_view = (
        await client.get(f"/api/v1/feedback/requests/{rid}", headers=bearer(ravi))
    ).json()
    assert subject_view["my_role"] == "subject" and subject_view["responses"] == 2
    assert subject_view["aggregate"] is None
    assert subject_view["reviewers"] is None and subject_view["responses_named"] is None
    manager_view = (
        await client.get(f"/api/v1/feedback/requests/{rid}", headers=bearer(maya))
    ).json()
    assert {r["reviewer_name"] for r in manager_view["responses_named"]} == {
        "Deepa Nair",
        "Farhan Ali",
    }

    await answer(peers[2], "Writes clear proposals", "3")
    assert ravi.id in await _events(session, "performance.feedback.summary_ready")

    # Three answers: an anonymised summary, and still no names anywhere in it.
    subject_view = (
        await client.get(f"/api/v1/feedback/requests/{rid}", headers=bearer(ravi))
    ).json()
    summary = subject_view["aggregate"]
    assert summary["responses"] == 3 and summary["overall_average"] == "4.00"
    assert summary["strengths"] == [
        "Calm with customers",
        "Unblocks people",
        "Writes clear proposals",
    ]
    assert summary["improvements"] == ["Delegate more"]
    assert subject_view["responses_named"] is None and subject_view["reviewers"] is None
    for name in ("Deepa", "Farhan", "Gita"):
        assert name not in str(subject_view)

    # An answer is given once; a reviewer cannot read the request itself.
    again = await client.post(
        f"/api/v1/feedback/responses/{reviewer_id}/submit",
        headers=bearer(peers[0]),
        json={"strengths": "twice"},
    )
    assert again.status_code == 409
    assert (
        await client.get(f"/api/v1/feedback/requests/{rid}", headers=bearer(peers[0]))
    ).status_code == 404

    # Reminders: by hand to the one still pending, then the scheduler holds off.
    reminded = await client.post(f"/api/v1/feedback/requests/{rid}/remind", headers=bearer(maya))
    assert reminded.json() == {"reminded": 1}
    assert await _events(session, "performance.feedback.reminder") == {peers[3].id}
    swept = await feedback_service.send_due_reminders(session, system_actor(tenant.id), on=due)
    assert swept == {"requests": 1, "reminded": 0}

    # HR sees every request.
    everything = (
        await client.get("/api/v1/feedback/requests?scope=all", headers=bearer(hr))
    ).json()
    assert [r["id"] for r in everything] == [rid]

    # Private feedback: the recipient and their manager read it; nobody else.
    sent = await client.post(
        "/api/v1/feedback/private",
        headers=bearer(peers[0]),
        json={
            "to_employee_id": str(ravi.employee_id),
            "kind": "suggestion",
            "body": "Send the recap the same day.",
        },
    )
    assert sent.status_code == 201, sent.text
    assert ravi.id in await _events(session, "performance.feedback.private_received")
    received = (await client.get("/api/v1/feedback/private", headers=bearer(ravi))).json()
    assert [r["body"] for r in received] == ["Send the recap the same day."]
    team_view = (
        await client.get("/api/v1/feedback/private?scope=team", headers=bearer(maya))
    ).json()
    assert [r["from_name"] for r in team_view] == ["Deepa Nair"]
    other = (await client.get("/api/v1/feedback/private", headers=bearer(peers[1]))).json()
    assert other == []


# ═══════════════════════════════════════════════════════════════════════════
# Letter acknowledgement
# ═══════════════════════════════════════════════════════════════════════════


async def test_employee_acknowledges_a_letter_in_the_portal(client, session, tenant, team) -> None:
    ravi, hr, peer = team["ravi"], team["hr"], team["p1"]
    template = LetterTemplate(
        tenant_id=tenant.id,
        code="APPT",
        name="Appointment letter",
        category=LetterCategory.APPOINTMENT,
        body_html="<p>Dear {{employee.first_name}}, you are appointed.</p>",
        version=1,
        active=True,
    )
    template.fields.append(
        LetterTemplateField(
            tenant_id=tenant.id,
            token="employee.first_name",
            label="First name",
            source=FieldSource.EMPLOYEE,
            required=True,
        )
    )
    session.add(template)
    await session.commit()

    issued = await client.post(
        "/api/v1/letters/issue",
        headers=bearer(hr),
        json={
            "employee_id": str(ravi.employee_id),
            "template_id": str(template.id),
            "as_of": date.today().isoformat(),
        },
    )
    assert issued.status_code == 201, issued.text
    letter = issued.json()
    assert letter["status"] == "approved" and letter["acknowledged_at"] is None
    assert ravi.id in await _events(session, "documents.letter.issued")
    lid = letter["id"]

    asked = await client.post(f"/api/v1/letters/{lid}/request-acknowledgement", headers=bearer(hr))
    assert asked.status_code == 200, asked.text
    assert asked.json()["acknowledgement_requested_at"] is not None
    assert ravi.id in await _events(session, "documents.letter.acknowledgement_requested")
    refused = await client.post(
        f"/api/v1/letters/{lid}/request-acknowledgement", headers=bearer(ravi)
    )
    assert refused.status_code == 403

    mine = (await client.get("/api/v1/letters", headers=bearer(ravi))).json()
    assert mine[0]["can_acknowledge"] is True

    wrong_name = await client.post(
        f"/api/v1/letters/{lid}/acknowledge",
        headers=bearer(ravi),
        json={"typed_name": "Somebody Else", "accept": True},
    )
    assert wrong_name.status_code == 422
    unticked = await client.post(
        f"/api/v1/letters/{lid}/acknowledge",
        headers=bearer(ravi),
        json={"typed_name": "Ravi Kumar", "accept": False},
    )
    assert unticked.status_code == 422
    # Nobody accepts on somebody else's behalf — a colleague or HR.
    for other in (peer, hr):
        denied = await client.post(
            f"/api/v1/letters/{lid}/acknowledge",
            headers=bearer(other),
            json={"typed_name": "Ravi Kumar", "accept": True},
        )
        assert denied.status_code == 404

    accepted = await client.post(
        f"/api/v1/letters/{lid}/acknowledge",
        headers=bearer(ravi),
        json={"typed_name": "  ravi   kumar ", "accept": True},
    )
    assert accepted.status_code == 200, accepted.text
    body = accepted.json()
    assert body["acknowledged_at"] is not None and body["acknowledged_name"] == "ravi kumar"
    assert body["can_acknowledge"] is False
    assert hr.id in await _events(session, "documents.letter.acknowledged")

    row = await session.scalar(
        select(LetterAcknowledgement).where(LetterAcknowledgement.letter_id == uuid.UUID(lid))
    )
    assert row is not None and row.content_hash == letter["content_hash"]
    assert row.ip_address is not None
    audited = await session.scalar(
        select(AuditLog).where(
            AuditLog.entity_type == "issued_letter",
            AuditLog.entity_id == uuid.UUID(lid),
            AuditLog.summary.like("%acknowledged letter%"),
        )
    )
    assert audited is not None and audited.actor_user_id == ravi.id

    hr_view = (await client.get("/api/v1/letters", headers=bearer(hr))).json()
    assert hr_view[0]["acknowledged_name"] == "ravi kumar"
    twice = await client.post(
        f"/api/v1/letters/{lid}/acknowledge",
        headers=bearer(ravi),
        json={"typed_name": "Ravi Kumar", "accept": True},
    )
    assert twice.status_code == 409


async def test_a_recurring_one_on_one_books_the_next(client, session, team) -> None:
    ravi, maya = team["ravi"], team["maya"]
    first_day = datetime.now(UTC).date() + timedelta(days=3)
    created = await client.post(
        "/api/v1/one-on-ones",
        headers=bearer(maya),
        json={
            "with_employee_id": str(ravi.employee_id),
            "scheduled_on": first_day.isoformat(),
            "start_time": "11:00",
            "repeat_every_weeks": 2,
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["repeat_every_weeks"] == 2
    held = await client.post(
        f"/api/v1/one-on-ones/{created.json()['id']}/complete",
        headers=bearer(maya),
        json={"summary": "Pipeline reviewed."},
    )
    assert held.status_code == 200, held.text
    listing = await client.get("/api/v1/one-on-ones", headers=bearer(maya))
    upcoming = [
        m
        for m in listing.json()
        if m["status"] == "scheduled" and m["with_employee_id"] == str(ravi.employee_id)
    ]
    assert [m["scheduled_on"] for m in upcoming] == [(first_day + timedelta(weeks=2)).isoformat()]
    assert upcoming[0]["repeat_every_weeks"] == 2
