"""Slice 29 — autonomy ceilings, routing, expiry and the rejection signal.

The centre of this file is the ceiling: four actions can never be applied
automatically, whatever a client configures, and the tests assert each one by
name rather than deriving it from the same data they are checking.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.autonomy import proposals
from app.services.autonomy.catalogue import (
    ACTIONS,
    PROTECTED_KEYS,
    AutonomyError,
    AutonomyLevel,
    ImpactLevel,
    action,
    catalogue_contradictions,
    ceiling_for,
    protected_actions,
    validate_autonomy,
)

NOW = datetime(2026, 9, 7, tzinfo=UTC)


# ══════════════════════════════════════════════════════════════════════════════
# The platform ceiling
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("action_key", sorted(PROTECTED_KEYS))
@pytest.mark.parametrize(
    "level",
    [AutonomyLevel.AUTO_APPLY_WITH_NOTIFY, AutonomyLevel.AUTO_APPLY_SILENT],
)
def test_a_protected_action_cannot_be_configured_to_auto_apply(action_key, level):
    """Tested per protected action, per automatic level."""
    with pytest.raises(AutonomyError) as exc:
        validate_autonomy(action_key, level)
    message = str(exc.value)
    assert message.strip()
    assert "cannot" in message or "capped" in message


def test_the_protected_actions_are_exactly_the_ones_named():
    """Asserted by key, not derived from the field being checked."""
    assert {a.key for a in protected_actions()} == PROTECTED_KEYS
    assert {
        "compliance_monitor.apply_payroll_rule_change",
        "disbursement.release_payment_file",
        "appraisal_assistant.change_rating",
        "your_voice.act_on_case",
        "timesheets.recommend_approval",
        "scheduling.cover_short_shift",
    } == PROTECTED_KEYS


def test_every_protected_action_explains_its_ceiling():
    """A client told no with no reason asks again next quarter."""
    for spec in protected_actions():
        assert spec.ceiling_reason.strip(), f"{spec.key} gives no reason"
        assert len(spec.ceiling_reason) > 40


def test_your_voice_cannot_even_propose():
    """The channel's value is that a person handles it end to end."""
    spec = action("your_voice.act_on_case")
    assert spec.max_autonomy is AutonomyLevel.SUGGEST_ONLY
    with pytest.raises(AutonomyError):
        validate_autonomy(
            "your_voice.act_on_case", AutonomyLevel.PROPOSE_FOR_APPROVAL
        )


def test_an_irreversible_action_is_capped_at_approval():
    for key, spec in ACTIONS.items():
        if not spec.reversible:
            assert ceiling_for(key) is AutonomyLevel.PROPOSE_FOR_APPROVAL or (
                ceiling_for(key) is AutonomyLevel.SUGGEST_ONLY
            ), f"{key} is irreversible and its ceiling permits automation"


def test_the_irreversible_refusal_says_why():
    with pytest.raises(AutonomyError) as exc:
        validate_autonomy(
            "appraisal_assistant.change_rating", AutonomyLevel.AUTO_APPLY_SILENT
        )
    assert "cannot be undone" in str(exc.value) or "increment" in str(exc.value)


def test_a_reversible_low_impact_action_may_be_silent():
    """The ceiling is not a blanket ban — it is a judgement per action."""
    assert (
        validate_autonomy(
            "attendance.regularise_missing_punch", AutonomyLevel.AUTO_APPLY_SILENT
        )
        is AutonomyLevel.AUTO_APPLY_SILENT
    )


def test_configuring_at_the_ceiling_is_allowed():
    assert (
        validate_autonomy(
            "expense_extraction.approve_claim",
            AutonomyLevel.AUTO_APPLY_WITH_NOTIFY,
        )
        is AutonomyLevel.AUTO_APPLY_WITH_NOTIFY
    )


def test_the_catalogue_does_not_contradict_itself():
    assert catalogue_contradictions() == []


def test_no_default_exceeds_its_own_ceiling():
    for key, spec in ACTIONS.items():
        assert spec.default_autonomy.rank <= ceiling_for(key).rank, key


def test_a_threshold_on_an_action_that_has_no_unit_is_refused():
    with pytest.raises(AutonomyError) as exc:
        validate_autonomy(
            "attendance.regularise_missing_punch",
            AutonomyLevel.AUTO_APPLY_SILENT,
            threshold=Decimal("100"),
        )
    assert "stored and never read" in str(exc.value)


