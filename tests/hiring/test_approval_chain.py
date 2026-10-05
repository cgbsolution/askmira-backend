"""A junior backfill and a senior new position are not the same decision."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.hiring.approval_chain import (
    ApprovalChainError,
    RuleSpec,
    resolve_chain,
    validate_rules,
)

L = Decimal


def _band(rule_id, sequence, role, lo, hi=None, **kwargs) -> RuleSpec:
    return RuleSpec(
        rule_id=rule_id,
        sequence=sequence,
        approver_role=role,
        ctc_min=L(lo),
        ctc_max=None if hi is None else L(hi),
        **kwargs,
    )


def _complete() -> list[RuleSpec]:
    """A rule set that covers every CTC, with a band step on top."""
    return [
        _band("r1", 1, "hiring_manager", 0, 1500000),
        _band("r2", 2, "admin", 1500000, 3000000),
        _band("r3", 3, "admin", 3000000),
        _band("r4", 4, "finance_head", 0, only_when_over_band=True, label="Finance"),
    ]


# ═══════════════════════════════════════════════════════════════════════════
# Validation — a gap is an offer released on nobody's signature
# ═══════════════════════════════════════════════════════════════════════════


def test_an_empty_rule_set_is_refused() -> None:
    with pytest.raises(ApprovalChainError, match="at least one rule"):
        validate_rules([])


def test_a_gap_between_bands_is_refused() -> None:
    """An offer at exactly ₹15,00,001 would resolve to no chain at all."""
    rules = [
        _band("r1", 1, "hiring_manager", 0, 1500000),
        _band("r2", 2, "admin", 2000000),
    ]
    with pytest.raises(ApprovalChainError, match="Nothing covers CTC between"):
        validate_rules(rules)


def test_a_capped_top_band_is_refused() -> None:
    """The offers above the top band are exactly the ones that need a chain."""
    rules = [
        _band("r1", 1, "hiring_manager", 0, 1500000),
        _band("r2", 2, "admin", 1500000, 3000000),
    ]
    with pytest.raises(ApprovalChainError, match="resolves to no approver"):
        validate_rules(rules)


def test_bands_that_do_not_start_at_zero_are_refused() -> None:
    rules = [_band("r1", 1, "admin", 500000)]
    with pytest.raises(ApprovalChainError, match="An offer below that has no approver"):
        validate_rules(rules)


def test_a_rule_set_where_every_rule_is_conditional_is_refused() -> None:
    """Grade-scoped and over-band rules cannot be the whole chain: an offer
    matching neither would resolve to nothing and release unapproved."""
    rules = [
        _band("r1", 1, "admin", 0, grades=("L5",)),
        _band("r2", 2, "finance_head", 0, only_when_over_band=True),
    ]
    with pytest.raises(ApprovalChainError, match="Every rule is conditional"):
        validate_rules(rules)


def test_duplicate_sequence_numbers_are_refused() -> None:
    rules = _complete()
    rules.append(_band("r5", 2, "cfo", 0))
    with pytest.raises(ApprovalChainError, match="used more than once"):
        validate_rules(rules)


def test_a_complete_rule_set_validates() -> None:
    validate_rules(_complete())


def test_overlapping_bands_are_allowed() -> None:
    """Two rules that both cover the same CTC is not a gap. Both fire, which is
    the point — a senior offer can need two signatures."""
    rules = [
        _band("r1", 1, "hiring_manager", 0),
        _band("r2", 2, "admin", 1500000),
    ]
    validate_rules(rules)
    chain = resolve_chain(rules, ctc_annual=L("2000000"))
    assert [s.approver_role for s in chain] == ["hiring_manager", "admin"]


# ═══════════════════════════════════════════════════════════════════════════
# Resolution
# ═══════════════════════════════════════════════════════════════════════════


def test_a_junior_offer_needs_one_signature() -> None:
    chain = resolve_chain(_complete(), ctc_annual=L("900000"), grade="L2")
    assert [s.approver_role for s in chain] == ["hiring_manager"]


def test_a_senior_offer_reaches_a_different_approver() -> None:
    chain = resolve_chain(_complete(), ctc_annual=L("4000000"), grade="L7")
    assert [s.approver_role for s in chain] == ["admin"]


def test_breaking_the_band_adds_a_person_rather_than_a_colour() -> None:
    """A step is somebody who has to look. A warning is a thing that gets
    closed."""
    inside = resolve_chain(_complete(), ctc_annual=L("2000000"), over_band=False)
    outside = resolve_chain(_complete(), ctc_annual=L("2000000"), over_band=True)
    assert [s.approver_role for s in inside] == ["admin"]
    assert [s.approver_role for s in outside] == ["admin", "finance_head"]
    assert "exceeds the band" in outside[-1].reason


def test_the_band_is_half_open_at_its_ceiling() -> None:
    """15,00,000 belongs to exactly one band, and it is the upper one.

    Half-open is what stops the boundary belonging to two bands or to neither,
    which every band scheme gets wrong at least once.
    """
    assert [s.approver_role for s in resolve_chain(_complete(), ctc_annual=L("1499999"))] == [
        "hiring_manager"
    ]
    assert [s.approver_role for s in resolve_chain(_complete(), ctc_annual=L("1500000"))] == [
        "admin"
    ]


def test_a_grade_scoped_rule_only_fires_for_that_grade() -> None:
    rules = _complete()
    rules.append(_band("r5", 5, "founder", 0, grades=("L8", "L9"), label="Founder"))
    assert [s.approver_role for s in resolve_chain(rules, ctc_annual=L("900000"), grade="L2")] == [
        "hiring_manager"
    ]
    assert [s.approver_role for s in resolve_chain(rules, ctc_annual=L("900000"), grade="L8")] == [
        "hiring_manager",
        "founder",
    ]


def test_the_same_role_reached_twice_signs_once() -> None:
    """Asking the CFO to approve the same offer twice teaches them the second
    request is noise."""
    rules = [
        _band("r1", 1, "cfo", 0),
        _band("r2", 2, "cfo", 0, grades=("L5",)),
        _band("r3", 3, "admin", 0),
    ]
    chain = resolve_chain(rules, ctc_annual=L("1000000"), grade="L5")
    assert [s.approver_role for s in chain] == ["cfo", "admin"]


def test_the_chain_is_ordered_by_sequence_not_by_rule_order() -> None:
    rules = [
        _band("r3", 30, "admin", 0),
        _band("r1", 10, "hiring_manager", 0),
        _band("r2", 20, "sub_admin", 0),
    ]
    chain = resolve_chain(rules, ctc_annual=L("1000000"))
    assert [s.approver_role for s in chain] == ["hiring_manager", "sub_admin", "admin"]


def test_the_same_inputs_resolve_the_same_chain_every_time() -> None:
    """Storage order must not decide who signs."""
    rules = _complete()
    first = [s.rule_id for s in resolve_chain(rules, ctc_annual=L("2000000"), over_band=True)]
    second = [
        s.rule_id
        for s in resolve_chain(list(reversed(rules)), ctc_annual=L("2000000"), over_band=True)
    ]
    assert first == second


def test_a_ctc_matching_no_rule_raises_rather_than_returning_nothing() -> None:
    """Releasing on no signature is not a fallback."""
    rules = [_band("r1", 1, "admin", 0, grades=("L5",))]
    with pytest.raises(ApprovalChainError, match="Releasing on no signature"):
        resolve_chain(rules, ctc_annual=L("900000"), grade="L2")


def test_each_step_explains_why_it_is_in_the_chain() -> None:
    """"You are step 3 of 4" tells an approver nothing."""
    chain = resolve_chain(_complete(), ctc_annual=L("2000000"), over_band=True)
    assert all(step.reason for step in chain)
    assert "CTC band" in chain[0].reason


def test_a_float_ctc_is_refused() -> None:
    with pytest.raises(ApprovalChainError, match="Decimal"):
        resolve_chain(_complete(), ctc_annual=2000000.0)
