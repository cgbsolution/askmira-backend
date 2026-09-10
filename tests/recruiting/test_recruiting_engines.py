"""Slice 36 — funnel, cost per hire, interviewer signals, referrals, diversity.

Organised by the way each control fails in a real recruitment dashboard: a
funnel that shows more interviews than screens, a cost per hire that omits
agency fees, an interviewer metric that becomes a scorecard, a duplicate
referral nobody warned two colleagues about, and a diversity figure computed
from four people.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.recruiting import diversity, funnel, interviewers, referrals

D = Decimal
S = funnel.Stage


def journey(index: int, furthest: S, source: str = "naukri", gaps=(3, 7, 19, 2, 10)):
    dates: dict[S, date] = {}
    cursor = date(2026, 1, 1) + timedelta(days=index)
    for position, stage in enumerate(list(S)):
        if stage.rank > furthest.rank:
            break
        dates[stage] = cursor
        if position < len(gaps):
            cursor += timedelta(days=gaps[position])
    return funnel.CandidateJourney(
        candidate_id=f"c{index}",
        source=source,
        stage_dates=dates,
        department_id="eng",
        recruiter_id="r1",
    )


POPULATION = (
    [journey(i, S.APPLIED) for i in range(40)]
    + [journey(i + 40, S.SCREENED) for i in range(20)]
    + [journey(i + 60, S.INTERVIEWED) for i in range(12)]
    + [journey(i + 80, S.OFFERED) for i in range(6)]
    + [journey(i + 90, S.JOINED, "referral") for i in range(6)]
)


# ══════════════════════════════════════════════════════════════════════════════
# Funnel
# ══════════════════════════════════════════════════════════════════════════════


def test_stages_are_cumulative_so_the_funnel_reconciles():
    """A candidate fast-tracked to interview with no recorded screen has been
    screened. Counting only recorded dates is what makes "interviewed" exceed
    "screened", and a recruiter notices within a minute."""
    fast = funnel.CandidateJourney(
        candidate_id="cx",
        source="naukri",
        stage_dates={S.APPLIED: date(2026, 1, 1), S.INTERVIEWED: date(2026, 1, 10)},
    )
    assert fast.reached(S.SCREENED)
    assert not fast.reached(S.OFFERED)


def test_the_funnel_is_monotonic():
    result = funnel.funnel(POPULATION)
    counts = [row["count"] for row in result["stages"]]
    assert counts == sorted(counts, reverse=True)


def test_the_largest_drop_off_is_named():
    result = funnel.funnel(POPULATION)
    assert result["largest_drop_off"]["into_stage"] in {
        "screened",
        "interviewed",
        "offered",
    }


def test_reconciliation_reports_a_monotonic_break_rather_than_rendering_it():
    """A chart that silently shows more interviews than screens is the version
    that destroys trust."""
    result = funnel.reconcile(POPULATION, expected_totals={"applied": 84})
    assert result["reconciles"] is True

    mismatch = funnel.reconcile(POPULATION, expected_totals={"applied": 99})
    assert mismatch["reconciles"] is False
    assert mismatch["total_breaks"][0]["difference"] == -15


def test_a_funnel_grouped_by_something_too_fine_is_refused():
    with pytest.raises(funnel.FunnelError) as exc:
        funnel.funnel(POPULATION, group_by="candidate_id")
    assert "describes individuals" in str(exc.value)


def test_the_funnel_groups_by_source():
    result = funnel.funnel(POPULATION, group_by="source")
    keys = {group["key"] for group in result["groups"]}
    assert keys == {"naukri", "referral"}


def test_a_drop_off_rate_from_an_empty_stage_is_none():
    """"100% drop-off from zero applicants" makes a dashboard look broken."""
    result = funnel.funnel([])
    assert all(row["drop_off_rate"] is None for row in result["stages"])


# ══════════════════════════════════════════════════════════════════════════════
# Timings
# ══════════════════════════════════════════════════════════════════════════════


def test_the_stall_is_named_with_its_share_of_the_total():
    """"Time to hire is 47 days" is a number nobody can act on."""
    result = funnel.stage_timings(POPULATION)
    assert result["stalls_at"]["from_stage"] == "interviewed"
    assert result["stalls_at"]["to_stage"] == "offered"
    assert result["stalls_at"]["median_days"] == 19.0
    assert "of the whole process" in result["stalls_at"]["reading"]


def test_a_transition_below_the_sample_floor_has_no_median():
    result = funnel.stage_timings([journey(0, S.JOINED)])
    assert all(row["median_days"] is None for row in result["transitions"])


def test_a_reschedule_does_not_reset_the_clock():
    """The elapsed time is measured between the stage dates, so a candidate
    rescheduled three times shows the full delay - which is the delay the
    hiring manager needs to see."""
    slow = [
        funnel.CandidateJourney(
            candidate_id=f"s{i}",
            source="x",
            stage_dates={
                S.APPLIED: date(2026, 1, 1),
                S.SCREENED: date(2026, 1, 3),
                S.INTERVIEWED: date(2026, 2, 20),
            },
        )
        for i in range(6)
    ]
    result = funnel.stage_timings(slow)
    row = next(
        r for r in result["transitions"] if r["to_stage"] == "interviewed"
    )
    assert row["median_days"] == 48.0


# ══════════════════════════════════════════════════════════════════════════════
# Cost per hire
# ══════════════════════════════════════════════════════════════════════════════


def test_a_missing_cost_component_is_named_and_the_figure_is_a_floor():
    """A cost per hire that omits agency fees looks excellent and is wrong, and
    it is the number a client repeats to their board."""
    result = funnel.cost_per_hire(
        hires=6,
        components={"job_board_spend": D("300000"), "referral_bonuses": D("120000")},
    )
    assert result["cost_per_hire"] == "70000.00"
    assert result["is_complete"] is False
    assert set(result["missing_components"]) == {"agency_fees", "recruiter_time"}
    assert "**floor**" in result["note"]


def test_a_complete_cost_says_so():
    result = funnel.cost_per_hire(
        hires=4, components=dict.fromkeys(funnel.COST_COMPONENTS, D("100000"))
    )
    assert result["is_complete"] is True
    assert result["cost_per_hire"] == "100000.00"


def test_an_unrecognised_cost_component_is_refused():
    """A component nobody defined silently changes the headline number."""
    with pytest.raises(funnel.FunnelError):
        funnel.cost_per_hire(hires=1, components={"pizza": D("500")})


def test_no_hires_gives_no_cost_per_hire_rather_than_infinity():
    result = funnel.cost_per_hire(
        hires=0, components={"job_board_spend": D("300000")}
    )
    assert result["cost_per_hire"] is None
    assert result["total_cost"] == "300000.00"


# ══════════════════════════════════════════════════════════════════════════════
# Source effectiveness
# ══════════════════════════════════════════════════════════════════════════════


def test_quality_is_the_pass_rate_not_the_volume():
    """Ranking by volume is how a recruiter ends up defending the job board
    that wastes the most of their time."""
    result = funnel.source_effectiveness(
        POPULATION, spend_by_source={"naukri": D("300000")}
    )
    naukri = next(r for r in result["sources"] if r["source"] == "naukri")
    assert naukri["applied"] == 78
    assert naukri["joined"] == 0
    assert naukri["conversion_to_hire"] == 0.0


def test_a_source_below_the_sample_floor_gets_no_rate():
    result = funnel.source_effectiveness(
        [journey(0, S.JOINED, "tiny")], minimum=10
    )
    tiny = next(r for r in result["sources"] if r["source"] == "tiny")
    assert tiny["conversion_to_hire"] is None


# ══════════════════════════════════════════════════════════════════════════════
# Interviewer signals
# ══════════════════════════════════════════════════════════════════════════════


def feedback(index, who, rating, rec=None, length=200, days=1.0, advanced=True):
    return interviewers.FeedbackFacts(
        feedback_id=f"f{index}",
        interviewer_id=who,
        candidate_id=f"c{index}",
        competency_ratings={"a": rating, "b": rating, "c": rating, "d": rating},
        recommendation_score=rec if rec is not None else rating,
        comment_length=length,
        turnaround_days=days,
        advanced=advanced,
    )


PEERS = [feedback(i + 100, f"p{i % 4}", 4) for i in range(20)]


def test_severity_reports_a_deviation_with_no_rank():
    """A league table is the artefact people optimise against, and once it
    exists somebody will screenshot it."""
    harsh = [feedback(i, "j1", 2, advanced=False) for i in range(12)]
    result = interviewers.severity(harsh + PEERS, interviewer_id="j1")
    assert result["deviation"] == -2.0
    assert "rank" not in result
    assert "percentile" not in result


def test_the_coaching_framing_travels_with_every_payload():
    """A UI that wants to present this as a score has to deliberately drop the
    sentence saying it is not one."""
    harsh = [feedback(i, "j1", 2) for i in range(12)]
    for payload in (
        interviewers.severity(harsh + PEERS, interviewer_id="j1"),
        interviewers.turnaround(harsh, interviewer_id="j1"),
        interviewers.feedback_quality(harsh, interviewer_id="j1"),
    ):
        assert "not a scorecard" in payload["framing"]


def test_being_strict_is_not_reported_as_being_wrong():
    harsh = [feedback(i, "j1", 2) for i in range(12)]
    result = interviewers.severity(harsh + PEERS, interviewer_id="j1")
    assert "It is a question, not a finding" in result["reading"]


def test_an_interviewer_below_the_floor_gets_no_figure():
    """Nobody's interviewing is characterised from four conversations."""
    few = [feedback(i, "j2", 3) for i in range(4)]
    result = interviewers.severity(few + PEERS, interviewer_id="j2")
    assert result["deviation"] is None
    assert "handful of conversations" in result["note"]


