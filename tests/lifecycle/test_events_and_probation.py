"""Consequences an approver has to be told, and probation that does not drift."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.services.lifecycle.events import (
    CHANGED_FIELDS,
    EventFacts,
    EventType,
    LifecycleError,
    LocationFacts,
    Severity,
    consequences_of,
    describe,
    retrospective_note,
    validate_event,
)
from app.services.lifecycle.probation import (
    ESCALATION_SCHEDULE,
    ProbationError,
    ReminderStep,
    ReviewFacts,
    SilencePolicy,
    due_reminders,
    resolve_silence,
)

TODAY = date(2026, 9, 7)

BENGALURU = LocationFacts(
    location_id="blr", name="Bengaluru", state_code="KA", timezone_name="Asia/Kolkata"
)
MUMBAI = LocationFacts(
    location_id="bom", name="Mumbai", state_code="MH", timezone_name="Asia/Kolkata"
)
WHITEFIELD = LocationFacts(
    location_id="wf", name="Whitefield", state_code="KA", timezone_name="Asia/Kolkata"
)


def _facts(**overrides) -> EventFacts:
    base = {
        "event_type": EventType.TRANSFER,
        "employee_id": "emp-1",
        "effective_from": date(2026, 10, 1),
        "previous_values": {"location_id": "blr"},
        "new_values": {"location_id": "bom"},
        "reason": "Team relocation to the Mumbai office.",
        "previous_location": BENGALURU,
        "new_location": MUMBAI,
    }
    base.update(overrides)
    return EventFacts(**base)


# ═══════════════════════════════════════════════════════════════════════════
# Cross-state consequences — the checklist gate
# ═══════════════════════════════════════════════════════════════════════════


def test_a_cross_state_transfer_surfaces_professional_tax() -> None:
    """The checklist gate.

    PT is a state tax with different slabs, different frequencies and in some
    states none at all. An approver who signs a transfer without knowing this
    changes finds out from payroll — or from the employee.
    """
    consequences = consequences_of(_facts())
    pt = next(c for c in consequences if c.kind == "professional_tax")
    assert pt.severity is Severity.PAYROLL
    assert "KA" in pt.message and "MH" in pt.message
    assert pt.action_required


def test_a_cross_state_transfer_surfaces_the_holiday_calendar() -> None:
    """State holidays differ substantially across India, and leave already
    approved on a day that is no longer a holiday needs looking at."""
    consequences = consequences_of(_facts())
    holidays = next(c for c in consequences if c.kind == "holiday_calendar")
    assert "State holidays differ" in holidays.message
    assert "approved leave" in holidays.action_required


def test_a_within_state_move_does_not_claim_professional_tax_changes() -> None:
    """Bengaluru to Whitefield is a different office and the same state. Flagging
    PT there would train people to ignore the flag."""
    consequences = consequences_of(
        _facts(new_location=WHITEFIELD, new_values={"location_id": "wf"})
    )
    kinds = {c.kind for c in consequences}
    assert "professional_tax" not in kinds
    assert "holiday_calendar" not in kinds
    assert "geofence" in kinds, "the site still changes"


def test_any_location_change_surfaces_the_geofence_and_the_shift() -> None:
    consequences = consequences_of(_facts())
    kinds = {c.kind for c in consequences}
    assert "geofence" in kinds
    shift = next(c for c in consequences if c.kind == "shift")
    assert "attendance exceptions from day one" in shift.message


def test_a_timezone_change_is_surfaced_separately() -> None:
    delhi = LocationFacts(
        location_id="del", name="Dubai", state_code="XX", timezone_name="Asia/Dubai"
    )
    consequences = consequences_of(
        _facts(new_location=delhi, new_values={"location_id": "del"})
    )
    timezone = next(c for c in consequences if c.kind == "timezone")
    assert "Asia/Dubai" in timezone.message


def test_a_manager_change_with_pending_approvals_names_the_count() -> None:
    """Left behind, those requests sit with somebody who no longer has any
    reason to look at them."""
    consequences = consequences_of(
        _facts(
            event_type=EventType.MANAGER_CHANGE,
            previous_values={"manager_id": "old"},
            new_values={"manager_id": "new"},
            previous_location=None,
            new_location=None,
            pending_approvals_held=3,
        )
    )
    pending = next(c for c in consequences if c.kind == "pending_approvals")
    assert "3 pending approval(s)" in pending.message
    assert "no longer has any reason to look at them" in pending.message


def test_a_manager_change_with_nothing_pending_says_only_what_is_true() -> None:
    consequences = consequences_of(
        _facts(
            event_type=EventType.MANAGER_CHANGE,
            previous_values={"manager_id": "old"},
            new_values={"manager_id": "new"},
            previous_location=None, new_location=None,
            pending_approvals_held=0,
        )
    )
    kinds = {c.kind for c in consequences}
    assert "pending_approvals" not in kinds
    assert "approval_routing" in kinds


def test_an_employment_type_change_surfaces_statutory_eligibility() -> None:
    """A contractor moving to full-time starts accruing gratuity from this date,
    not from when they first walked in."""
    consequences = consequences_of(
        _facts(
            event_type=EventType.EMPLOYMENT_TYPE_CHANGE,
            previous_values={"employment_type": "contract"},
            new_values={"employment_type": "full_time"},
            previous_location=None, new_location=None,
        )
    )
    statutory = next(c for c in consequences if c.kind == "statutory_eligibility")
    assert statutory.severity is Severity.PAYROLL
    assert "gratuity" in statutory.message


def test_a_salary_change_is_a_payroll_consequence() -> None:
    consequences = consequences_of(
        _facts(
            event_type=EventType.PROMOTION,
            previous_values={"grade": "L4"},
            new_values={"grade": "L5"},
            new_ctc_annual=Decimal("2500000"),
            previous_location=None, new_location=None,
        )
    )
    salary = next(c for c in consequences if c.kind == "salary_revision")
    assert salary.severity is Severity.PAYROLL
    assert "the letter and the payslip agree" in salary.message


# ═══════════════════════════════════════════════════════════════════════════
# Retrospective events
# ═══════════════════════════════════════════════════════════════════════════


def test_a_backdated_pay_change_flags_arrears() -> None:
    """A promotion agreed in April and processed in June genuinely is effective
    in April. Not refused — flagged, because the approver is approving back pay.
    """
    note = retrospective_note(
        _facts(
            event_type=EventType.PROMOTION,
            effective_from=date(2026, 4, 1),
            previous_values={"grade": "L4"},
            new_values={"grade": "L5"},
            new_ctc_annual=Decimal("2500000"),
        ),
        today=TODAY,
    )
    assert note is not None
    assert note.severity is Severity.PAYROLL
    assert "Arrears will be due" in note.message
    assert "rule versions in force in each affected month" in note.message


def test_a_backdated_change_with_no_pay_says_so_without_mentioning_arrears() -> None:
    note = retrospective_note(_facts(effective_from=date(2026, 8, 1)), today=TODAY)
    assert note is not None
    assert note.severity is Severity.INFORMATIONAL
    assert "Arrears" not in note.message


def test_a_future_dated_event_has_no_retrospective_note() -> None:
    assert retrospective_note(_facts(effective_from=date(2026, 12, 1)), today=TODAY) is None


def test_the_description_flags_future_and_past_dating_separately() -> None:
    future = describe(_facts(effective_from=date(2026, 12, 1)), today=TODAY)
    assert future["is_future_dated"] and not future["is_retrospective"]

    past = describe(_facts(effective_from=date(2026, 6, 1)), today=TODAY)
    assert past["is_retrospective"] and not past["is_future_dated"]


# ═══════════════════════════════════════════════════════════════════════════
# Validation
# ═══════════════════════════════════════════════════════════════════════════


def test_an_event_cannot_change_a_field_its_type_has_no_business_with() -> None:
    """An event that can change anything is a field edit with a nicer name, and
    the point of these is that they are not field edits."""
    with pytest.raises(LifecycleError, match="field edit with a nicer name"):
        validate_event(
            _facts(
                event_type=EventType.REDESIGNATION,
                new_values={"designation": "Staff Engineer", "grade": "L6"},
            ),
            today=TODAY,
        )


def test_every_event_type_declares_what_it_can_change() -> None:
    """A type missing from the table would raise a KeyError at validation, which
    is a worse error than a clear refusal."""
    for event_type in EventType:
        assert event_type in CHANGED_FIELDS, event_type
        assert CHANGED_FIELDS[event_type], event_type


def test_an_event_that_changes_nothing_is_refused() -> None:
    with pytest.raises(LifecycleError, match="changes nothing"):
        validate_event(_facts(new_values={}), today=TODAY)


def test_an_event_whose_values_all_match_the_current_ones_is_refused() -> None:
    with pytest.raises(LifecycleError, match="Nothing would change"):
        validate_event(
            _facts(
                previous_values={"location_id": "blr"},
                new_values={"location_id": "blr"},
            ),
            today=TODAY,
        )


def test_a_confirmation_on_the_planned_date_is_not_a_no_op() -> None:
    """Confirming on the day probation was due to end is the normal case: the
    decision is the change, and it is what the letter records."""
    validate_event(
        _facts(
            event_type=EventType.CONFIRMATION,
            previous_values={"probation_end_date": "2026-09-30"},
            new_values={"probation_end_date": "2026-09-30"},
            reason="Probation completed well.",
        ),
        today=TODAY,
    )


def test_an_event_needs_a_reason() -> None:
    """It goes on the letter, into the employee's own history, and into whatever
    conversation happens if the decision is questioned."""
    with pytest.raises(LifecycleError, match="needs a reason"):
        validate_event(_facts(reason="   "), today=TODAY)


def test_an_event_dated_years_out_is_refused_as_a_probable_typo() -> None:
    """A promotion silently scheduled for 2036 is one nobody chases."""
    with pytest.raises(LifecycleError, match="typo"):
        validate_event(_facts(effective_from=date(2036, 1, 1)), today=TODAY)


def test_a_backdated_event_is_allowed() -> None:
    """The arrears engine exists for exactly this."""
    validate_event(_facts(effective_from=date(2026, 4, 1)), today=TODAY)


def test_a_manager_change_needs_no_letter_and_everything_else_does() -> None:
    """A letter about an internal routing change would puzzle the recipient."""
    assert not EventType.MANAGER_CHANGE.needs_a_letter
    for event_type in EventType:
        if event_type is not EventType.MANAGER_CHANGE:
            assert event_type.needs_a_letter, event_type


def test_a_redesignation_is_not_a_promotion() -> None:
    """Both change the title; only one changes the pay. An HR team that cannot
    tell them apart cannot answer "how many people did we promote"."""
    assert EventType.PROMOTION.affects_pay
    assert not EventType.REDESIGNATION.affects_pay


# ═══════════════════════════════════════════════════════════════════════════
# Probation reminders — the checklist gate
# ═══════════════════════════════════════════════════════════════════════════


def _review(
    review_id: str = "r1",
    *,
    due: date = date(2026, 9, 20),
    outcome: str | None = None,
    sent: frozenset = frozenset(),
    extends: str | None = None,
) -> ReviewFacts:
    return ReviewFacts(
        review_id=review_id,
        employee_id="emp-1",
        employee_label="Anita Sharma",
        probation_end_date=due + timedelta(days=10),
        review_due_date=due,
        outcome=outcome,
        manager_user_id="mgr-1",
        skip_level_user_id="skip-1",
        reminders_sent=sent,
        extends_review_id=extends,
    )


def test_reminders_escalate_to_a_wider_circle_each_time() -> None:
    """The checklist gate: reminders escalate.

    Manager, manager again, skip level, HR. Each step is a wider circle of people
    who could act, and the last is somebody whose job it is to notice.
    """
    steps = [step for _, step in ESCALATION_SCHEDULE]
    assert steps == [
        ReminderStep.MANAGER_FIRST,
        ReminderStep.MANAGER_SECOND,
        ReminderStep.SKIP_LEVEL,
        ReminderStep.HR,
    ]
    thresholds = [days for days, _ in ESCALATION_SCHEDULE]
    assert thresholds == sorted(thresholds, reverse=True), "each step comes later"
    assert thresholds[-1] < 0, "the escalation continues past the deadline"


def test_the_first_reminder_goes_to_the_manager_two_weeks_out() -> None:
    [reminder] = due_reminders([_review(due=date(2026, 9, 20))], today=date(2026, 9, 7))
    assert reminder.step is ReminderStep.MANAGER_FIRST
    assert reminder.recipient_user_id == "mgr-1"
    assert "your decision" in reminder.message


def test_the_skip_level_is_told_on_the_due_date() -> None:
    [reminder] = due_reminders(
        [
            _review(
                due=date(2026, 9, 7),
                sent=frozenset({ReminderStep.MANAGER_FIRST, ReminderStep.MANAGER_SECOND}),
            )
        ],
        today=date(2026, 9, 7),
    )
    assert reminder.step is ReminderStep.SKIP_LEVEL
    assert reminder.recipient_user_id == "skip-1"
    assert "does not know where they stand" in reminder.message


def test_hr_is_told_a_week_after_the_deadline() -> None:
    [reminder] = due_reminders(
        [
            _review(
                due=date(2026, 8, 25),
                sent=frozenset(
                    {
                        ReminderStep.MANAGER_FIRST,
                        ReminderStep.MANAGER_SECOND,
                        ReminderStep.SKIP_LEVEL,
                    }
                ),
            )
        ],
        today=TODAY,
    )
    assert reminder.step is ReminderStep.HR
    assert "surfaces two years later" in reminder.message


def test_a_skip_level_reminder_falls_back_to_the_manager_rather_than_nowhere() -> None:
    review = ReviewFacts(
        review_id="r1", employee_id="e1", employee_label="Someone",
        probation_end_date=date(2026, 9, 17), review_due_date=date(2026, 9, 7),
        manager_user_id="mgr-1", skip_level_user_id=None,
        reminders_sent=frozenset(
            {ReminderStep.MANAGER_FIRST, ReminderStep.MANAGER_SECOND}
        ),
    )
    [reminder] = due_reminders([review], today=TODAY)
    assert reminder.recipient_user_id == "mgr-1"


def test_only_the_furthest_step_is_sent_when_a_sweep_has_been_missed() -> None:
    """A sweep that had not run for a fortnight would otherwise send four
    messages at once about the same person, which is how a reminder becomes
    something people filter."""
    reminders = due_reminders([_review(due=date(2026, 8, 25))], today=TODAY)
    assert len(reminders) == 1
    assert reminders[0].step is ReminderStep.HR


def test_a_reminder_already_sent_is_not_sent_again() -> None:
    review = _review(due=date(2026, 9, 20), sent=frozenset({ReminderStep.MANAGER_FIRST}))
    assert due_reminders([review], today=date(2026, 9, 7)) == []


def test_a_decided_review_gets_no_reminders() -> None:
    assert due_reminders([_review(due=date(2026, 8, 1), outcome="confirm")], today=TODAY) == []


def test_a_review_still_far_off_gets_no_reminders() -> None:
    assert due_reminders([_review(due=date(2026, 12, 1))], today=TODAY) == []


# ═══════════════════════════════════════════════════════════════════════════
# Default on silence — the checklist gate
# ═══════════════════════════════════════════════════════════════════════════


def test_the_silence_policy_has_no_member_meaning_nobody_chose() -> None:
    """The checklist gate: configuration explicit.

    Auto-confirming and requiring a decision lead to opposite outcomes for a real
    person, so there is no default and no "unset".
    """
    values = {policy.value for policy in SilencePolicy}
    assert values == {"auto_confirm", "require_decision", "auto_extend_once"}
    assert not any(v in values for v in ("default", "unset", "none", "unspecified"))


def test_auto_confirm_confirms_and_says_it_happened_without_the_manager() -> None:
    [case] = resolve_silence(
        [_review(due=date(2026, 8, 25))],
        policy=SilencePolicy.AUTO_CONFIRM,
        today=TODAY,
    )
    assert case.auto_outcome == "confirm"
    assert case.days_overdue == 13
    assert "silence as consent" in case.resolution
    assert "without them" in case.resolution


def test_require_decision_changes_nothing_and_keeps_escalating() -> None:
    """Opposite outcome from AUTO_CONFIRM, for the same silence."""
    [case] = resolve_silence(
        [_review(due=date(2026, 8, 25))],
        policy=SilencePolicy.REQUIRE_DECISION,
        today=TODAY,
    )
    assert case.auto_outcome is None
    assert "Nothing happens to this employee's status" in case.resolution


def test_auto_extend_fires_once_and_then_stops_extending() -> None:
    """An extension nobody chose should not be renewable by continuing not to
    choose."""
    first = resolve_silence(
        [_review(due=date(2026, 8, 25))],
        policy=SilencePolicy.AUTO_EXTEND_ONCE,
        today=TODAY,
    )
    assert first[0].auto_outcome == "extend"
    assert first[0].new_probation_end_date is not None

    already_extended = resolve_silence(
        [_review(due=date(2026, 8, 25), extends="r0")],
        policy=SilencePolicy.AUTO_EXTEND_ONCE,
        today=TODAY,
    )
    assert already_extended[0].auto_outcome is None
    assert "Waiting for a decision" in already_extended[0].resolution


def test_the_grace_period_stops_a_weekend_from_auto_confirming_somebody() -> None:
    review = _review(due=date(2026, 9, 5))
    assert resolve_silence(
        [review], policy=SilencePolicy.AUTO_CONFIRM, today=TODAY, grace_days=5
    ) == []
    assert resolve_silence(
        [review], policy=SilencePolicy.AUTO_CONFIRM, today=TODAY, grace_days=0
    )


def test_a_decided_review_is_not_a_drift_case() -> None:
    assert resolve_silence(
        [_review(due=date(2026, 8, 1), outcome="confirm")],
        policy=SilencePolicy.AUTO_CONFIRM,
        today=TODAY,
    ) == []


def test_a_missing_policy_is_refused_rather_than_defaulted() -> None:
    with pytest.raises(ProbationError, match="no default"):
        resolve_silence([_review()], policy="whatever", today=TODAY)
