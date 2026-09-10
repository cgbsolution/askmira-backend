"""Slice 32 — surveys, awards, values, action plans and internal comms.

Organised by the way each control fails in a real culture programme: a
breakdown that identifies a team of three, a jury that reads the first score, a
value tally converted into a rating, an action plan completed with nothing done,
a policy edited after two hundred people acknowledged it, and a birthday
broadcast the employee never agreed to.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.services.culture import actionplans as ap
from app.services.culture import awards, comms, surveys, values

TODAY = date(2026, 9, 7)
NOW = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)


def rows(n: int, **answers: int) -> list[dict[str, int]]:
    return [dict(answers) for _ in range(n)]


# ══════════════════════════════════════════════════════════════════════════════
# eNPS: the standard question, scored the standard way
# ══════════════════════════════════════════════════════════════════════════════


def test_enps_uses_the_standard_bands():
    result = surveys.enps([10, 9, 9, 8, 7, 6, 3, 10, 9, 5])
    assert result.promoters == 5
    assert result.passives == 2
    assert result.detractors == 3
    assert result.score == 20


def test_enps_is_promoters_minus_detractors_as_a_percentage():
    assert surveys.enps([10] * 10).score == 100
    assert surveys.enps([0] * 10).score == -100
    assert surveys.enps([8] * 10).score == 0


def test_a_response_outside_zero_to_ten_is_refused():
    """A house variant produces a figure comparable with nothing, which defeats
    the only reason to use a standard instrument."""
    with pytest.raises(surveys.SurveyError) as exc:
        surveys.enps([1, 2, 11])
    assert "not measuring eNPS" in str(exc.value)


def test_enps_below_the_threshold_is_suppressed():
    result = surveys.enps([10, 9, 0])
    assert result.suppressed
    assert result.as_dict()["score"] is None
    assert result.as_dict()["responses"] == 3


def test_the_enps_question_is_the_instrument_wording():
    inst = surveys.instrument("enps")
    assert inst.items[0].text == surveys.ENPS_QUESTION
    assert inst.scale is surveys.ScaleKind.ZERO_TO_TEN


# ══════════════════════════════════════════════════════════════════════════════
# The anonymity threshold
# ══════════════════════════════════════════════════════════════════════════════


def test_a_group_below_the_threshold_is_withheld():
    """A team of three has no average that is not three named opinions."""
    result = surveys.breakdown(
        {
            "eng": rows(12, clarity=4),
            "tiny": rows(3, clarity=2),
            "sales": rows(9, clarity=5),
        },
        instrument_key="engagement_9",
    )
    tiny = next(g for g in result if g.key == "tiny")
    assert not tiny.is_visible
    assert tiny.suppression is surveys.Suppression.BELOW_THRESHOLD
    assert tiny.scores == {}


def test_the_count_is_still_shown_for_a_withheld_group():
    """"Too few to report" with no number sends somebody to ask HR for it."""
    result = surveys.breakdown(
        {"tiny": rows(3, clarity=2), "eng": rows(12, clarity=4), "ops": rows(8, clarity=3)},
        instrument_key="engagement_9",
    )
    tiny = next(g for g in result if g.key == "tiny")
    assert tiny.responses == 3
    assert tiny.as_dict()["responses"] == 3


def test_suppression_is_complementary():
    """Otherwise the hidden group is the remainder, and a manager can subtract.

    Engineering's total minus the three visible teams is the fourth team's
    score. Suppressing only the small one is the appearance of a threshold.
    """
    result = surveys.breakdown(
        {
            "a": rows(20, clarity=5),
            "b": rows(12, clarity=4),
            "c": rows(6, clarity=3),
            "tiny": rows(3, clarity=1),
        },
        instrument_key="engagement_9",
    )
    hidden = {g.key for g in result if not g.is_visible}
    assert "tiny" in hidden
    assert "c" in hidden, (
        "Only the smallest group was suppressed, so its score is recoverable "
        "by subtracting the visible groups from the published total."
    )
    complementary = next(g for g in result if g.key == "c")
    assert complementary.suppression is surveys.Suppression.COMPLEMENTARY


def test_no_complementary_suppression_when_no_total_is_published():
    result = surveys.breakdown(
        {"a": rows(20, clarity=5), "b": rows(12, clarity=4), "tiny": rows(3, clarity=1)},
        instrument_key="engagement_9",
        parent_total_shown=False,
    )
    hidden = {g.key for g in result if not g.is_visible}
    assert hidden == {"tiny"}


def test_every_group_below_the_threshold_is_withheld_not_just_the_smallest():
    result = surveys.breakdown(
        {"a": rows(20, clarity=5), "x": rows(4, clarity=1), "y": rows(2, clarity=1)},
        instrument_key="engagement_9",
    )
    assert {g.key for g in result if g.is_visible} == set()


# ══════════════════════════════════════════════════════════════════════════════
# Scoring
# ══════════════════════════════════════════════════════════════════════════════


def test_a_reverse_scored_item_is_flipped_before_averaging():
    """"I have to hide parts of who I am" scored 1 is a good answer.

    Averaging it unflipped reads a healthy team as a sick one, invisibly.
    """
    scores = surveys.score_scale(
        [{"hide": 1, "belong": 5}], instrument_key="inclusion_6"
    )
    assert scores["hide"] == 5.0
    assert scores["belong"] == 5.0


def test_a_value_outside_the_scale_is_refused():
    with pytest.raises(surveys.SurveyError):
        surveys.score_scale([{"clarity": 9}], instrument_key="engagement_9")


def test_an_unknown_instrument_is_refused_rather_than_invented():
    with pytest.raises(surveys.SurveyError) as exc:
        surveys.instrument("vibes")
    assert "validated instruments" in str(exc.value)


# ══════════════════════════════════════════════════════════════════════════════
# Re-survey comparison
# ══════════════════════════════════════════════════════════════════════════════


def test_movement_is_reported_between_two_waves_of_one_instrument():
    result = surveys.compare_waves(
        before=rows(8, clarity=3),
        after=rows(8, clarity=4),
        before_instrument="engagement_9",
        after_instrument="engagement_9",
    )
    assert result.comparable
    assert result.deltas["clarity"] == 1.0


def test_a_movement_across_instruments_is_refused():
    """A delta between a five-point and a seven-point average is fabricated, and
    printed as "3.8 → 4.4" nothing shows they measured different things."""
    result = surveys.compare_waves(
        before=rows(8, clarity=3),
        after=rows(8, cheerful=4),
        before_instrument="engagement_9",
        after_instrument="wellbeing_5",
    )
    assert not result.comparable
    assert result.deltas == {}


def test_a_movement_from_a_handful_of_answers_is_refused():
    result = surveys.compare_waves(
        before=rows(3, clarity=3),
        after=rows(8, clarity=4),
        before_instrument="engagement_9",
        after_instrument="engagement_9",
    )
    assert not result.comparable


# ══════════════════════════════════════════════════════════════════════════════
# Awards: the window
# ══════════════════════════════════════════════════════════════════════════════


WINDOW = awards.NominationWindow(opens_on=date(2026, 9, 1), closes_on=date(2026, 9, 20))


def test_the_window_is_inclusive_at_both_ends():
    """A window advertised as closing on the 20th and closing at 00:00 on the
    20th loses every nomination made on the day people were told they had."""
    assert awards.window_state(WINDOW, today=date(2026, 9, 1)) is awards.WindowState.OPEN
    assert awards.window_state(WINDOW, today=date(2026, 9, 20)) is awards.WindowState.OPEN


def test_before_and_after_the_window_are_distinct_states():
    assert (
        awards.window_state(WINDOW, today=date(2026, 8, 31))
        is awards.WindowState.NOT_OPEN
    )
    assert (
        awards.window_state(WINDOW, today=date(2026, 9, 21))
        is awards.WindowState.CLOSED
    )


def test_a_closed_window_refuses_and_says_when_it_closed():
    with pytest.raises(awards.AwardError) as exc:
        awards.check_window_open(WINDOW, today=date(2026, 9, 21))
    assert "2026-09-20" in str(exc.value)


def test_a_window_that_closes_before_it_opens_is_refused():
    with pytest.raises(awards.AwardError):
        awards.NominationWindow(opens_on=date(2026, 9, 20), closes_on=date(2026, 9, 1))


def test_a_nomination_needs_a_citation():
    """A name with no case attached is a vote, and a vote is a popularity
    contest with a jury watching."""
    with pytest.raises(awards.AwardError):
        awards.Nomination("n1", "e2", "e1", "impact", "   ")


def test_a_self_nomination_is_refused():
    with pytest.raises(awards.AwardError):
        awards.Nomination("n1", "e1", "e1", "impact", "I was great.")


# ══════════════════════════════════════════════════════════════════════════════
# Awards: jury anchoring
# ══════════════════════════════════════════════════════════════════════════════


SUBMITTED_A = awards.JuryScore("n1", "j1", {"impact": 9}, is_submitted=True)
SUBMITTED_B = awards.JuryScore("n1", "j2", {"impact": 3}, is_submitted=True)
DRAFT_C = awards.JuryScore("n1", "j3", {"impact": 10}, is_submitted=False)


def test_a_juror_who_has_not_submitted_sees_only_their_own():
    """Reading "9/10, obvious winner" before scoring changes what you write, and
    five opinions that anchored on the first are one opinion."""
    visible = awards.visible_scores_for(
        viewer_juror_id="j3", all_scores=[SUBMITTED_A, SUBMITTED_B, DRAFT_C]
    )
    assert [s.juror_id for s in visible] == ["j3"]


def test_a_juror_who_has_submitted_sees_the_other_submitted_scores():
    visible = awards.visible_scores_for(
        viewer_juror_id="j1", all_scores=[SUBMITTED_A, SUBMITTED_B, DRAFT_C]
    )
    assert {s.juror_id for s in visible} == {"j1", "j2"}


def test_a_juror_never_sees_another_jurors_draft():
    visible = awards.visible_scores_for(
        viewer_juror_id="j1", all_scores=[SUBMITTED_A, SUBMITTED_B, DRAFT_C]
    )
    assert "j3" not in {s.juror_id for s in visible}


def test_a_juror_with_no_score_at_all_sees_nothing():
    visible = awards.visible_scores_for(
        viewer_juror_id="j9", all_scores=[SUBMITTED_A, SUBMITTED_B]
    )
    assert visible == []


def test_outstanding_jurors_are_named_not_counted():
    assert awards.outstanding_jurors(
        jurors=["j1", "j2", "j3"], scores=[SUBMITTED_A, SUBMITTED_B, DRAFT_C]
    ) == ["j3"]


# ══════════════════════════════════════════════════════════════════════════════
# Awards: aggregation
# ══════════════════════════════════════════════════════════════════════════════


NOMINATION = awards.Nomination("n1", "e2", "e1", "impact", "Rebuilt reconciliation.")


def test_only_submitted_scores_are_aggregated():
    """A draft is somebody's thinking; averaging it means the standings move as
    people type."""
    result = awards.aggregate_scores(
        NOMINATION, [SUBMITTED_A, SUBMITTED_B, DRAFT_C]
    )
    assert result.juror_count == 2
    assert result.mean_score == 6.0


def test_the_spread_is_reported_beside_the_mean():
    """7 from 7,7,7 and 7 from 2,9,10 are different facts, and only one of them
    is a decision the jury has reached."""
    result = awards.aggregate_scores(NOMINATION, [SUBMITTED_A, SUBMITTED_B])
    assert result.spread == 6.0
    assert "worth a conversation" in result.note


def test_a_tie_is_left_as_a_tie():
    """An award decided by alphabetical order is one the runner-up hears
    about."""
    a = awards.NominationResult("n1", "e1", "impact", 3, 8.0, {}, 0.0, "")
    b = awards.NominationResult("n2", "e2", "impact", 3, 8.0, {}, 0.0, "")
    shortlist = awards.ranked_shortlist([a, b], category_key="impact")
    assert shortlist["tied"] is True
    assert set(shortlist["leading"]) == {"n1", "n2"}


def test_a_nomination_without_enough_jurors_is_not_ranked():
    thin = awards.NominationResult("n3", "e3", "impact", 1, 10.0, {}, 0.0, "")
    solid = awards.NominationResult("n1", "e1", "impact", 3, 8.0, {}, 0.0, "")
    shortlist = awards.ranked_shortlist([thin, solid], category_key="impact")
    assert shortlist["awaiting_jury"] == ["n3"]
    assert shortlist["leading"] == ["n1"]


# ══════════════════════════════════════════════════════════════════════════════
# Values
# ══════════════════════════════════════════════════════════════════════════════


VALUES = [
    values.ValueDefinition("customer", "Customer obsession", "We start from the customer."),
    values.ValueDefinition("craft", "Craft", "We ship work we would sign."),
]


def tagged(n: int, code: str, source=values.TagSource.SHOUTOUT, subject="e1", group="eng"):
    return [
        values.TaggedItem(f"{code}-{i}", source, code, subject, date(2026, 9, 1), group)
        for i in range(n)
    ]


def test_a_value_needs_a_description():
    """A one-word value on a dropdown gets tagged by whoever is guessing
    hardest, and the analytics then measure the guessing."""
    with pytest.raises(values.ValuesError):
        values.ValueDefinition("speed", "Speed", "  ")


def test_coverage_counts_each_source_separately():
    """A hundred shoutouts and one award are not a hundred and one pieces of
    evidence."""
    items = tagged(19, "craft") + tagged(1, "customer", values.TagSource.NOMINATION)
    report = values.coverage(items, values=VALUES)
    assert report["totals"] == {"shoutout": 19, "nomination": 1}
    customer = next(r for r in report["values"] if r["code"] == "customer")
    assert customer["counts"] == {"shoutout": 0, "nomination": 1}
    assert customer["share"]["nomination"] == 1.0


def test_a_tag_to_an_undefined_value_is_refused():
    with pytest.raises(values.ValuesError) as exc:
        values.coverage(tagged(1, "nope"), values=VALUES)
    assert "tally of typos" in str(exc.value)


def test_an_unlived_value_is_surfaced_with_a_reading():
    items = tagged(40, "craft") + tagged(1, "customer")
    unlived = values.unlived_values(items, values=VALUES)
    assert [u["code"] for u in unlived] == ["customer"]
    assert "not how this company actually works" in unlived[0]["reading"]


def test_a_small_team_gets_no_value_breakdown():
    report = values.by_group(
        tagged(6, "craft", group="eng") + tagged(2, "craft", group="tiny"),
        values=VALUES,
        group_sizes={"eng": 12, "tiny": 3},
    )
    tiny = next(g for g in report["groups"] if g["group_key"] == "tiny")
    assert tiny["visible"] is False
    assert tiny["values"] == []


def test_a_persons_values_come_back_as_evidence_not_a_score():
    """Recognition converted into a rating is a popularity metric wearing a
    value's name."""
    report = values.for_person(tagged(3, "craft", subject="e1"), subject_id="e1")
    assert len(report["items"]) == 3
    assert "score" not in report
    assert "Evidence, not a score" in report["note"]


