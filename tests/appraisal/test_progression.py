"""Slice 15 gate: stage progression, returns, and the self-appraisal conflict.

Returns are the interesting part. A manager sending a self-appraisal back for
more detail is ordinary and constant, and a workflow that only moves forward
makes people work around it — usually by rating something they think is wrong
rather than asking for a rewrite.
"""

from __future__ import annotations

import pytest

from app.services.appraisal.progression import (
    ProgressionError,
    StageDefinition,
    detect_self_appraisal_conflict,
    next_stage,
    plan_return,
    resolve_stage_owner,
)

SELF = StageDefinition("s1", 1, "self", "Self appraisal")
MANAGER = StageDefinition("s2", 2, "manager", "Manager review")
REVIEWER = StageDefinition("s3", 3, "reviewer", "Reviewer sign-off", is_mandatory=False)
HR = StageDefinition("s4", 4, "hr", "HR closure")

CHAIN = [SELF, MANAGER, REVIEWER, HR]


# ═══════════════════════════════════════════════════════════════════════════
# Sequence
# ═══════════════════════════════════════════════════════════════════════════


def test_a_cycle_starts_at_the_first_stage() -> None:
    move = next_stage(stages=CHAIN, current_stage_id=None, completed_stage_ids=set())
    assert move.to_stage_id == "s1"
    assert move.is_return is False


def test_stages_advance_in_sequence() -> None:
    move = next_stage(stages=CHAIN, current_stage_id="s1", completed_stage_ids={"s1"})
    assert move.to_stage_id == "s2"


def test_stage_order_comes_from_the_sequence_not_the_list_order() -> None:
    """A cycle built out of order still runs in order."""
    shuffled = [HR, SELF, REVIEWER, MANAGER]
    move = next_stage(stages=shuffled, current_stage_id=None, completed_stage_ids=set())
    assert move.to_stage_id == "s1"


def test_a_mandatory_stage_cannot_be_skipped() -> None:
    """An appraisal nobody in the reporting line signed off is not an
    appraisal."""
    move = next_stage(stages=CHAIN, current_stage_id="s1", completed_stage_ids={"s1"})
    assert move.to_stage_id == MANAGER.stage_id
    assert move.skipped == []


def test_an_optional_stage_is_stepped_over_and_reported() -> None:
    """Reported, so a cycle that bypassed the reviewer says so rather than
    looking as though the reviewer signed off."""
    move = next_stage(
        stages=CHAIN, current_stage_id="s2", completed_stage_ids={"s1", "s2"}
    )
    assert move.to_stage_id == "s4"
    assert move.skipped == ["Reviewer sign-off"]


def test_a_completed_optional_stage_is_not_skipped() -> None:
    move = next_stage(
        stages=CHAIN, current_stage_id="s2", completed_stage_ids={"s1", "s2", "s3"}
    )
    assert move.to_stage_id == "s3"


def test_the_last_stage_completes_the_chain() -> None:
    move = next_stage(
        stages=CHAIN, current_stage_id="s4", completed_stage_ids={"s1", "s2", "s3", "s4"}
    )
    assert move.to_stage_id is None
    assert move.is_complete is True


def test_a_cycle_with_no_stages_is_refused() -> None:
    with pytest.raises(ProgressionError, match="no stages"):
        next_stage(stages=[], current_stage_id=None, completed_stage_ids=set())


def test_duplicate_sequence_numbers_are_refused() -> None:
    """Otherwise the order the chain runs in is undefined."""
    clashing = [SELF, StageDefinition("sx", 1, "manager", "Also first")]
    with pytest.raises(ProgressionError, match="share a sequence"):
        next_stage(stages=clashing, current_stage_id=None, completed_stage_ids=set())


def test_a_stage_from_another_cycle_is_refused() -> None:
    with pytest.raises(ProgressionError, match="not part of this cycle"):
        next_stage(stages=CHAIN, current_stage_id="elsewhere", completed_stage_ids=set())


# ═══════════════════════════════════════════════════════════════════════════
# Returns — first class
# ═══════════════════════════════════════════════════════════════════════════


def test_a_return_goes_backwards_and_records_the_reason() -> None:
    """The checklist gate."""
    move = plan_return(
        stages=CHAIN,
        current_stage_id="s2",
        target_stage_id="s1",
        reason="Please add specific examples against the delivery goal.",
    )
    assert move.is_return is True
    assert move.to_stage_id == "s1"
    assert "specific examples" in move.reason


def test_a_return_without_a_reason_is_refused() -> None:
    """"Returned" with no reason tells the recipient nothing about what to
    change, and they will guess or ask — both slower than a sentence."""
    with pytest.raises(ProgressionError, match="needs a reason"):
        plan_return(
            stages=CHAIN, current_stage_id="s2", target_stage_id="s1", reason="   "
        )


def test_a_return_can_skip_back_more_than_one_stage() -> None:
    """HR sending something back to the self-appraisal is a real move."""
    move = plan_return(
        stages=CHAIN, current_stage_id="s4", target_stage_id="s1",
        reason="The self-appraisal is blank.",
    )
    assert move.to_stage_id == "s1"


def test_returning_forwards_is_refused() -> None:
    with pytest.raises(ProgressionError, match="A return goes backwards"):
        plan_return(
            stages=CHAIN, current_stage_id="s1", target_stage_id="s2", reason="Onward."
        )


def test_returning_to_the_same_stage_is_refused() -> None:
    with pytest.raises(ProgressionError, match="A return goes backwards"):
        plan_return(
            stages=CHAIN, current_stage_id="s2", target_stage_id="s2", reason="Again."
        )


