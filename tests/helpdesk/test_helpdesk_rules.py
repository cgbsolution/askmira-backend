"""The helpdesk's pure rules: numbering, the SLA clock, moves and reopen windows."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models.helpdesk import HelpdeskTicketStatus as S
from app.services.helpdesk import rules
from app.services.helpdesk.service import safe_file_name

RAISED = datetime(2026, 9, 25, 18, 0, tzinfo=UTC)


# ── Ticket numbers ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("seq", "expected"),
    [(1, "HD-00001"), (42, "HD-00042"), (99_999, "HD-99999"), (100_000, "HD-100000")],
)
def test_ticket_numbers_are_zero_padded_and_grow_rather_than_wrap(seq, expected) -> None:
    assert rules.format_ticket_number(seq) == expected


def test_a_sequence_starts_at_one() -> None:
    with pytest.raises(ValueError):
        rules.format_ticket_number(0)


# ── SLA ─────────────────────────────────────────────────────────────────────


def test_due_is_calendar_hours_so_a_friday_evening_ticket_is_due_on_saturday() -> None:
    friday_6pm = datetime(2026, 9, 25, 12, 30, tzinfo=UTC)  # 18:00 IST, a Friday
    due = rules.compute_due_at(friday_6pm, 24)
    assert due == datetime(2026, 9, 26, 12, 30, tzinfo=UTC)
    assert rules.compute_due_at(friday_6pm, 72) - friday_6pm == timedelta(hours=72)


def test_due_needs_an_aware_time_and_a_positive_sla() -> None:
    with pytest.raises(ValueError):
        rules.compute_due_at(datetime(2026, 9, 25, 18, 0), 24)
    with pytest.raises(ValueError):
        rules.compute_due_at(RAISED, 0)


def _state(status=S.OPEN, *, sla=24, resolved_at=None, after=timedelta(hours=1)):
    due = rules.compute_due_at(RAISED, sla)
    return rules.sla_state(
        status=status, due_at=due, sla_hours=sla, resolved_at=resolved_at, now=RAISED + after
    )


def test_sla_state_while_the_ticket_is_being_worked() -> None:
    assert _state(after=timedelta(hours=1)) == "on_track"
    # Inside the last four hours of a 24-hour SLA.
    assert _state(after=timedelta(hours=21)) == "due_soon"
    assert _state(after=timedelta(hours=25)) == "overdue"
    # Waiting on the employee does not stop the clock.
    assert _state(S.WAITING_ON_EMPLOYEE, after=timedelta(hours=25)) == "overdue"


def test_due_soon_is_a_quarter_of_a_short_sla_not_the_whole_of_it() -> None:
    # A 4-hour SLA is due soon in its last hour, not from the moment it is raised.
    assert _state(sla=4, after=timedelta(minutes=30)) == "on_track"
    assert _state(sla=4, after=timedelta(hours=3, minutes=30)) == "due_soon"


def test_a_resolved_ticket_either_met_or_breached_its_sla() -> None:
    assert _state(S.RESOLVED, resolved_at=RAISED + timedelta(hours=5)) == "met"
    assert (
        _state(S.CLOSED, resolved_at=RAISED + timedelta(hours=30), after=timedelta(days=9))
        == "breached"
    )
    assert _state(S.CANCELLED, after=timedelta(days=3)) == "none"


# ── Moves ───────────────────────────────────────────────────────────────────


def test_what_an_agent_may_move_a_ticket_to() -> None:
    assert rules.agent_may_move(S.OPEN, S.RESOLVED)
    assert rules.agent_may_move(S.WAITING_ON_EMPLOYEE, S.IN_PROGRESS)
    assert rules.agent_may_move(S.RESOLVED, S.IN_PROGRESS)
    assert rules.agent_may_move(S.RESOLVED, S.CLOSED)
    assert not rules.agent_may_move(S.RESOLVED, S.WAITING_ON_EMPLOYEE)
    assert not rules.agent_may_move(S.OPEN, S.CANCELLED)  # only the employee cancels
    for terminal in (S.CLOSED, S.CANCELLED):
        assert not any(rules.agent_may_move(terminal, target) for target in S)


def test_reopen_is_allowed_for_seven_days_after_resolution_and_not_after_a_rating() -> None:
    resolved = RAISED
    assert rules.can_reopen(
        status=S.RESOLVED, resolved_at=resolved, rated_at=None, now=resolved + timedelta(days=7)
    )
    assert not rules.can_reopen(
        status=S.RESOLVED,
        resolved_at=resolved,
        rated_at=None,
        now=resolved + timedelta(days=7, seconds=1),
    )
    assert not rules.can_reopen(
        status=S.CLOSED,
        resolved_at=resolved,
        rated_at=resolved + timedelta(hours=1),
        now=resolved + timedelta(days=1),
    )
    # Closed by an agent without ever being resolved: nothing to reopen.
    assert not rules.can_reopen(
        status=S.CLOSED, resolved_at=None, rated_at=None, now=resolved + timedelta(hours=1)
    )
    assert not rules.can_reopen(
        status=S.IN_PROGRESS, resolved_at=None, rated_at=None, now=resolved
    )


# ── Standard categories and file names ─────────────────────────────────────


def test_the_standard_set_has_one_confidential_grievance_category() -> None:
    names = [c.name for c in rules.STANDARD_CATEGORIES]
    assert len(names) == len({n.lower() for n in names})
    confidential = [c for c in rules.STANDARD_CATEGORIES if c.is_confidential]
    assert [c.name for c in confidential] == ["Grievance (confidential)"]
    assert all(c.sla_hours > 0 for c in rules.STANDARD_CATEGORIES)


def test_a_client_filename_never_becomes_a_path_or_keeps_a_lying_extension() -> None:
    assert safe_file_name("../../etc/passwd", ".pdf") == "passwd.pdf"
    assert safe_file_name("C:\\Users\\me\\salary slip.exe", ".png") == "salary slip.png"
    assert safe_file_name(None, ".jpg") == "attachment.jpg"
    assert safe_file_name("<script>.pdf", ".pdf") == "script.pdf"
