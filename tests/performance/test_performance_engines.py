"""Slice 35 — PIP trail, increment bands, budget, equity and promotion.

Organised by the way each control fails in a real performance process: a PIP
that reads as a decision already made, a rating band that forces a manager to
inflate the rating, a budget a manager cannot see while allocating, a pay-gap
figure computed from four people, and a promotion that half-applies.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.services.performance import increments, pip, promotion

D = Decimal
TODAY = date(2026, 9, 7)
E = pip.EmployeeInputState


# ══════════════════════════════════════════════════════════════════════════════
# PIP: the check-in that carries the employee's voice
# ══════════════════════════════════════════════════════════════════════════════


def check_in(index: int, **kw) -> pip.CheckIn:
    body = {
        "check_in_id": f"c{index}",
        "scheduled_for": date(2026, 9, 7) + timedelta(days=index * 14),
        "held_on": date(2026, 9, 7) + timedelta(days=index * 14),
        "manager_comments": "Reviewed the ticket numbers and the two escalations.",
        "employee_comments": "I disagree about the Q2 target but will work on it.",
        "employee_input_state": E.GIVEN,
    }
    body.update(kw)
    return pip.record_check_in(**body)


MILESTONE = pip.Milestone(
    "m1",
    "Close twelve tickets a week",
    date(2026, 10, 1),
    "Weekly closed-ticket count of 12 or more for four consecutive weeks",
)


def plan(**kw) -> pip.PipFacts:
    body = {
        "pip_id": "p1",
        "employee_id": "e1",
        "initiated_by": "m1",
        "start_date": date(2026, 9, 1),
        "end_date": date(2026, 11, 30),
        "reason": "Sustained shortfall against agreed targets since May.",
        "expectations": "Close twelve tickets a week and clear the escalation backlog.",
        "support_provided": "Weekly coaching with the team lead and a rewritten runbook.",
        "status": pip.PipStatus.ACTIVE,
        "milestones": (MILESTONE,),
        "check_ins": tuple(check_in(i) for i in range(3)),
    }
    body.update(kw)
    return pip.PipFacts(**body)


def test_a_check_in_the_employee_was_never_asked_about_is_refused():
    """A trail of manager observations with no employee response is one side of
    a conversation, and its evidential value is close to zero."""
    with pytest.raises(pip.PipError) as exc:
        check_in(0, employee_input_state=E.NOT_ASKED, employee_comments="")
    assert "one side of a conversation" in str(exc.value)


def test_a_bare_declined_is_refused():
    """Indistinguishable from never having asked."""
    with pytest.raises(pip.PipError):
        check_in(0, employee_input_state=E.DECLINED, employee_comments="")


def test_a_reasoned_decline_is_accepted_and_evidenced():
    """Declining is a real thing people do and it has to be representable;
    otherwise a manager types something on the employee's behalf."""
    row = check_in(
        0,
        employee_input_state=E.DECLINED,
        employee_comments="",
        decline_reason="Said he would prefer to respond in writing later.",
    )
    assert row.is_evidenced
    assert not row.has_employee_voice


def test_an_empty_given_comment_is_refused():
    with pytest.raises(pip.PipError):
        check_in(0, employee_input_state=E.GIVEN, employee_comments="   ")


def test_a_check_in_with_no_manager_account_is_refused():
    with pytest.raises(pip.PipError):
        check_in(0, manager_comments="  ")


def test_an_unheld_check_in_is_not_evidence():
    with pytest.raises(pip.PipError) as exc:
        check_in(0, held_on=None)
    assert "has not happened yet" in str(exc.value)


# ══════════════════════════════════════════════════════════════════════════════
# PIP: what the plan itself has to contain
# ══════════════════════════════════════════════════════════════════════════════


def test_a_plan_needs_written_expectations():
    """'Improve your performance' is not a standard anybody can meet or fail."""
    with pytest.raises(pip.PipError) as exc:
        plan(expectations="   ")
    assert "not a standard anybody can meet or fail" in str(exc.value)