def test_a_trend_is_counts_per_named_period_not_a_direction():
    items = tagged(3, "craft")
    report = values.trend(
        items,
        value_code="craft",
        periods=[
            ("Aug", date(2026, 8, 1), date(2026, 8, 31)),
            ("Sep", date(2026, 9, 1), date(2026, 9, 30)),
        ],
    )
    assert [p["count"] for p in report["periods"]] == [0, 3]
    assert "Direction is left to the reader" in report["note"]


# ══════════════════════════════════════════════════════════════════════════════
# Action plans
# ══════════════════════════════════════════════════════════════════════════════


DONE = ap.ActionItem(
    "i1", "Weekly one-to-ones reinstated", "m1", date(2026, 8, 1),
    done_on=date(2026, 8, 20),
)
OPEN = ap.ActionItem("i2", "Publish the promotion criteria", "m1", date(2026, 8, 15))
DROPPED = ap.ActionItem(
    "i2",
    "Publish the promotion criteria",
    "m1",
    date(2026, 8, 15),
    dropped_reason="Superseded by the new levelling framework",
)


def plan(items, status=ap.PlanStatus.IN_PROGRESS) -> ap.ActionPlan:
    return ap.ActionPlan(
        "p1", "s1", ap.PlanScope.DEPARTMENT, "eng", "voice", "m1",
        date(2026, 8, 31), status, tuple(items),
    )