def test_a_peer_group_below_the_floor_gives_no_comparison():
    """A deviation from a peer group of three is a deviation from three
    individuals."""
    mine = [feedback(i, "j1", 3) for i in range(12)]
    others = [feedback(i + 50, "p1", 4) for i in range(3)]
    result = interviewers.severity(mine + others, interviewer_id="j1")
    assert result["peer_mean_rating"] is None


def test_turnaround_surfaces_the_slowest_not_only_the_median():
    """A median of two days hides the three-week wait that lost a candidate."""
    rows = [feedback(i, "j3", 3, days=1.0) for i in range(11)]
    rows.append(feedback(99, "j3", 3, days=21.0))
    result = interviewers.turnaround(rows, interviewer_id="j3")
    assert result["median_days"] == 1.0
    assert result["slowest_days"] == 21.0


def test_incoherent_feedback_is_the_diagnostic_signal():
    """"Strong hire" attached to ratings averaging 2 means the form was filled
    in after the decision."""
    rows = [feedback(i, "j4", 2, rec=5) for i in range(11)]
    result = interviewers.feedback_quality(rows, interviewer_id="j4")
    assert result["incoherent_count"] == 11
    assert len(result["incoherent_feedback_ids"]) == 11


def test_coherent_feedback_is_not_flagged():
    rows = [feedback(i, "j5", 4) for i in range(11)]
    result = interviewers.feedback_quality(rows, interviewer_id="j5")
    assert result["incoherent_count"] == 0
    assert "No review contradicts its own ratings" in result["reading"]