def test_a_plan_needs_the_support_recorded():
    """An improvement plan the employer contributed nothing to is a notice
    period with paperwork."""
    with pytest.raises(pip.PipError) as exc:
        plan(support_provided="")
    assert "notice period with paperwork" in str(exc.value)


def test_a_milestone_needs_measurable_criteria():
    with pytest.raises(pip.PipError) as exc:
        pip.Milestone("m2", "Improve communication", date(2026, 10, 1), "  ")
    assert "cannot be defended" in str(exc.value)


def test_a_plan_cannot_end_before_it_starts():
    with pytest.raises(pip.PipError):
        plan(start_date=date(2026, 11, 1), end_date=date(2026, 9, 1))


# ══════════════════════════════════════════════════════════════════════════════
# PIP: trail quality
# ══════════════════════════════════════════════════════════════════════════════


def test_a_complete_trail_reports_defensible():
    assessment = pip.trail_quality(plan())
    assert assessment.is_defensible
    assert assessment.check_ins_held == 3
    assert assessment.employee_voice_count == 3
    assert assessment.gaps == ()


def test_too_few_check_ins_is_named_as_a_gap():
    """A plan with one conversation at the start and one at the end was declared
    rather than run."""
    assessment = pip.trail_quality(plan(check_ins=(check_in(0),)))
    assert not assessment.is_defensible
    assert any("declared rather than run" in g for g in assessment.gaps)


def test_no_milestones_is_named_as_a_gap():
    assessment = pip.trail_quality(plan(milestones=()))
    assert any("nothing the employee could have met" in g for g in assessment.gaps)


def test_a_very_short_plan_is_named_as_a_gap():
    """A period too short for the employee to have plausibly changed anything
    reads as a decision already made."""
    assessment = pip.trail_quality(
        plan(start_date=date(2026, 9, 1), end_date=date(2026, 9, 20))
    )
    assert any("decision already made" in g for g in assessment.gaps)


def test_gaps_are_named_rather_than_scored():
    """"72% complete" tells nobody what to do."""
    assessment = pip.trail_quality(plan(check_ins=()))
    assert assessment.gaps
    assert not hasattr(assessment, "score")


# ══════════════════════════════════════════════════════════════════════════════
# PIP: outcomes
# ══════════════════════════════════════════════════════════════════════════════


def test_an_unsuccessful_outcome_on_a_thin_trail_is_refused():
    """An exit built on a plan that cannot be evidenced is the situation this
    module exists to prevent."""
    with pytest.raises(pip.PipError) as exc:
        pip.close_pip(
            plan(check_ins=(check_in(0),)),
            outcome=pip.PipOutcome.UNSUCCESSFUL,
            today=date(2026, 12, 1),
            reason="Targets not met.",
        )
    assert "routes to an exit" in str(exc.value)


def test_proceeding_on_a_thin_trail_is_possible_and_recorded():
    """Forcing an organisation to falsify a check-in to proceed would be far
    worse than proceeding with the gap recorded."""
    result = pip.close_pip(
        plan(check_ins=(check_in(0),)),
        outcome=pip.PipOutcome.UNSUCCESSFUL,
        today=date(2026, 12, 1),
        reason="Targets not met.",
        acknowledge_weak_trail=True,
    )
    assert result.routes_to_exit
    assert "acknowledged despite gaps" in result.message


def test_a_successful_outcome_on_a_thin_trail_is_permitted():
    """The asymmetry is deliberate: a successful PIP closed on a thin trail
    costs nobody anything - the employee keeps their job."""
    result = pip.close_pip(
        plan(check_ins=(check_in(0),)),
        outcome=pip.PipOutcome.SUCCESSFUL,
        today=date(2026, 12, 1),
    )
    assert result.new_status is pip.PipStatus.SUCCESSFUL
    assert not result.routes_to_exit