def test_an_action_item_needs_a_named_owner():
    """"The team" owns nothing, and an item owned by nobody is the one still
    open next year."""
    with pytest.raises(ap.ActionPlanError):
        ap.ActionItem("x", "Do a thing", "", date(2026, 9, 1))


def test_a_plan_with_open_items_cannot_be_completed():
    """A plan completed with open items is a checkbox, and a checkbox is what
    the second year of a dying survey programme is made of."""
    ok, reason = ap.can_complete(plan([DONE, OPEN]))
    assert not ok
    assert "still open" in reason
    assert "Publish the promotion criteria" in reason


def test_a_plan_with_no_items_cannot_be_completed():
    ok, reason = ap.can_complete(plan([]))
    assert not ok
    assert "no action items" in reason


def test_an_explicitly_dropped_item_resolves_the_plan():
    """A dropped item people can see is better than an open one nobody
    mentions."""
    ok, _ = ap.can_complete(plan([DONE, DROPPED]))
    assert ok
    assert plan([DONE, DROPPED]).completion == 1.0


def test_completion_counts_dropped_as_resolved_not_as_done():
    p = plan([DONE, DROPPED])
    assert p.done_count == 1
    assert p.completion == 1.0


def test_overdue_plans_are_listed_oldest_first():
    rows = ap.overdue([plan([DONE, OPEN])], today=TODAY)
    assert rows[0]["plan_id"] == "p1"
    assert rows[0]["days_late"] == 7