def test_the_transition_is_json_serialisable() -> None:
    import json

    json.dumps(
        plan_return(
            stages=CHAIN, current_stage_id="s2", target_stage_id="s1", reason="Redo."
        ).as_dict()
    )


# ═══════════════════════════════════════════════════════════════════════════
# The self-appraisal conflict — the gate
# ═══════════════════════════════════════════════════════════════════════════


def test_the_self_stage_is_meant_to_be_the_subject() -> None:
    check = detect_self_appraisal_conflict(
        stage_role="self", subject_user_id="u1", stage_owner_user_id="u1",
        escalation_chain=["u2", "u3"],
    )
    assert check.has_conflict is False


def test_owning_your_own_manager_stage_is_detected_and_escalated() -> None:
    """The checklist gate: detected, not silently permitted.

    It escalates rather than blocking — blocking would stall the cycle on an
    org-chart problem HR may not be able to fix today.
    """
    check = detect_self_appraisal_conflict(
        stage_role="manager", subject_user_id="u1", stage_owner_user_id="u1",
        escalation_chain=["u2", "u3"],
    )
    assert check.has_conflict is True
    assert check.escalate_to_user_id == "u2"
    assert "no independent view" in check.reason


def test_an_ordinary_manager_stage_has_no_conflict() -> None:
    check = detect_self_appraisal_conflict(
        stage_role="manager", subject_user_id="u1", stage_owner_user_id="u2",
        escalation_chain=["u3"],
    )
    assert check.has_conflict is False


def test_the_escalation_never_lands_back_on_the_subject() -> None:
    """A chain that loops back to the person being appraised is the same
    problem one level removed."""
    check = detect_self_appraisal_conflict(
        stage_role="manager", subject_user_id="u1", stage_owner_user_id="u1",
        escalation_chain=["u1", "u4"],
    )
    assert check.escalate_to_user_id == "u4"


def test_nobody_to_escalate_to_says_so_rather_than_falling_back() -> None:
    """A founder, or someone whose manager left. Falling back to the subject
    would be the original problem with extra steps."""
    check = detect_self_appraisal_conflict(
        stage_role="manager", subject_user_id="u1", stage_owner_user_id="u1",
        escalation_chain=[],
    )
    assert check.has_conflict is True
    assert check.escalate_to_user_id is None
    assert "HR has to assign a reviewer" in check.reason


def test_an_ownerless_stage_is_a_conflict_too() -> None:
    """A manager field that is simply empty leaves nobody accountable."""
    check = detect_self_appraisal_conflict(
        stage_role="manager", subject_user_id="u1", stage_owner_user_id=None,
        escalation_chain=["u2"],
    )
    assert check.has_conflict is True
    assert check.escalate_to_user_id == "u2"


def test_the_conflict_check_is_json_serialisable() -> None:
    import json

    json.dumps(
        detect_self_appraisal_conflict(
            stage_role="manager", subject_user_id="u1", stage_owner_user_id="u1",
            escalation_chain=["u2"],
        ).as_dict()
    )


# ═══════════════════════════════════════════════════════════════════════════
# Delegation and escalation
# ═══════════════════════════════════════════════════════════════════════════


def test_a_delegation_records_the_original_owner() -> None:
    """The checklist gate.

    An audit trail showing only who acted cannot answer "why did someone
    outside my reporting line rate me".
    """
    acting, original, escalated, reason = resolve_stage_owner(
        nominal_owner_user_id="u2", delegate_user_id="u5",
        is_overdue=False, escalation_chain=["u3"],
    )
    assert acting == "u5"
    assert original == "u2"
    assert escalated is False
    assert "original owner is recorded" in reason


def test_an_overdue_stage_escalates_one_level_up() -> None:
    """Without this a cycle stalls on one absent manager."""
    acting, original, escalated, reason = resolve_stage_owner(
        nominal_owner_user_id="u2", delegate_user_id=None,
        is_overdue=True, escalation_chain=["u3", "u4"],
    )
    assert acting == "u3"
    assert original == "u2"
    assert escalated is True
    assert "stalls on one absent manager" in reason


def test_an_explicit_delegation_beats_an_escalation() -> None:
    """Someone who said "Priya covers my reviews while I am away" has already
    answered the question the escalation would be asking."""
    acting, original, escalated, _ = resolve_stage_owner(
        nominal_owner_user_id="u2", delegate_user_id="u5",
        is_overdue=True, escalation_chain=["u3"],
    )
    assert acting == "u5"
    assert escalated is False


def test_an_overdue_stage_with_nobody_above_stays_put() -> None:
    acting, original, escalated, reason = resolve_stage_owner(
        nominal_owner_user_id="u2", delegate_user_id=None,
        is_overdue=True, escalation_chain=[],
    )
    assert acting == "u2"
    assert escalated is False
    assert "HR is notified" in reason


def test_an_on_time_stage_stays_with_its_owner() -> None:
    acting, original, escalated, _ = resolve_stage_owner(
        nominal_owner_user_id="u2", delegate_user_id=None,
        is_overdue=False, escalation_chain=["u3"],
    )
    assert acting == "u2"
    assert original is None
    assert escalated is False


def test_escalation_never_lands_on_the_stage_owner_themselves() -> None:
    acting, _, _, _ = resolve_stage_owner(
        nominal_owner_user_id="u2", delegate_user_id=None,
        is_overdue=True, escalation_chain=["u2", "u7"],
    )
    assert acting == "u7"