def test_an_unsuccessful_outcome_needs_a_written_reason():
    settled = plan(
        milestones=(
            pip.Milestone(
                "m1", "Close twelve a week", date(2026, 10, 1), ">= 12 weekly", met=False
            ),
        )
    )
    with pytest.raises(pip.PipError) as exc:
        pip.close_pip(
            settled,
            outcome=pip.PipOutcome.UNSUCCESSFUL,
            today=date(2026, 12, 1),
            reason="   ",
        )
    assert "employment tribunal reads first" in str(exc.value)


def test_an_extension_needs_a_new_date_and_a_reason():
    """Extending without one reads, later, as having moved the goalposts."""
    with pytest.raises(pip.PipError):
        pip.close_pip(
            plan(), outcome=pip.PipOutcome.EXTENDED, today=TODAY, reason="Because."
        )
    with pytest.raises(pip.PipError):
        pip.close_pip(
            plan(),
            outcome=pip.PipOutcome.EXTENDED,
            today=TODAY,
            reason="",
            new_end_date=date(2026, 12, 31),
        )


def test_an_extension_must_move_the_date_forward():
    with pytest.raises(pip.PipError):
        pip.close_pip(
            plan(),
            outcome=pip.PipOutcome.EXTENDED,
            today=TODAY,
            reason="More time needed.",
            new_end_date=date(2026, 10, 1),
        )


def test_an_abandoned_plan_says_it_is_not_a_failure():
    result = pip.close_pip(
        plan(),
        outcome=pip.PipOutcome.ABANDONED,
        today=TODAY,
        reason="The role was removed in a reorganisation.",
    )
    assert result.new_status is pip.PipStatus.ABANDONED
    assert "not a failure by the employee" in result.message


def test_a_closed_plan_cannot_be_closed_again():
    with pytest.raises(pip.PipError):
        pip.close_pip(
            plan(status=pip.PipStatus.SUCCESSFUL),
            outcome=pip.PipOutcome.UNSUCCESSFUL,
            today=TODAY,
            reason="Changed our minds.",
        )


def test_an_unassessed_milestone_on_a_closed_plan_is_a_gap():
    """Whether the milestones were met is the substance of the outcome."""
    assessment = pip.trail_quality(plan(status=pip.PipStatus.UNSUCCESSFUL))
    assert any("never assessed" in g for g in assessment.gaps)


def test_the_evidence_pack_reproduces_the_check_ins_in_full():
    """A précis of somebody's contemporaneous account is not their account."""
    pack = pip.evidence_pack(plan(), today=TODAY)
    assert len(pack["check_ins"]) == 3
    assert (
        pack["check_ins"][0]["employee_comments"]
        == "I disagree about the Q2 target but will work on it."
    )
    assert pack["support_provided"]
    assert pack["trail"]["is_defensible"] is True


# ══════════════════════════════════════════════════════════════════════════════
# Increment bands
# ══════════════════════════════════════════════════════════════════════════════


BANDS = [
    increments.IncrementBand("Exceeds", D("8"), D("14"), D("11")),
    increments.IncrementBand("Meets", D("4"), D("8"), D("6")),
    increments.IncrementBand("Below", D("0"), D("3"), D("0")),
]


def test_a_recommendation_inside_its_band_is_clean():
    result = increments.recommend(
        employee_id="e1",
        rating_label="Meets",
        current_ctc=D("1200000"),
        percentage=D("7"),
        bands=BANDS,
    )
    assert result.within_band
    assert result.recommended_ctc == D("1284000.00")
    assert not result.requires_justification