def test_a_completed_plan_is_not_reported_overdue():
    assert ap.overdue([plan([DONE, DROPPED], ap.PlanStatus.COMPLETED)], today=TODAY) == []


def test_loop_closure_reports_action_and_movement_together():
    report = ap.loop_closure(
        plans=[plan([DONE, OPEN])], movement={"voice": 0.4}, today=TODAY
    )
    voice = next(r for r in report["themes"] if r["theme"] == "voice")
    assert voice["acted"] is True
    assert voice["movement"] == 0.4
    assert "loop closed" in voice["reading"]


def test_movement_with_no_action_is_not_booked_as_a_win():
    report = ap.loop_closure(
        plans=[plan([OPEN])], movement={"voice": 0.6}, today=TODAY
    )
    voice = next(r for r in report["themes"] if r["theme"] == "voice")
    assert "do not book this as a win" in voice["reading"]


def test_a_theme_never_re_surveyed_is_reported_as_unmeasured():
    """Not measured and did not move are different findings."""
    report = ap.loop_closure(plans=[plan([DONE])], movement={}, today=TODAY)
    voice = next(r for r in report["themes"] if r["theme"] == "voice")
    assert voice["movement"] is None
    assert "gap in the measurement" in voice["reading"]


def test_nothing_done_and_nothing_moved_says_what_that_means():
    report = ap.loop_closure(
        plans=[plan([OPEN])], movement={"voice": -0.3}, today=TODAY
    )
    voice = next(r for r in report["themes"] if r["theme"] == "voice")
    assert "ends survey programmes" in voice["reading"]