def test_a_negative_threshold_is_refused():
    with pytest.raises(AutonomyError):
        validate_autonomy(
            "expense_extraction.approve_claim",
            AutonomyLevel.PROPOSE_FOR_APPROVAL,
            threshold=Decimal("-1"),
        )


def test_an_unknown_action_names_what_exists():
    with pytest.raises(AutonomyError) as exc:
        action("nonsense.key")
    assert "compliance_monitor" in str(exc.value)


def test_autonomy_levels_are_ordered():
    ranks = [level.rank for level in AutonomyLevel]
    assert ranks == sorted(ranks)
    assert AutonomyLevel.SUGGEST_ONLY.rank < AutonomyLevel.AUTO_APPLY_SILENT.rank
    assert not AutonomyLevel.PROPOSE_FOR_APPROVAL.applies_automatically
    assert AutonomyLevel.AUTO_APPLY_WITH_NOTIFY.applies_automatically


# ══════════════════════════════════════════════════════════════════════════════
# Threshold routing
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("amount", "auto"),
    [("1999", True), ("1999.99", True), ("2000", False), ("2001", False)],
)
def test_threshold_routing_at_and_either_side_of_the_boundary(amount, auto):
    """At the boundary it routes to a person: 2000 is not under 2000."""
    routing = proposals.route(
        "expense_extraction.approve_claim",
        configured=AutonomyLevel.AUTO_APPLY_WITH_NOTIFY,
        magnitude=Decimal(amount),
        threshold=Decimal("2000"),
    )
    assert routing.applies_automatically is auto
    assert routing.reason


def test_no_threshold_means_the_configured_level_applies():
    routing = proposals.route(
        "expense_extraction.approve_claim",
        configured=AutonomyLevel.AUTO_APPLY_WITH_NOTIFY,
    )
    assert routing.applies_automatically


def test_routing_caps_a_stored_value_above_the_ceiling():
    """The last thing between a stored value and an automatic action."""
    routing = proposals.route(
        "disbursement.release_payment_file",
        configured=AutonomyLevel.AUTO_APPLY_SILENT,
    )
    assert not routing.applies_automatically
    assert routing.autonomy is AutonomyLevel.PROPOSE_FOR_APPROVAL


def test_notify_is_distinct_from_silent():
    notify = proposals.route(
        "lifecycle.raise_confirmation",
        configured=AutonomyLevel.AUTO_APPLY_WITH_NOTIFY,
    )
    assert notify.notify
    silent = proposals.route(
        "attendance.regularise_missing_punch",
        configured=AutonomyLevel.AUTO_APPLY_SILENT,
    )
    assert not silent.notify
    assert silent.applies_automatically


# ══════════════════════════════════════════════════════════════════════════════
# Proposals: evaluable, and they expire
# ══════════════════════════════════════════════════════════════════════════════


def _proposal(**overrides):
    base = {
        "action_key": "expense_extraction.approve_claim",
        "subject_type": "reimbursement_claim",
        "subject_id": "c1",
        "proposed_change": {"approve": True},
        "reasoning": "The receipt total matches the claimed amount exactly.",
        "citations": ("reimbursement_claims:c1",),
        "confidence": 0.91,
        "created_at": NOW,
        "previous_state": {"status": "pending"},
    }
    return proposals.Proposal(**(base | overrides))


def test_a_proposal_with_no_reasoning_is_refused():
    with pytest.raises(AutonomyError) as exc:
        proposals.assert_evaluable(_proposal(reasoning="   "))
    assert "approval step becomes theatre" in str(exc.value)


def test_a_proposal_with_no_citations_is_refused():
    with pytest.raises(AutonomyError) as exc:
        proposals.assert_evaluable(_proposal(citations=()))
    assert "able to disagree" in str(exc.value)


def test_a_reversible_proposal_with_no_previous_state_is_refused():
    with pytest.raises(AutonomyError) as exc:
        proposals.assert_evaluable(_proposal(previous_state={}))
    assert "reconstruction, not a" in str(exc.value)


def test_a_complete_proposal_passes():
    proposals.assert_evaluable(_proposal())


def test_a_confidence_outside_the_range_is_refused():
    with pytest.raises(AutonomyError):
        proposals.assert_evaluable(_proposal(confidence=1.4))


def test_expiry_is_shorter_for_higher_impact():
    critical = proposals.EXPIRY_WINDOWS[ImpactLevel.CRITICAL]
    low = proposals.EXPIRY_WINDOWS[ImpactLevel.LOW]
    assert critical < low