def test_outside_the_band_needs_a_justification_but_is_not_refused():
    """A hard band produces a manager who inflates the rating to pay what
    somebody is worth, which corrupts the rating data everything depends on."""
    without = increments.recommend(
        employee_id="e1",
        rating_label="Meets",
        current_ctc=D("1200000"),
        percentage=D("15"),
        bands=BANDS,
    )
    assert not without.within_band
    assert without.requires_justification

    with_reason = increments.recommend(
        employee_id="e1",
        rating_label="Meets",
        current_ctc=D("1200000"),
        percentage=D("15"),
        bands=BANDS,
        justification="Took over a failing project in March and turned it round.",
    )
    assert not with_reason.requires_justification


def test_an_unmapped_rating_is_refused_rather_than_defaulted():
    """Quietly applying somebody else's band produces a number the employee
    cannot be given a reason for."""
    with pytest.raises(increments.IncrementError) as exc:
        increments.band_for(BANDS, rating_label="Outstanding")
    assert "nobody can explain to the employee" in str(exc.value)


def test_a_band_whose_recommendation_falls_outside_it_is_refused():
    """A manager following the recommendation would be out of policy."""
    with pytest.raises(increments.IncrementError):
        increments.IncrementBand("X", D("8"), D("14"), D("20"))


def test_a_negative_band_is_refused():
    with pytest.raises(increments.IncrementError):
        increments.IncrementBand("X", D("-5"), D("14"), D("0"))


# ══════════════════════════════════════════════════════════════════════════════
# Live budget
# ══════════════════════════════════════════════════════════════════════════════


def test_headroom_reports_both_scopes_and_names_the_binding_one():
    """A manager told their department has room while the company total is
    exhausted will allocate and be rejected."""
    result = increments.headroom(
        department_allocated=D("1000000"),
        department_consumed=D("500000"),
        total_allocated=D("5000000"),
        total_consumed=D("4990000"),
        department_key="eng",
    )
    assert result["binding_scope"] == "company"
    assert result["spendable"] == "10000.00"


def test_a_block_mode_overage_refuses(monkeypatch=None):
    result = increments.consume(
        increase=D("200000"),
        department_allocated=D("1000000"),
        department_consumed=D("900000"),
        total_allocated=D("5000000"),
        total_consumed=D("1000000"),
        department_key="eng",
        mode=increments.OverageMode.BLOCK,
    )
    assert not result.permitted
    assert result.overage == D("100000.00")
    assert "set to block" in result.message


def test_an_escalate_mode_overage_permits_and_routes():
    result = increments.consume(
        increase=D("200000"),
        department_allocated=D("1000000"),
        department_consumed=D("900000"),
        total_allocated=D("5000000"),
        total_consumed=D("1000000"),
        department_key="eng",
        mode=increments.OverageMode.ESCALATE,
    )
    assert result.permitted
    assert result.requires_escalation
    assert result.overage == D("100000.00")


def test_a_recommendation_inside_budget_reports_what_is_left():
    result = increments.consume(
        increase=D("50000"),
        department_allocated=D("1000000"),
        department_consumed=D("100000"),
        total_allocated=D("5000000"),
        total_consumed=D("500000"),
        department_key="eng",
    )
    assert result.permitted
    assert not result.requires_escalation
    assert result.after["department"]["remaining"] == "850000.00"


def test_a_negative_increment_is_refused():
    with pytest.raises(increments.IncrementError):
        increments.consume(
            increase=D("-1000"),
            department_allocated=D("1000000"),
            department_consumed=D("0"),
            total_allocated=D("5000000"),
            total_consumed=D("0"),
            department_key="eng",
        )


# ══════════════════════════════════════════════════════════════════════════════
# Equity
# ══════════════════════════════════════════════════════════════════════════════


def peers(n: int, start: int = 1000000, step: int = 50000):
    return [
        increments.PeerFacts(
            employee_id=f"p{i}",
            ctc=D(str(start + i * step)),
            grade="M3",
            role="Engineer",
            location="BLR",
            gender="male" if i % 2 else "female",
        )
        for i in range(n)
    ]