# ══════════════════════════════════════════════════════════════════════════════
# Announcements
# ══════════════════════════════════════════════════════════════════════════════


RECIPIENTS = [
    comms.Recipient("e1", "eng", "blr", "M3"),
    comms.Recipient("e2", "eng", "del", "M3"),
    comms.Recipient("e3", "sales", "blr", "M2"),
]


def posh(body: str = "The policy body.") -> comms.Announcement:
    return comms.Announcement(
        "a1", "POSH policy 2026", body, comms.Audience(), NOW,
        requires_acknowledgement=True,
    )


def test_an_empty_audience_means_everyone():
    """Unconfigured and nobody have to be different states."""
    assert comms.Audience().is_everyone
    assert len(comms.resolve_audience(comms.Audience(), RECIPIENTS)) == 3


def test_audience_dimensions_are_combined_with_and():
    """"Engineering" and "Bengaluru" means Engineering in Bengaluru."""
    audience = comms.Audience(department_ids=("eng",), location_ids=("blr",))
    assert [r.employee_id for r in comms.resolve_audience(audience, RECIPIENTS)] == ["e1"]


def test_an_acknowledgement_names_who_has_not_read_it():
    """"43 outstanding" cannot be chased, and the point of the register is that
    somebody chases it before the audit."""
    announcement = posh()
    ack = comms.Acknowledgement("a1", "e1", NOW, announcement.content_hash)
    report = comms.acknowledgement_report(
        announcement=announcement, recipients=RECIPIENTS, acknowledgements=[ack]
    )
    assert report["outstanding"] == ["e2", "e3"]
    assert report["acknowledged"] == 1
    assert report["complete"] is False