def test_the_quality_metric_calls_itself_a_proxy():
    """A four-word review saying "no - cannot write SQL, tested twice" is
    better than four paragraphs of impressions, and this scores it lower."""
    rows = [feedback(i, "j5", 4) for i in range(11)]
    result = interviewers.feedback_quality(rows, interviewer_id="j5")
    assert "**proxies**" in result["proxy_caveat"]


# ══════════════════════════════════════════════════════════════════════════════
# Referrals
# ══════════════════════════════════════════════════════════════════════════════


ELIGIBLE = referrals.ReferrerFacts(
    employee_id="e1", is_confirmed=True, is_on_pip=False, is_active=True
)


def test_an_eligible_referrer_passes():
    assert referrals.check_eligibility(ELIGIBLE, requisition_id="r1").eligible


def test_somebody_on_probation_cannot_refer():
    check = referrals.check_eligibility(
        referrals.ReferrerFacts("e1", False, False, True), requisition_id="r1"
    )
    assert not check.eligible
    assert check.reason is referrals.IneligibilityReason.NOT_CONFIRMED


def test_somebody_on_a_pip_cannot_refer_and_is_told_why():
    """It is a configured rule rather than a judgement about their referrals -
    and worth saying so, because they will ask."""
    check = referrals.check_eligibility(
        referrals.ReferrerFacts("e1", True, True, True), requisition_id="r1"
    )
    assert check.reason is referrals.IneligibilityReason.ON_PIP
    assert "configured rule rather than a judgement" in check.message