def test_compa_ratio_reads_in_plain_words():
    """0.82 means nothing to most managers."""
    result = increments.compa_ratio(ctc=D("1000000"), band_midpoint=D("1200000"))
    assert result["compa_ratio"] == "0.833"
    assert "below the midpoint" in result["reading"]


def test_a_zero_midpoint_is_refused():
    with pytest.raises(increments.IncrementError):
        increments.compa_ratio(ctc=D("1000000"), band_midpoint=D("0"))


def test_a_small_group_gets_no_parity_or_gap_figure():
    """With three people the "gap" is one person's salary and the arithmetic
    names them."""
    snapshot = increments.equity_snapshot(
        employee_id="e1", proposed_ctc=D("1150000"), peers=peers(3)
    )
    assert snapshot["parity"] is None
    assert snapshot["gender_gap"] is None
    assert "names them" in snapshot["note"]


def test_a_usable_group_gets_parity_against_the_median():
    snapshot = increments.equity_snapshot(
        employee_id="e1", proposed_ctc=D("1150000"), peers=peers(12)
    )
    assert snapshot["parity"]["peer_median"]
    assert "comparable colleagues" in snapshot["parity"]["reading"]


def test_the_gender_gap_needs_both_groups_to_clear_the_floor():
    """A gap computed across smaller groups identifies individuals."""
    lopsided = [
        increments.PeerFacts(f"p{i}", D("1000000"), "M3", "Eng", "BLR", "male")
        for i in range(10)
    ] + [
        increments.PeerFacts(f"q{i}", D("900000"), "M3", "Eng", "BLR", "female")
        for i in range(2)
    ]
    snapshot = increments.equity_snapshot(
        employee_id="e1", proposed_ctc=D("1000000"), peers=lopsided
    )
    assert snapshot["gender_gap"] is None
    assert "identifies individuals" in snapshot["note"]


def test_the_gap_is_reported_as_an_observation_not_a_verdict():
    """A tool that asserts discrimination on four data points is switched off
    within a quarter."""
    snapshot = increments.equity_snapshot(
        employee_id="e1",
        proposed_ctc=D("1150000"),
        peers=peers(14),
        employee_gender="female",
    )
    reading = snapshot["gender_gap"]["reading"]
    assert "not a finding" in reading
    assert "legitimate explanations" in reading


def test_the_employees_own_row_is_excluded_from_the_comparison():
    """So a small group is not compared largely against itself."""
    group = [*peers(10), increments.PeerFacts("e1", D("9999999"), "M3", "Eng", "BLR")]
    snapshot = increments.equity_snapshot(
        employee_id="e1", proposed_ctc=D("1150000"), peers=group
    )
    assert snapshot["comparison_group_size"] == 10
    assert Decimal(snapshot["parity"]["peer_max"]) < D("9999999")


# ══════════════════════════════════════════════════════════════════════════════
# Promotion
# ══════════════════════════════════════════════════════════════════════════════


def promotion_kwargs(**kw):
    body = {
        "employee_id": "e1",
        "from_grade": "M3",
        "to_grade": "M4",
        "from_position_id": "pos-1",
        "to_position_id": "pos-2",
        "current_ctc": D("1200000"),
        "new_ctc": D("1500000"),
        "effective_from": date(2026, 4, 1),
        "today": TODAY,
    }
    body.update(kw)
    return body


def test_a_promotion_plans_all_four_steps():
    """Three of them leaves the employee on the new grade at the old salary."""
    plan_out = promotion.plan_promotion(**promotion_kwargs())
    assert set(plan_out.steps) == set(promotion.PromotionStep)
    assert plan_out.is_complete


def test_a_promotion_with_no_target_grade_is_refused_before_starting():
    """Rolling back cleanly is good; not having attempted it is better, because
    the rollback is invisible and the manager is told nothing."""
    with pytest.raises(promotion.PromotionError) as exc:
        promotion.plan_promotion(**promotion_kwargs(to_grade="  "))
    assert "promoted everywhere except in the grade" in str(exc.value)