def test_editing_the_body_invalidates_earlier_acknowledgements():
    """Otherwise a company tells an auditor that two hundred people read a
    policy that did not exist when they signed."""
    original = posh()
    ack = comms.Acknowledgement("a1", "e1", NOW, original.content_hash)
    revised = posh("The policy body, revised.")

    report = comms.acknowledgement_report(
        announcement=revised, recipients=RECIPIENTS, acknowledgements=[ack]
    )
    assert report["stale"] == ["e1"]
    assert "e1" in report["outstanding"]
    assert "does not carry over" in report["note"]


def test_retitling_also_invalidates_an_acknowledgement():
    body = "The policy body."
    assert comms.body_hash("POSH policy 2026", body) != comms.body_hash(
        "Updated POSH policy", body
    )


def test_an_acknowledgement_announcement_cannot_expire():
    """The audit question is asked months after it stopped being news."""
    with pytest.raises(comms.AnnouncementError) as exc:
        comms.Announcement(
            "a1", "POSH", "body", comms.Audience(), NOW,
            expires_at=datetime(2026, 10, 1, tzinfo=UTC),
            requires_acknowledgement=True,
        )
    assert "register" in str(exc.value)


def test_a_register_is_refused_for_an_announcement_that_never_asked():
    plain = comms.Announcement("a2", "Canteen", "Closed Friday.", comms.Audience(), NOW)
    with pytest.raises(comms.AnnouncementError):
        comms.acknowledgement_report(
            announcement=plain, recipients=RECIPIENTS, acknowledgements=[]
        )


# ══════════════════════════════════════════════════════════════════════════════
# Events calendar and birthday privacy
# ══════════════════════════════════════════════════════════════════════════════


PERSON_EVENTS = [
    comms.PersonEvent("e1", comms.EventKind.BIRTHDAY, date(2026, 9, 9), "Anita"),
    comms.PersonEvent("e2", comms.EventKind.WORK_ANNIVERSARY, date(2026, 9, 10), "Ravi", years=5),
    comms.PersonEvent("e9", comms.EventKind.HOLIDAY, date(2026, 10, 2), "Gandhi Jayanti"),
]


def test_privacy_defaults_to_not_sharing():
    setting = comms.EventPrivacy("e1")
    assert setting.share_birthday is False
    assert setting.share_work_anniversary is False


def test_an_employee_with_no_privacy_record_is_not_broadcast():
    """The absence of a record is a refusal, not a gap to fill with yes."""
    events = comms.calendar_events(person_events=PERSON_EVENTS, privacy={})
    assert all(e.kind is not comms.EventKind.BIRTHDAY for e in events)


def test_a_birthday_appears_only_after_an_explicit_opt_in():
    events = comms.calendar_events(
        person_events=PERSON_EVENTS,
        privacy={"e1": comms.EventPrivacy("e1", share_birthday=True)},
    )
    assert any(
        e.kind is comms.EventKind.BIRTHDAY and e.employee_id == "e1" for e in events
    )


def test_a_work_anniversary_is_a_separate_opt_in_from_a_birthday():
    """Somebody happy to mark five years may not want their age inferable."""
    events = comms.calendar_events(
        person_events=PERSON_EVENTS,
        privacy={"e1": comms.EventPrivacy("e1", share_work_anniversary=True)},
    )
    assert all(e.kind is not comms.EventKind.BIRTHDAY for e in events)


def test_a_holiday_needs_no_opt_in():
    events = comms.calendar_events(person_events=PERSON_EVENTS, privacy={})
    holiday = next(e for e in events if e.kind is comms.EventKind.HOLIDAY)
    assert holiday.employee_id is None


def test_a_shared_birthday_carries_its_scope():
    events = comms.calendar_events(
        person_events=PERSON_EVENTS,
        privacy={
            "e1": comms.EventPrivacy("e1", share_birthday=True, birthday_scope="team")
        },
    )
    birthday = next(e for e in events if e.kind is comms.EventKind.BIRTHDAY)
    assert birthday.scope == "team"


def test_a_permissive_default_privacy_is_refused():
    """A caller that can pass a permissive default has an opt-out setting
    wearing an opt-in's name."""
    with pytest.raises(comms.AnnouncementError):
        comms.calendar_events(
            person_events=PERSON_EVENTS,
            privacy={},
            default_privacy=comms.EventPrivacy("x", share_birthday=True),
        )