def test_an_expired_proposal_cannot_be_approved():
    """The whole reason expiry exists — applying it late is wrong, not untidy."""
    stale = _proposal(created_at=NOW - timedelta(days=40))
    with pytest.raises(AutonomyError) as exc:
        proposals.decide(stale, approve=True, now=NOW)
    assert "may since have changed" in str(exc.value)


def test_a_fresh_proposal_can_be_approved():
    decided = proposals.decide(
        _proposal(created_at=NOW - timedelta(days=2)), approve=True, now=NOW
    )
    assert decided.status is proposals.ProposalStatus.APPROVED


def test_a_rejection_needs_a_note():
    with pytest.raises(AutonomyError) as exc:
        proposals.decide(_proposal(), approve=False, now=NOW)
    assert "defective agent" in str(exc.value)


def test_a_rejection_with_a_note_is_recorded():
    decided = proposals.decide(
        _proposal(), approve=False, now=NOW, note="The receipt is for a duplicate."
    )
    assert decided.status is proposals.ProposalStatus.REJECTED
    assert decided.decision_note


def test_a_decided_proposal_cannot_be_decided_again():
    decided = proposals.decide(_proposal(), approve=True, now=NOW)
    with pytest.raises(AutonomyError) as exc:
        proposals.decide(decided, approve=False, now=NOW, note="changed my mind")
    assert "second outcome for one decision" in str(exc.value)


def test_expiring_a_batch_separates_live_from_lapsed():
    live_one = _proposal(created_at=NOW - timedelta(days=1))
    lapsed = _proposal(created_at=NOW - timedelta(days=40))
    already = _proposal(status=proposals.ProposalStatus.APPROVED)

    live, expired = proposals.expire_due([live_one, lapsed, already], now=NOW)
    assert live == [live_one]
    assert expired == [lapsed]


def test_a_critical_proposal_expires_sooner_than_a_low_one():
    critical = proposals.expires_at(
        "compliance_monitor.apply_payroll_rule_change", created_at=NOW
    )
    low = proposals.expires_at(
        "attendance.regularise_missing_punch", created_at=NOW
    )
    assert critical < low


# ══════════════════════════════════════════════════════════════════════════════
# The rejection-rate signal
# ══════════════════════════════════════════════════════════════════════════════


def _statuses(**counts):
    out = []
    for name, count in counts.items():
        out.extend([proposals.ProposalStatus(name)] * count)
    return out


def test_a_high_rejection_rate_reads_as_a_defective_agent():
    stats = proposals.stats_for(
        "expense_extraction.approve_claim", _statuses(rejected=9, approved=1)
    )
    assert stats.rejection_rate == pytest.approx(0.9)
    assert stats.is_defective_signal
    assert "renewal" in stats.as_dict()["note"]


def test_a_healthy_rate_is_not_flagged():
    stats = proposals.stats_for(
        "expense_extraction.approve_claim", _statuses(approved=18, rejected=2)
    )
    assert not stats.is_defective_signal
    assert stats.as_dict()["note"] == ""


def test_expiries_are_excluded_from_the_rejection_rate():
    """An unworked queue and a bad agent need opposite responses."""
    stats = proposals.stats_for(
        "expense_extraction.approve_claim",
        _statuses(approved=8, rejected=2, expired=40),
    )
    assert stats.decided == 10
    assert stats.rejection_rate == pytest.approx(0.2)
    assert stats.expired == 40
    assert not stats.is_defective_signal


def test_a_small_sample_produces_no_rate():
    """Three rejections out of three is noise, not a signal."""
    stats = proposals.stats_for(
        "expense_extraction.approve_claim", _statuses(rejected=3)
    )
    assert stats.rejection_rate is None
    assert not stats.is_defective_signal


def test_a_high_revert_rate_also_reads_as_defective():
    stats = proposals.stats_for(
        "attendance.regularise_missing_punch",
        _statuses(auto_applied=10, reverted=4),
    )
    assert stats.revert_rate == pytest.approx(0.4)
    assert stats.is_defective_signal


def test_only_human_decisions_count_toward_the_rate():
    for statusuvalue in proposals.ProposalStatus:
        expected = statusuvalue in (
            proposals.ProposalStatus.APPROVED,
            proposals.ProposalStatus.REJECTED,
        )
        assert statusuvalue.counts_toward_rejection_rate is expected


def test_stats_for_an_unknown_action_is_refused():
    with pytest.raises(AutonomyError):
        proposals.stats_for("nonsense.key", [])