def test_a_promotion_to_the_same_grade_is_refused():
    with pytest.raises(promotion.PromotionError) as exc:
        promotion.plan_promotion(**promotion_kwargs(to_grade="M3"))
    assert "calling it a promotion misstates" in str(exc.value)


def test_a_promotion_with_no_target_position_is_refused():
    with pytest.raises(promotion.PromotionError):
        promotion.plan_promotion(**promotion_kwargs(to_position_id=""))


def test_a_promotion_that_reduces_pay_is_refused():
    with pytest.raises(promotion.PromotionError):
        promotion.plan_promotion(**promotion_kwargs(new_ctc=D("1000000")))


def test_a_backdated_promotion_flags_arrears():
    plan_out = promotion.plan_promotion(**promotion_kwargs())
    assert plan_out.is_backdated
    assert plan_out.arrears_from == date(2026, 4, 1)


def test_arrears_run_from_the_effective_date_not_the_payroll_run():
    """A revision effective in April and approved in July owes April onward
    whether or not payroll ran in between."""
    window = promotion.arrears_window(
        effective_from=date(2026, 4, 1),
        today=date(2026, 7, 15),
        last_payroll_run=date(2026, 6, 30),
    )
    assert window["from"] == "2026-04-01"
    assert window["months_spanned"] == 3


def test_a_future_effective_date_owes_no_arrears():
    window = promotion.arrears_window(
        effective_from=date(2026, 10, 1), today=TODAY
    )
    assert window["arrears_due"] is False


# ══════════════════════════════════════════════════════════════════════════════
# Letter release
# ══════════════════════════════════════════════════════════════════════════════


def test_letters_do_not_release_before_the_controlled_date():
    """Released as each approval lands, the first person to hear is whoever has
    the fastest approver."""
    decision = promotion.release_plan(
        release_on=date(2026, 10, 1),
        today=TODAY,
        approved_employee_ids=["a", "b"],
        pending_employee_ids=[],
    )
    assert not decision.permitted
    assert decision.block is promotion.ReleaseBlock.BEFORE_RELEASE_DATE


def test_letters_do_not_release_while_approvals_are_outstanding():
    decision = promotion.release_plan(
        release_on=date(2026, 10, 1),
        today=date(2026, 10, 1),
        approved_employee_ids=["a", "b"],
        pending_employee_ids=["c"],
    )
    assert not decision.permitted
    assert decision.block is promotion.ReleaseBlock.APPROVALS_OUTSTANDING
    assert decision.outstanding_approvals == ("c",)


def test_a_complete_cycle_releases_together():
    decision = promotion.release_plan(
        release_on=date(2026, 10, 1),
        today=date(2026, 10, 1),
        approved_employee_ids=["a", "b", "c"],
        pending_employee_ids=[],
    )
    assert decision.permitted
    assert decision.letter_count == 3


def test_a_partial_release_is_possible_and_names_who_is_missing():
    """A cycle occasionally has one genuinely stuck approval, and holding two
    hundred letters for it helps nobody."""
    decision = promotion.release_plan(
        release_on=date(2026, 10, 1),
        today=date(2026, 10, 1),
        approved_employee_ids=["a", "b"],
        pending_employee_ids=["c"],
        force_partial=True,
    )
    assert decision.permitted
    assert "Partial release" in decision.message
    assert decision.outstanding_approvals == ("c",)


def test_an_empty_cycle_has_nothing_to_release():
    decision = promotion.release_plan(
        release_on=date(2026, 10, 1),
        today=date(2026, 10, 1),
        approved_employee_ids=[],
        pending_employee_ids=[],
    )
    assert not decision.permitted
    assert decision.block is promotion.ReleaseBlock.NOTHING_TO_RELEASE
