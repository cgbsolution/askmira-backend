"""Slice 21 gates that need a real database.

The engines and adapters are proved in `tests/hiring/`. What only a database
settles: that a reschedule really does keep the same calendar event, that a
decline is recorded as having reached the recruiter, that the advance gate holds
against a direct service call, and that self-scheduling books the right instant
across two timezones.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_secret
from app.models.hiring import Candidate, Requisition, RequisitionStatus
from app.models.interview import (
    CalendarConnection,
    CalendarProviderKind,
    Interview,
    InterviewFeedback,
    InterviewMode,
    InterviewPanelist,
    InterviewStage,
    InterviewStageType,
    InterviewStatus,
    PanelistRole,
    Recommendation,
    ResponseStatus,
)
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.calendar import registry as calendar_registry
from app.services.calendar.base import (
    CalendarAdapter,
    CalendarPermanentFailure,
    CalendarProvider,
    EventResult,
    FreeBusyResult,
)
from app.services.interviews import service as interview_service
from tests.hiring.db_fixture import engine  # noqa: F401 - shadows the shared fixture
from tests.integration.conftest import requires_db

pytestmark = requires_db

NOW = datetime(2026, 9, 7, 6, 0, tzinfo=UTC)


# ═══════════════════════════════════════════════════════════════════════════
# Doubles
# ═══════════════════════════════════════════════════════════════════════════


class ScriptedCalendar(CalendarAdapter):
    """Records what it was asked to do, so the test can assert on the shape of
    the call rather than only on the result."""

    provider = CalendarProvider.GOOGLE

    def __init__(self, *, busy=None, read_fails: bool = False, raises=None) -> None:
        self.busy = busy or ()
        self.read_fails = read_fails
        self.raises = raises
        self.created: list = []
        self.updated: list[str] = []
        self.cancelled: list[str] = []
        self._next_id = 0

    async def free_busy(self, accounts, *, start, end, token):
        if self.read_fails:
            return [
                FreeBusyResult(account=a, ok=False, error="consent revoked")
                for a in accounts
            ]
        return [FreeBusyResult(account=a, busy=tuple(self.busy)) for a in accounts]

    async def create_event(self, spec, *, token):
        if self.raises:
            raise self.raises
        self._next_id += 1
        event_id = f"evt-{self._next_id}"
        self.created.append((event_id, spec))
        return EventResult(
            external_event_id=event_id, join_url=f"https://meet.test/{event_id}"
        )

    async def update_event(self, external_event_id, spec, *, token):
        if self.raises:
            raise self.raises
        self.updated.append(external_event_id)
        return EventResult(
            external_event_id=external_event_id,
            join_url=f"https://meet.test/{external_event_id}",
        )

    async def cancel_event(self, external_event_id, *, token):
        self.cancelled.append(external_event_id)

    async def responses(self, external_event_id, *, token):
        return {}


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    calendar_registry._OVERRIDES.clear()


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════


async def _connect(session: AsyncSession, tenant: Tenant, user: User) -> CalendarConnection:
    connection = CalendarConnection(
        tenant_id=tenant.id,
        user_id=user.id,
        provider=CalendarProviderKind.GOOGLE,
        account_email=user.email,
        access_token_encrypted=encrypt_secret("token", tenant.id),
        # A token that is valid now. The expiry check runs on the real clock —
        # rightly — so pinning this to the fixed NOW made every calendar look
        # expired once the calendar moved past NOW + 1 hour.
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    session.add(connection)
    await session.commit()
    return connection


async def _panelist_user(
    session: AsyncSession, tenant: Tenant, email: str
) -> User:
    person = User(
        tenant_id=tenant.id, email=email, password_hash="not-a-real-hash",
        full_name=email.split("@")[0], role=UserRole.EMPLOYEE, is_active=True,
    )
    session.add(person)
    await session.commit()
    await session.refresh(person)
    return person


async def _stage(
    session: AsyncSession, tenant: Tenant, *, required=("comm",), feedback_required=True
) -> InterviewStage:
    requisition = Requisition(
        tenant_id=tenant.id, code=f"REQ-{datetime.now(UTC).timestamp()}",
        title="Senior Engineer", status=RequisitionStatus.OPEN,
    )
    session.add(requisition)
    await session.flush()
    stage = InterviewStage(
        tenant_id=tenant.id, requisition_id=requisition.id, sequence=1,
        name="Technical round", stage_type=InterviewStageType.TECHNICAL,
        duration_minutes=60, mode=InterviewMode.VIDEO,
        required_competencies=list(required), feedback_required=feedback_required,
        preparation_notes="Bring a laptop.",
    )
    session.add(stage)
    await session.commit()
    await session.refresh(stage)
    return stage


async def _candidate(session: AsyncSession, tenant: Tenant, stage: InterviewStage) -> Candidate:
    candidate = Candidate(
        tenant_id=tenant.id, requisition_id=stage.requisition_id,
        full_name="Rohit Verma", email="rohit@example.test", phone="9876543210",
        fit_score=Decimal("82.5"),
    )
    session.add(candidate)
    await session.commit()
    await session.refresh(candidate)
    return candidate


# ═══════════════════════════════════════════════════════════════════════════
# Availability
# ═══════════════════════════════════════════════════════════════════════════


async def test_free_busy_excludes_genuinely_busy_slots(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """The checklist gate."""
    stage = await _stage(session, tenant)
    panelist = await _panelist_user(session, tenant, "alice@acme.test")
    await _connect(session, tenant, panelist)

    busy_start = datetime(2026, 9, 7, 5, 30, tzinfo=UTC)
    calendar_registry.register_adapter(
        CalendarProvider.GOOGLE,
        ScriptedCalendar(busy=[(busy_start, busy_start + timedelta(hours=2))]),
    )

    result = await interview_service.propose_panel_slots(
        session, hr_user, stage_id=stage.id, panelist_user_ids=[panelist.id],
        from_day=date(2026, 9, 7), days=1,
    )
    await session.commit()

    assert result["all_calendars_read"] is True
    starts = [datetime.fromisoformat(s["start_utc"]) for s in result["slots"]]
    assert starts
    for start in starts:
        busy_end = busy_start + timedelta(hours=2)
        assert not (start < busy_end and busy_start < start + timedelta(hours=1))


async def test_a_calendar_that_could_not_be_read_is_flagged_not_assumed_free(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    stage = await _stage(session, tenant)
    panelist = await _panelist_user(session, tenant, "bob@acme.test")
    await _connect(session, tenant, panelist)
    calendar_registry.register_adapter(
        CalendarProvider.GOOGLE, ScriptedCalendar(read_fails=True)
    )

    result = await interview_service.propose_panel_slots(
        session, hr_user, stage_id=stage.id, panelist_user_ids=[panelist.id],
        from_day=date(2026, 9, 7), days=1,
    )
    await session.commit()

    assert result["all_calendars_read"] is False
    assert any("consent revoked" in c for c in result["caveats"])
    assert all(not s["fully_verified"] for s in result["slots"])


async def test_a_panelist_with_no_calendar_connected_is_named(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    stage = await _stage(session, tenant)
    panelist = await _panelist_user(session, tenant, "nocal@acme.test")
    calendar_registry.register_adapter(CalendarProvider.GOOGLE, ScriptedCalendar())

    result = await interview_service.propose_panel_slots(
        session, hr_user, stage_id=stage.id, panelist_user_ids=[panelist.id],
        from_day=date(2026, 9, 7), days=1,
    )
    assert any("no calendar connected" in c for c in result["caveats"])
    assert any("nocal@acme.test" in c for c in result["caveats"])


# ═══════════════════════════════════════════════════════════════════════════
# Scheduling and rescheduling
# ═══════════════════════════════════════════════════════════════════════════


async def _scheduled(session, tenant, hr_user, *, adapter=None, timezone="Asia/Kolkata"):
    stage = await _stage(session, tenant)
    candidate = await _candidate(session, tenant, stage)
    panelist = await _panelist_user(
        session, tenant, f"panel{datetime.now(UTC).timestamp()}@acme.test"
    )
    await _connect(session, tenant, hr_user)
    calendar = adapter or ScriptedCalendar()
    calendar_registry.register_adapter(CalendarProvider.GOOGLE, calendar)

    interview = await interview_service.schedule_interview(
        session, hr_user,
        candidate_id=candidate.id, stage_id=stage.id,
        start=datetime(2026, 9, 8, 15, 0),
        panelist_user_ids=[panelist.id],
        candidate_timezone=timezone,
    )
    await session.commit()
    await session.refresh(interview)
    return interview, stage, candidate, panelist, calendar


async def test_scheduling_stores_utc_and_the_candidates_own_zone(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """3pm in Delhi is 09:30 UTC. Storing the wall clock would be an interview at
    the wrong hour for everyone not in Delhi."""
    interview, _, _, _, calendar = await _scheduled(session, tenant, hr_user)
    assert interview.scheduled_start == datetime(2026, 9, 8, 9, 30, tzinfo=UTC)
    assert interview.candidate_timezone == "Asia/Kolkata"
    assert interview.external_event_id == "evt-1"
    assert interview.join_url == "https://meet.test/evt-1"
    assert len(calendar.created) == 1


async def test_a_reschedule_moves_the_event_rather_than_creating_a_second(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """The checklist gate: reschedule never leaves an orphan calendar event.

    An orphan is an interviewer who turns up at the old time and a candidate who
    joins a call with nobody in it.
    """
    interview, _, _, _, calendar = await _scheduled(session, tenant, hr_user)
    original_event_id = interview.external_event_id

    result = await interview_service.reschedule_interview(
        session, hr_user, interview_id=interview.id,
        start=datetime(2026, 9, 9, 11, 0),
        reason="The interviewer has a conflict.",
    )
    await session.commit()
    await session.refresh(interview)

    assert calendar.updated == [original_event_id]
    assert len(calendar.created) == 1, "no second event was created"
    assert calendar.cancelled == [], "and the first was not deleted"
    assert interview.external_event_id == original_event_id
    assert result["event_was_moved_not_duplicated"] is True
    assert interview.reschedule_count == 1
    assert interview.scheduled_start == datetime(2026, 9, 9, 5, 30, tzinfo=UTC)


async def test_a_reschedule_resets_panel_responses(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """An acceptance was for the old time."""
    interview, _, _, panelist, _ = await _scheduled(session, tenant, hr_user)
    await interview_service.record_response(
        session, hr_user, interview_id=interview.id,
        panelist_user_id=panelist.id, response=ResponseStatus.ACCEPTED, now=NOW,
    )
    await session.commit()

    await interview_service.reschedule_interview(
        session, hr_user, interview_id=interview.id,
        start=datetime(2026, 9, 9, 11, 0), reason="Moved.",
    )
    await session.commit()

    row = await session.scalar(
        select(InterviewPanelist).where(
            InterviewPanelist.interview_id == interview.id,
            InterviewPanelist.user_id == panelist.id,
        )
    )
    assert row.response_status is ResponseStatus.NEEDS_ACTION
    assert row.responded_at is None


async def test_a_reschedule_needs_a_reason(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """Repeated reschedules are a real signal, and it only exists if somebody
    wrote it down."""
    interview, _, _, _, _ = await _scheduled(session, tenant, hr_user)
    with pytest.raises(HTTPException) as exc:
        await interview_service.reschedule_interview(
            session, hr_user, interview_id=interview.id,
            start=datetime(2026, 9, 9, 11, 0), reason="   ",
        )
    assert "needs a reason" in exc.value.detail


async def test_a_completed_interview_cannot_be_rescheduled(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    interview, _, _, _, _ = await _scheduled(session, tenant, hr_user)
    interview.status = InterviewStatus.COMPLETED
    await session.commit()
    with pytest.raises(HTTPException) as exc:
        await interview_service.reschedule_interview(
            session, hr_user, interview_id=interview.id,
            start=datetime(2026, 9, 9, 11, 0), reason="Moving.",
        )
    assert "already happened" in exc.value.detail


async def test_a_calendar_failure_is_recorded_on_the_interview_not_swallowed(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """The interview exists here and is in nobody's calendar. Somebody has to be
    told that, or everyone assumes the invitation went out."""
    calendar = ScriptedCalendar(raises=CalendarPermanentFailure("consent revoked"))
    interview, _, _, _, _ = await _scheduled(session, tenant, hr_user, adapter=calendar)
    assert interview.external_event_id is None
    assert interview.calendar_warnings
    assert "could not be sent" in interview.calendar_warnings[0]
    assert "tell them by hand" in interview.calendar_warnings[0]


async def test_cancelling_removes_the_calendar_event(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    interview, _, _, _, calendar = await _scheduled(session, tenant, hr_user)
    await interview_service.cancel_interview(
        session, hr_user, interview_id=interview.id, reason="Role withdrawn."
    )
    await session.commit()
    assert calendar.cancelled == ["evt-1"]
    await session.refresh(interview)
    assert interview.status is InterviewStatus.CANCELLED


# ═══════════════════════════════════════════════════════════════════════════
# Panel responses
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_decline_is_recorded_as_having_reached_the_recruiter(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """The checklist gate: a panelist decline surfaces immediately, not at
    interview time."""
    interview, _, _, panelist, _ = await _scheduled(session, tenant, hr_user)

    result = await interview_service.record_response(
        session, hr_user, interview_id=interview.id,
        panelist_user_id=panelist.id, response=ResponseStatus.DECLINED, now=NOW,
    )
    await session.commit()

    assert result["recruiter_notified"] is True
    row = await session.scalar(
        select(InterviewPanelist).where(
            InterviewPanelist.interview_id == interview.id,
            InterviewPanelist.user_id == panelist.id,
        )
    )
    assert row.response_status is ResponseStatus.DECLINED
    assert row.decline_notified_at == NOW


async def test_an_acceptance_does_not_notify_anybody(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    interview, _, _, panelist, _ = await _scheduled(session, tenant, hr_user)
    result = await interview_service.record_response(
        session, hr_user, interview_id=interview.id,
        panelist_user_id=panelist.id, response=ResponseStatus.ACCEPTED, now=NOW,
    )
    assert result["recruiter_notified"] is False


async def test_somebody_not_on_the_panel_cannot_respond(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    interview, _, _, _, _ = await _scheduled(session, tenant, hr_user)
    outsider = await _panelist_user(session, tenant, "outsider@acme.test")
    with pytest.raises(HTTPException) as exc:
        await interview_service.record_response(
            session, hr_user, interview_id=interview.id,
            panelist_user_id=outsider.id, response=ResponseStatus.DECLINED,
        )
    assert exc.value.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# Feedback, anchoring and the advance gate
# ═══════════════════════════════════════════════════════════════════════════


async def _two_panel(session, tenant, hr_user):
    stage = await _stage(session, tenant)
    candidate = await _candidate(session, tenant, stage)
    alice = await _panelist_user(session, tenant, "alice.f@acme.test")
    bob = await _panelist_user(session, tenant, "bob.f@acme.test")
    await _connect(session, tenant, hr_user)
    calendar_registry.register_adapter(CalendarProvider.GOOGLE, ScriptedCalendar())

    interview = await interview_service.schedule_interview(
        session, hr_user, candidate_id=candidate.id, stage_id=stage.id,
        start=datetime(2026, 9, 8, 15, 0), panelist_user_ids=[alice.id, bob.id],
    )
    interview.status = InterviewStatus.COMPLETED
    await session.commit()
    return interview, stage, candidate, alice, bob


async def test_advance_is_blocked_while_feedback_is_missing(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """The checklist gate: enforced server-side.

    A check that lives only in the screen is not a check, because the API is
    reachable without it.
    """
    interview, _, _, alice, _ = await _two_panel(session, tenant, hr_user)

    blockers = await interview_service.advance_blockers(
        session, hr_user, interview_id=interview.id
    )
    assert blockers
    assert "alice.f@acme.test" in blockers[0]
    assert "bob.f@acme.test" in blockers[0]
    assert "impossible to defend" in blockers[0]

    await interview_service.submit_feedback(
        session, alice, interview_id=interview.id,
        recommendation=Recommendation.YES, ratings={"comm": "8"},
    )
    await session.commit()

    blockers = await interview_service.advance_blockers(
        session, hr_user, interview_id=interview.id
    )
    assert len(blockers) == 1
    assert "bob.f@acme.test" in blockers[0]
    assert "alice.f@acme.test" not in blockers[0]


async def test_advance_clears_once_everybody_has_submitted(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    interview, _, _, alice, bob = await _two_panel(session, tenant, hr_user)
    for person in (alice, bob):
        await interview_service.submit_feedback(
            session, person, interview_id=interview.id,
            recommendation=Recommendation.YES, ratings={"comm": "8"},
        )
        await session.commit()
    assert await interview_service.advance_blockers(
        session, hr_user, interview_id=interview.id
    ) == []


async def test_an_interview_that_has_not_happened_blocks_advance(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    interview, _, _, _, _ = await _two_panel(session, tenant, hr_user)
    interview.status = InterviewStatus.SCHEDULED
    await session.commit()
    blockers = await interview_service.advance_blockers(
        session, hr_user, interview_id=interview.id
    )
    assert any("still scheduled" in b for b in blockers)


async def test_an_observer_does_not_block_advance(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    interview, _, _, alice, bob = await _two_panel(session, tenant, hr_user)
    row = await session.scalar(
        select(InterviewPanelist).where(
            InterviewPanelist.interview_id == interview.id,
            InterviewPanelist.user_id == bob.id,
        )
    )
    row.role = PanelistRole.OBSERVER
    await session.commit()

    await interview_service.submit_feedback(
        session, alice, interview_id=interview.id,
        recommendation=Recommendation.YES, ratings={"comm": "8"},
    )
    await session.commit()
    assert await interview_service.advance_blockers(
        session, hr_user, interview_id=interview.id
    ) == []


async def test_peer_feedback_is_hidden_until_you_submit_your_own(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """The checklist gate: anchoring protection verified."""
    interview, _, _, alice, bob = await _two_panel(session, tenant, hr_user)
    await interview_service.submit_feedback(
        session, alice, interview_id=interview.id,
        recommendation=Recommendation.STRONG_YES, ratings={"comm": "10"},
        strengths="Outstanding across the board.",
    )
    await session.commit()

    before = await interview_service.visible_feedback(
        session, bob, interview_id=interview.id
    )
    assert before["feedback"] == []
    assert before["withheld_count"] == 1
    assert "changes what you write" in before["withheld_note"]
    assert "Outstanding across the board" not in str(before)

    await interview_service.submit_feedback(
        session, bob, interview_id=interview.id,
        recommendation=Recommendation.NO, ratings={"comm": "4"},
        concerns="Could not explain their own design decisions.",
    )
    await session.commit()

    after = await interview_service.visible_feedback(
        session, bob, interview_id=interview.id
    )
    assert len(after["feedback"]) == 2
    assert after["withheld_count"] == 0


async def test_a_tenant_can_switch_anchoring_protection_off(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    interview, _, _, alice, bob = await _two_panel(session, tenant, hr_user)
    await interview_service.submit_feedback(
        session, alice, interview_id=interview.id,
        recommendation=Recommendation.YES, ratings={"comm": "8"},
    )
    await session.commit()

    visible = await interview_service.visible_feedback(
        session, bob, interview_id=interview.id, hide_until_submitted=False
    )
    assert len(visible["feedback"]) == 1


async def test_feedback_cannot_be_submitted_twice(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """The first is what you thought before you knew what anyone else said."""
    interview, _, _, alice, _ = await _two_panel(session, tenant, hr_user)
    await interview_service.submit_feedback(
        session, alice, interview_id=interview.id,
        recommendation=Recommendation.YES, ratings={"comm": "8"},
    )
    await session.commit()
    with pytest.raises(HTTPException) as exc:
        await interview_service.submit_feedback(
            session, alice, interview_id=interview.id,
            recommendation=Recommendation.NO, ratings={"comm": "3"},
            concerns="Changed my mind.",
        )
    assert exc.value.status_code == 409

    count = await session.scalar(
        select(func.count()).select_from(InterviewFeedback).where(
            InterviewFeedback.interview_id == interview.id
        )
    )
    assert count == 1


async def test_a_partial_feedback_form_is_refused(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    interview, _, _, alice, _ = await _two_panel(session, tenant, hr_user)
    with pytest.raises(HTTPException) as exc:
        await interview_service.submit_feedback(
            session, alice, interview_id=interview.id,
            recommendation=Recommendation.YES, ratings={},
        )
    assert "not rated" in exc.value.detail


async def test_a_negative_recommendation_without_a_concern_is_refused(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    interview, _, _, alice, _ = await _two_panel(session, tenant, hr_user)
    with pytest.raises(HTTPException) as exc:
        await interview_service.submit_feedback(
            session, alice, interview_id=interview.id,
            recommendation=Recommendation.NO, ratings={"comm": "3"}, concerns="",
        )
    assert "cannot be explained" in exc.value.detail


async def test_somebody_not_on_the_panel_cannot_submit_feedback(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    interview, _, _, _, _ = await _two_panel(session, tenant, hr_user)
    outsider = await _panelist_user(session, tenant, "outsider2@acme.test")
    with pytest.raises(HTTPException) as exc:
        await interview_service.submit_feedback(
            session, outsider, interview_id=interview.id,
            recommendation=Recommendation.YES, ratings={"comm": "8"},
        )
    assert exc.value.status_code == 403


async def test_the_consolidated_view_keeps_the_disagreement_and_the_ai_score_apart(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    interview, _, candidate, alice, bob = await _two_panel(session, tenant, hr_user)
    await interview_service.submit_feedback(
        session, alice, interview_id=interview.id,
        recommendation=Recommendation.STRONG_YES, ratings={"comm": "9"},
    )
    await session.commit()
    await interview_service.submit_feedback(
        session, bob, interview_id=interview.id,
        recommendation=Recommendation.STRONG_NO, ratings={"comm": "2"},
        concerns="Fabricated a project.",
    )
    await session.commit()

    view = await interview_service.consolidated_view(
        session, hr_user, interview_id=interview.id
    )
    assert view["strong_dissent"] is True
    assert "worth a conversation" in view["headline"]
    assert view["per_competency"]["comm"]["spread"] == "7"
    # The AI score sits apart from the panel rather than blended into it — and a
    # stored Fit Score with no round scores and signals behind it is withheld, not
    # shown bare (no AI score without its contributing signals, FS-ANL-02).
    assert view["fit_score"]["status"] == "withheld"
    assert view["fit_score"]["value"] is None
    assert view["fit_score"]["confidence"] is None
    assert view["ai_rounds"] == []
    assert view["complete"] is True


# ═══════════════════════════════════════════════════════════════════════════
# Self-scheduling
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_candidate_books_the_right_instant_from_another_timezone(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """The checklist gate: correct across two timezones.

    The recruiter approved 3pm IST. A candidate in New York picking "5:30am my
    time" is picking the same instant, and both sides must end up with it.
    """
    stage = await _stage(session, tenant)
    candidate = await _candidate(session, tenant, stage)
    panelist = await _panelist_user(session, tenant, "tz@acme.test")
    await _connect(session, tenant, hr_user)
    calendar_registry.register_adapter(CalendarProvider.GOOGLE, ScriptedCalendar())

    approved = datetime(2026, 9, 8, 9, 30, tzinfo=UTC)  # 15:00 IST
    link, token = await interview_service.create_self_schedule_link(
        session, hr_user,
        candidate_id=candidate.id, stage_id=stage.id,
        slots=[{
            "start": approved.isoformat(),
            "end": (approved + timedelta(hours=1)).isoformat(),
            "panelist_user_ids": [str(panelist.id)],
        }],
        candidate_timezone="America/New_York",
        now=NOW,
    )
    await session.commit()

    # 09:30 UTC is 05:30 in New York on that date.
    result = await interview_service.book_self_schedule(
        session, token=token,
        slot_start=datetime(2026, 9, 8, 5, 30),
        candidate_timezone="America/New_York",
        now=NOW,
    )
    await session.commit()

    interview = await session.get(Interview, result["interview_id"])
    assert interview.scheduled_start == approved
    assert result["timezone"] == "America/New_York"
    assert datetime.fromisoformat(result["start_local"]).hour == 5
    assert interview.candidate_timezone == "America/New_York"
    # And the recruiter's own view of the same interview is 3pm IST.
    from app.services.interviews.availability import render_for

    assert render_for(interview.scheduled_start, "Asia/Kolkata").hour == 15


async def test_a_time_that_was_not_offered_cannot_be_booked(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """Otherwise the link books any hour of any day."""
    stage = await _stage(session, tenant)
    candidate = await _candidate(session, tenant, stage)
    panelist = await _panelist_user(session, tenant, "tz2@acme.test")
    approved = datetime(2026, 9, 8, 9, 30, tzinfo=UTC)
    _, token = await interview_service.create_self_schedule_link(
        session, hr_user, candidate_id=candidate.id, stage_id=stage.id,
        slots=[{"start": approved.isoformat(),
                "end": (approved + timedelta(hours=1)).isoformat(),
                "panelist_user_ids": [str(panelist.id)]}],
        now=NOW,
    )
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await interview_service.book_self_schedule(
            session, token=token, slot_start=datetime(2026, 9, 8, 3, 0),
            candidate_timezone="Asia/Kolkata", now=NOW,
        )
    assert "not one of the offered slots" in exc.value.detail


async def test_a_self_schedule_link_books_only_once(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    stage = await _stage(session, tenant)
    candidate = await _candidate(session, tenant, stage)
    panelist = await _panelist_user(session, tenant, "tz3@acme.test")
    await _connect(session, tenant, hr_user)
    calendar_registry.register_adapter(CalendarProvider.GOOGLE, ScriptedCalendar())

    approved = datetime(2026, 9, 8, 9, 30, tzinfo=UTC)
    _, token = await interview_service.create_self_schedule_link(
        session, hr_user, candidate_id=candidate.id, stage_id=stage.id,
        slots=[{"start": approved.isoformat(),
                "end": (approved + timedelta(hours=1)).isoformat(),
                "panelist_user_ids": [str(panelist.id)]}],
        now=NOW,
    )
    await session.commit()
    await interview_service.book_self_schedule(
        session, token=token, slot_start=approved, now=NOW
    )
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await interview_service.book_self_schedule(
            session, token=token, slot_start=approved, now=NOW
        )
    assert "already been used" in exc.value.detail


async def test_an_expired_self_schedule_link_says_the_calendars_moved_on(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    stage = await _stage(session, tenant)
    candidate = await _candidate(session, tenant, stage)
    # Every offered slot needs an interviewer; this test is about expiry, not panels.
    panelist = await _panelist_user(session, tenant, "tz4@acme.test")
    approved = datetime(2026, 9, 8, 9, 30, tzinfo=UTC)
    _, token = await interview_service.create_self_schedule_link(
        session, hr_user, candidate_id=candidate.id, stage_id=stage.id,
        slots=[{"start": approved.isoformat(),
                "end": (approved + timedelta(hours=1)).isoformat(),
                "panelist_user_ids": [str(panelist.id)]}],
        now=NOW,
    )
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await interview_service.book_self_schedule(
            session, token=token, slot_start=approved,
            now=NOW + timedelta(days=interview_service.SELF_SCHEDULE_TTL_DAYS + 1),
        )
    assert exc.value.status_code == 410
    assert "moved on" in exc.value.detail


async def test_a_link_with_no_slots_is_refused(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """The candidate opens it and finds an empty page."""
    stage = await _stage(session, tenant)
    candidate = await _candidate(session, tenant, stage)
    with pytest.raises(HTTPException) as exc:
        await interview_service.create_self_schedule_link(
            session, hr_user, candidate_id=candidate.id, stage_id=stage.id, slots=[]
        )
    assert "empty page" in exc.value.detail


async def test_interviews_are_tenant_isolated(
    session: AsyncSession, tenant: Tenant, hr_user: User, other_hr_user: User
) -> None:
    interview, _, _, _, _ = await _scheduled(session, tenant, hr_user)
    with pytest.raises(HTTPException) as exc:
        await interview_service.consolidated_view(
            session, other_hr_user, interview_id=interview.id
        )
    assert exc.value.status_code == 404
