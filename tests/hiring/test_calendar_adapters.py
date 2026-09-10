"""Calendar adapters: a failed read is not a free calendar, and a move is a move."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services.calendar.base import (
    CalendarAdapter,
    CalendarPermanentFailure,
    CalendarProvider,
    CalendarTransientFailure,
    EventSpec,
)
from app.services.calendar.google import GoogleCalendarAdapter
from app.services.calendar.microsoft import (
    BLOCKING_CODES,
    INTERVAL_MINUTES,
    MicrosoftCalendarAdapter,
)

START = datetime(2026, 9, 7, 4, 0, tzinfo=UTC)
END = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


class FakeTransport:
    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.calls: list[tuple] = []

    async def request(self, method, path, *, json=None, headers=None):
        self.calls.append((method, path, json, headers))
        return self.responses.pop(0) if self.responses else (200, {})


def _spec(**overrides) -> EventSpec:
    base = {
        "summary": "Technical round - Rohit Verma",
        "description": "<p>Interview</p>",
        "start": START,
        "end": START + timedelta(hours=1),
        "attendees": ("alice@acme.test", "rohit@example.test"),
        "correlation_id": "interview-1",
    }
    base.update(overrides)
    return EventSpec(**base)


@pytest.mark.parametrize(
    "adapter_class", [GoogleCalendarAdapter, MicrosoftCalendarAdapter]
)
def test_every_adapter_implements_the_interface(adapter_class) -> None:
    adapter = adapter_class(FakeTransport())
    assert isinstance(adapter, CalendarAdapter)
    assert isinstance(adapter.provider, CalendarProvider)
    for method in (
        "free_busy", "create_event", "update_event", "cancel_event", "responses"
    ):
        assert callable(getattr(adapter, method)), method


# ═══════════════════════════════════════════════════════════════════════════
# Google
# ═══════════════════════════════════════════════════════════════════════════


async def test_google_returns_busy_blocks_for_a_readable_calendar() -> None:
    transport = FakeTransport(
        (200, {"calendars": {"alice@acme.test": {"busy": [
            {"start": "2026-09-07T05:00:00Z", "end": "2026-09-07T06:00:00Z"}
        ]}}}),
    )
    [result] = await GoogleCalendarAdapter(transport).free_busy(
        ["alice@acme.test"], start=START, end=END, token="t"
    )
    assert result.ok
    assert result.busy == ((
        datetime(2026, 9, 7, 5, tzinfo=UTC), datetime(2026, 9, 7, 6, tzinfo=UTC)
    ),)


async def test_a_per_calendar_error_inside_a_200_is_not_read_as_free() -> None:
    """Google reports a calendar it could not see as `errors` alongside the ones
    that worked, and the outer request succeeded. Reading `busy` and moving on
    turns "I could not look" into "nothing booked"."""
    transport = FakeTransport(
        (200, {"calendars": {"bob@acme.test": {"errors": [{"reason": "notFound"}]}}}),
    )
    [result] = await GoogleCalendarAdapter(transport).free_busy(
        ["bob@acme.test"], start=START, end=END, token="t"
    )
    assert not result.ok
    assert "notFound" in result.error
    assert result.busy == ()


async def test_a_calendar_google_did_not_answer_about_is_not_read_as_free() -> None:
    transport = FakeTransport((200, {"calendars": {}}))
    [result] = await GoogleCalendarAdapter(transport).free_busy(
        ["ghost@acme.test"], start=START, end=END, token="t"
    )
    assert not result.ok


async def test_google_asks_for_a_conference_link_the_way_that_actually_works() -> None:
    """Omit `conferenceDataVersion=1` and the request is ignored without
    complaining — an interview whose instructions say "video call" and carry no
    way to join."""
    transport = FakeTransport(
        (200, {"id": "evt-1", "conferenceData": {"entryPoints": [
            {"entryPointType": "video", "uri": "https://meet.test/abc"}
        ]}}),
    )
    result = await GoogleCalendarAdapter(transport).create_event(_spec(), token="t")
    _, path, body, _ = transport.calls[0]
    assert "conferenceDataVersion=1" in path
    assert body["conferenceData"]["createRequest"]["requestId"]
    assert result.join_url == "https://meet.test/abc"


async def test_an_event_created_without_the_requested_link_warns_about_it() -> None:
    transport = FakeTransport((200, {"id": "evt-1"}))
    result = await GoogleCalendarAdapter(transport).create_event(_spec(), token="t")
    assert any("no way to join" in w for w in result.warnings)


async def test_our_own_id_is_written_into_the_event() -> None:
    """So the event can be found again if the stored id is ever lost — which is
    how orphans happen."""
    transport = FakeTransport((200, {"id": "evt-1"}))
    await GoogleCalendarAdapter(transport).create_event(_spec(), token="t")
    _, _, body, _ = transport.calls[0]
    assert body["extendedProperties"]["private"]["askmira_interview_id"] == "interview-1"


async def test_google_moves_an_event_with_patch_not_a_second_create() -> None:
    """The checklist gate: reschedule never leaves an orphan calendar event."""
    transport = FakeTransport((200, {"id": "evt-1"}))
    result = await GoogleCalendarAdapter(transport).update_event(
        "evt-1", _spec(start=START + timedelta(hours=2)), token="t"
    )
    method, path, _, _ = transport.calls[0]
    assert method == "PATCH"
    assert "evt-1" in path
    assert result.external_event_id == "evt-1"


async def test_an_event_accepted_with_no_id_is_a_failure() -> None:
    """Nothing could move or cancel it later."""
    transport = FakeTransport((200, {}))
    with pytest.raises(CalendarPermanentFailure, match="returned no id"):
        await GoogleCalendarAdapter(transport).create_event(_spec(), token="t")


async def test_cancelling_an_already_deleted_event_is_not_an_error() -> None:
    """The desired state is "no event", and it is met."""
    transport = FakeTransport((410, {"error": {"message": "deleted"}}))
    await GoogleCalendarAdapter(transport).cancel_event("evt-1", token="t")


async def test_revoked_consent_is_permanent_and_says_what_to_do() -> None:
    transport = FakeTransport((401, {"error": {"message": "invalid credentials"}}))
    with pytest.raises(CalendarPermanentFailure, match="reconnect"):
        await GoogleCalendarAdapter(transport).create_event(_spec(), token="t")


async def test_a_throttle_is_transient() -> None:
    transport = FakeTransport((429, {"error": {"message": "rate limit"}}))
    with pytest.raises(CalendarTransientFailure):
        await GoogleCalendarAdapter(transport).create_event(_spec(), token="t")


# ═══════════════════════════════════════════════════════════════════════════
# Microsoft
# ═══════════════════════════════════════════════════════════════════════════


def test_the_availability_string_decodes_into_merged_busy_intervals() -> None:
    """Graph returns one character per interval. Four adjacent busy characters
    are one two-hour meeting, not four blocks — and the availability engine pads
    each block, so four would pad the middle of one meeting three times."""
    busy = MicrosoftCalendarAdapter.decode_availability("002200", start=START)
    assert busy == ((
        START + timedelta(minutes=2 * INTERVAL_MINUTES),
        START + timedelta(minutes=4 * INTERVAL_MINUTES),
    ),)


def test_tentative_counts_as_busy_and_working_elsewhere_does_not() -> None:
    """A tentatively-accepted meeting is one somebody expects to attend.
    "Working elsewhere" is a different desk, not an unavailable person."""
    assert "1" in BLOCKING_CODES
    assert "2" in BLOCKING_CODES
    assert "3" in BLOCKING_CODES
    assert "4" not in BLOCKING_CODES

    assert MicrosoftCalendarAdapter.decode_availability("4444", start=START) == ()
    assert len(MicrosoftCalendarAdapter.decode_availability("1111", start=START)) == 1


def test_a_busy_run_that_reaches_the_end_of_the_window_still_closes() -> None:
    busy = MicrosoftCalendarAdapter.decode_availability("0022", start=START)
    assert busy[0][1] == START + timedelta(minutes=4 * INTERVAL_MINUTES)


async def test_a_per_mailbox_error_inside_a_200_is_not_read_as_free() -> None:
    transport = FakeTransport(
        (200, {"value": [{"scheduleId": "bob@acme.test",
                          "error": {"message": "mailbox not found"}}]}),
    )
    [result] = await MicrosoftCalendarAdapter(transport).free_busy(
        ["bob@acme.test"], start=START, end=END, token="t"
    )
    assert not result.ok
    assert "mailbox not found" in result.error


async def test_a_mailbox_graph_did_not_answer_about_is_not_read_as_free() -> None:
    transport = FakeTransport((200, {"value": []}))
    [result] = await MicrosoftCalendarAdapter(transport).free_busy(
        ["ghost@acme.test"], start=START, end=END, token="t"
    )
    assert not result.ok


async def test_microsoft_moves_an_event_with_patch() -> None:
    transport = FakeTransport((200, {"id": "evt-9"}))
    await MicrosoftCalendarAdapter(transport).update_event("evt-9", _spec(), token="t")
    method, path, _, _ = transport.calls[0]
    assert method == "PATCH"
    assert "evt-9" in path


async def test_microsoft_asks_for_a_teams_link_and_warns_when_it_gets_none() -> None:
    transport = FakeTransport((200, {"id": "evt-9"}))
    result = await MicrosoftCalendarAdapter(transport).create_event(_spec(), token="t")
    _, _, body, _ = transport.calls[0]
    assert body["isOnlineMeeting"] is True
    assert any("no way to join" in w for w in result.warnings)


async def test_graph_response_vocabulary_is_normalised_to_ours() -> None:
    """So the service never has to know which provider an interview was booked
    on."""
    transport = FakeTransport(
        (200, {"attendees": [
            {"emailAddress": {"address": "a@acme.test"},
             "status": {"response": "tentativelyAccepted"}},
            {"emailAddress": {"address": "b@acme.test"},
             "status": {"response": "notResponded"}},
            {"emailAddress": {"address": "c@acme.test"},
             "status": {"response": "declined"}},
        ]}),
    )
    responses = await MicrosoftCalendarAdapter(transport).responses("evt-9", token="t")
    assert responses == {
        "a@acme.test": "tentative",
        "b@acme.test": "needsAction",
        "c@acme.test": "declined",
    }