def test_the_hiring_team_cannot_refer_into_their_own_requisition():
    """Deciding on a candidate you are paid for is not a position to put
    somebody in, however honest they are."""
    check = referrals.check_eligibility(
        referrals.ReferrerFacts("e1", True, False, True, ("r1",)),
        requisition_id="r1",
    )
    assert check.reason is referrals.IneligibilityReason.IN_HIRING_TEAM


def test_a_self_referral_is_refused():
    check = referrals.check_eligibility(
        ELIGIBLE, requisition_id="r1", candidate_employee_id="e1"
    )
    assert check.reason is referrals.IneligibilityReason.SELF_REFERRAL


def test_a_role_outside_the_programme_is_refused():
    check = referrals.check_eligibility(
        ELIGIBLE, requisition_id="r1", eligible_roles=("eng",), role_key="sales"
    )
    assert check.reason is referrals.IneligibilityReason.ROLE_NOT_ELIGIBLE


def test_the_duplicate_rule_is_in_the_terms_before_anybody_refers():
    """The argument it prevents happens between two colleagues who both
    believed they were entitled."""
    terms = referrals.programme_terms(
        name="Refer a friend", bonus_by_grade={"M3": D("50000")}
    )
    assert "first submission" in terms["duplicate_rule"]
    assert "policy document" in terms["duplicate_rule"]


def test_the_terms_say_the_bonus_is_taxed():
    terms = referrals.programme_terms(
        name="Refer a friend", bonus_by_grade={"M3": D("50000")}
    )
    assert "tax deducted" in terms["tax_note"]


def test_milestones_that_do_not_total_a_hundred_are_refused():
    """An employee paid 80% of what they were promised will notice, and one
    paid 120% is a reconciliation nobody spots."""
    with pytest.raises(referrals.ReferralError):
        referrals.programme_terms(
            name="x",
            bonus_by_grade={},
            milestones=(referrals.BonusMilestone("a", "A", D("80"), 0),),
        )


def test_a_share_link_carries_the_referrer():
    """The employee never handles a CV. A flow that requires uploading one gets
    used once."""
    link = referrals.share_link(
        base_url="https://careers.acme.test",
        requisition_id="r1",
        referrer_id="e1",
        channel="whatsapp",
    )
    assert link["url"] == "https://careers.acme.test/apply/r1?ref=r1.e1"


def test_an_untracked_channel_is_refused():
    """An unattributed referral is an unpaid bonus."""
    with pytest.raises(referrals.ReferralError):
        referrals.share_link(
            base_url="x", requisition_id="r1", referrer_id="e1", channel="pigeon"
        )


def test_the_first_submission_wins():
    first = referrals.ReferralFacts(
        "f1", "e1", "cand@x.test", datetime(2026, 9, 1, 10, tzinfo=UTC)
    )
    late = referrals.ReferralFacts(
        "f2", "e2", "cand@x.test", datetime(2026, 9, 5, 10, tzinfo=UTC)
    )
    result = referrals.resolve_duplicate([first], incoming=late)
    assert result["is_duplicate"] is True
    assert result["winner_referrer_id"] == "e1"
    assert "first submission wins" in result["message"]


def test_an_earlier_referral_arriving_second_still_wins():
    first = referrals.ReferralFacts(
        "f1", "e1", "cand@x.test", datetime(2026, 9, 1, 10, tzinfo=UTC)
    )
    late = referrals.ReferralFacts(
        "f2", "e2", "cand@x.test", datetime(2026, 9, 5, 10, tzinfo=UTC)
    )
    result = referrals.resolve_duplicate([late], incoming=first)
    assert result["is_duplicate"] is False
    assert result["superseded"] == ["f2"]


def test_a_different_candidate_is_not_a_duplicate():
    first = referrals.ReferralFacts(
        "f1", "e1", "a@x.test", datetime(2026, 9, 1, tzinfo=UTC)
    )
    other = referrals.ReferralFacts(
        "f2", "e2", "b@x.test", datetime(2026, 9, 5, tzinfo=UTC)
    )
    assert referrals.resolve_duplicate([first], incoming=other)["is_duplicate"] is False


def test_the_bonus_splits_across_milestones():
    """Paying it all on joining rewards a referral that leaves in six weeks
    exactly as much as one that stays five years."""
    schedule = referrals.payout_schedule(
        bonus_amount=D("10000"), joined_on=date(2026, 10, 1)
    )
    assert [m["amount"] for m in schedule["milestones"]] == ["5000.00", "5000.00"]
    assert schedule["milestones"][1]["payable_on"] == "2026-12-30"


def test_the_last_milestone_carries_the_rounding_remainder():
    """Paying 3,333.33 three times leaves a paisa nobody can account for."""
    schedule = referrals.payout_schedule(
        bonus_amount=D("10000"),
        joined_on=date(2026, 10, 1),
        milestones=(
            referrals.BonusMilestone("a", "A", D("33.33"), 0),
            referrals.BonusMilestone("b", "B", D("33.33"), 90),
            referrals.BonusMilestone("c", "C", D("33.34"), 180),
        ),
    )
    assert schedule["total_scheduled"] == "10000.00"
    assert schedule["reconciles"] is True


def test_retention_below_the_floor_is_not_reported():
    """This is the figure that justifies the programme's cost, so it is not
    produced from a handful of people."""
    result = referrals.retention_comparison(
        referred_tenures_days=[900] * 3, other_tenures_days=[600] * 15
    )
    assert result["median_referred_days"] is None
    assert "board pack" in result["note"]


def test_retention_is_a_median_not_a_mean():
    result = referrals.retention_comparison(
        referred_tenures_days=[900] * 12, other_tenures_days=[600] * 15
    )
    assert result["difference_days"] == 300.0
    assert "not a mean" in result["note"]


# ══════════════════════════════════════════════════════════════════════════════
# Diversity
# ══════════════════════════════════════════════════════════════════════════════


def diverse_population():
    rows, disclosures = [], []
    for i in range(30):
        furthest = (
            S.JOINED if i < 2 else S.INTERVIEWED if i < 12 else S.SCREENED
        )
        rows.append(journey(i, furthest))
        disclosures.append(diversity.DisclosedAttribute(f"c{i}", "gender", "female"))
    for i in range(30, 100):
        furthest = (
            S.JOINED if i < 45 else S.INTERVIEWED if i < 70 else S.SCREENED
        )
        rows.append(journey(i, furthest))
        disclosures.append(diversity.DisclosedAttribute(f"c{i}", "gender", "male"))
    for i in range(100, 120):
        rows.append(journey(i, S.APPLIED))
    return rows, disclosures


def test_undisclosed_candidates_are_excluded_not_imputed():
    """An absent answer is not an answer, and this attribute is collected under
    consent."""
    rows, disclosures = diverse_population()
    result = diversity.coverage(rows, disclosures=disclosures, dimension="gender")
    assert result["candidates"] == 120
    assert result["disclosed"] == 100
    assert "rather than counted as a default value" in result["note"]


def test_low_coverage_is_warned_about():
    """A 30% figure computed from 40% of candidates is a different claim from
    one computed from 95%."""
    rows, disclosures = diverse_population()
    padded = rows + [journey(i, S.APPLIED) for i in range(500, 700)]
    result = diversity.coverage(padded, disclosures=disclosures, dimension="gender")
    assert result["coverage_rate"] < 0.5
    assert "describe the people who answered" in result["note"]


def test_a_stage_below_the_threshold_is_suppressed():
    """A stage with four candidates in a group identifies them."""
    rows, disclosures = diverse_population()
    result = diversity.stage_representation(
        rows, disclosures=disclosures, dimension="gender", focus_value="female"
    )
    late = next(r for r in result["stages"] if r["stage"] == "joined")
    assert late["suppressed"] is True
    assert late["share"] is None


def test_the_largest_fall_in_share_is_named():
    """Every group shrinks through a funnel; the finding is the stage where one
    shrinks faster than the rest."""
    rows, disclosures = diverse_population()
    result = diversity.stage_representation(
        rows, disclosures=disclosures, dimension="gender", focus_value="female"
    )
    assert result["largest_fall"] is not None
    assert "next week" in result["largest_fall"]["reading"]


def test_a_tiny_population_produces_nothing_at_all():
    rows = [journey(i, S.APPLIED) for i in range(6)]
    disclosures = [
        diversity.DisclosedAttribute(f"c{i}", "gender", "female" if i < 3 else "male")
        for i in range(6)
    ]
    result = diversity.stage_representation(
        rows, disclosures=disclosures, dimension="gender", focus_value="female"
    )
    assert all(row["suppressed"] for row in result["stages"])
    assert result["largest_fall"] is None
