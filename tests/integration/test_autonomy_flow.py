"""Slice 29 — human-in-the-loop against a real database.

The checklist gates:

- protected actions provably cannot be auto-applied by any configuration path
- `reversible = false` implies max PROPOSE_FOR_APPROVAL, validated
- every proposal carries reasoning and verifiable citations
- revert tested on each reversible action type
- the consolidated queue covers every agent — none has a private one
- a high rejection rate surfaces as a product signal
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.models.autonomy import (
    AgentAutonomyConfig,
    AgentProposal,
    AutonomyLevelEnum,
    ProposalReversal,
    ProposalStatusEnum,
)
from app.services.autonomy import service as autonomy
from app.services.autonomy.catalogue import (
    ACTIONS,
    PROTECTED_KEYS,
    AutonomyLevel,
)
from tests.integration.conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]

NOW = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)

REVERSIBLE_ACTIONS = tuple(sorted(key for key, spec in ACTIONS.items() if spec.reversible))


async def _propose(session, user, *, action_key, **overrides):
    payload = {
        "action_key": action_key,
        "subject_type": "employee",
        "subject_id": uuid.uuid4(),
        "proposed_change": {"field": "value"},
        "previous_state": {"field": "old"},
        "reasoning": "The record supports this change.",
        "citations": ("employees:e1",),
        "confidence": 0.88,
        "now": NOW,
    }
    payload.update(overrides)
    return await autonomy.propose(session, user, **payload)


# ══════════════════════════════════════════════════════════════════════════════
# Gate: the platform ceiling cannot be exceeded by any configuration path
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("action_key", sorted(PROTECTED_KEYS))
async def test_a_protected_action_cannot_be_configured_to_auto_apply(session, hr_user, action_key):
    """Tested per protected action, through the service."""
    for level in (
        AutonomyLevel.AUTO_APPLY_WITH_NOTIFY,
        AutonomyLevel.AUTO_APPLY_SILENT,
    ):
        with pytest.raises(HTTPException) as exc:
            await autonomy.configure(
                session, hr_user, action_key=action_key, autonomy=level, now=NOW
            )
        assert exc.value.status_code == 400
        assert exc.value.detail.strip()

    count = (
        await session.execute(select(func.count()).select_from(AgentAutonomyConfig))
    ).scalar_one()
    assert count == 0, "A refused configuration still wrote a row."


async def test_a_protected_action_stays_manual_even_with_a_row_written_directly(session, hr_user):
    """The ceiling is enforced on read as well as on write.

    A row written by a migration, a seed script or a direct UPDATE must not
    produce an automatic disbursement.
    """
    session.add(
        AgentAutonomyConfig(
            tenant_id=hr_user.tenant_id,
            action_key="disbursement.release_payment_file",
            agent_key="disbursement",
            autonomy=AutonomyLevelEnum.AUTO_APPLY_SILENT,
            max_autonomy_at_time=AutonomyLevelEnum.AUTO_APPLY_SILENT,
            configured_at=NOW,
        )
    )
    await session.commit()

    level, _threshold, note = await autonomy.effective_autonomy(
        session, hr_user, action_key="disbursement.release_payment_file"
    )
    assert not level.applies_automatically, (
        "A directly-written config row made a disbursement release automatic."
    )
    assert "capped" in note

    proposal = await _propose(
        session,
        hr_user,
        action_key="disbursement.release_payment_file",
        previous_state={},
    )
    assert proposal.status is ProposalStatusEnum.PENDING
    assert proposal.applied_at is None


async def test_your_voice_cannot_even_produce_a_proposal(session, hr_user):
    with pytest.raises(HTTPException) as exc:
        await _propose(session, hr_user, action_key="your_voice.act_on_case", previous_state={})
    assert "does not create proposals" in exc.value.detail


async def test_a_reversible_low_impact_action_may_be_configured_silent(session, hr_user):
    row = await autonomy.configure(
        session,
        hr_user,
        action_key="attendance.regularise_missing_punch",
        autonomy=AutonomyLevel.AUTO_APPLY_SILENT,
        now=NOW,
    )
    assert row.autonomy is AutonomyLevelEnum.AUTO_APPLY_SILENT
    assert row.max_autonomy_at_time is AutonomyLevelEnum.AUTO_APPLY_SILENT


async def test_the_configuration_view_shows_defaults_for_unconfigured_actions(session, hr_user):
    listing = await autonomy.list_configuration(session, hr_user)
    assert len(listing["actions"]) == len(ACTIONS)
    assert all(not a["is_tenant_configured"] for a in listing["actions"])
    assert {a["key"] for a in listing["protected"]} == PROTECTED_KEYS


async def test_configuration_is_scoped_to_its_tenant(session, hr_user, other_hr_user):
    await autonomy.configure(
        session,
        hr_user,
        action_key="attendance.regularise_missing_punch",
        autonomy=AutonomyLevel.AUTO_APPLY_SILENT,
        now=NOW,
    )
    theirs = await autonomy.list_configuration(session, other_hr_user)
    assert all(not a["is_tenant_configured"] for a in theirs["actions"])


# ══════════════════════════════════════════════════════════════════════════════
# Gate: threshold routing
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        ("1999", ProposalStatusEnum.AUTO_APPLIED),
        ("2000", ProposalStatusEnum.PENDING),
        ("2001", ProposalStatusEnum.PENDING),
    ],
)
async def test_threshold_routing_at_and_either_side_of_the_boundary(
    session, hr_user, amount, expected
):
    await autonomy.configure(
        session,
        hr_user,
        action_key="expense_extraction.approve_claim",
        autonomy=AutonomyLevel.AUTO_APPLY_WITH_NOTIFY,
        threshold_value=Decimal("2000"),
        now=NOW,
    )
    proposal = await _propose(
        session,
        hr_user,
        action_key="expense_extraction.approve_claim",
        magnitude=Decimal(amount),
    )
    assert proposal.status is expected
    assert proposal.routing_reason


async def test_the_threshold_unit_is_stored_beside_the_value(session, hr_user):
    row = await autonomy.configure(
        session,
        hr_user,
        action_key="expense_extraction.approve_claim",
        autonomy=AutonomyLevel.AUTO_APPLY_WITH_NOTIFY,
        threshold_value=Decimal("2000"),
        now=NOW,
    )
    assert row.threshold_unit == "INR", "A number with no unit is a number nobody can read."


# ══════════════════════════════════════════════════════════════════════════════
# Gate: every proposal is evaluable
# ══════════════════════════════════════════════════════════════════════════════


async def test_a_proposal_with_no_reasoning_is_refused(session, hr_user):
    with pytest.raises(HTTPException) as exc:
        await _propose(
            session,
            hr_user,
            action_key="expense_extraction.approve_claim",
            reasoning="   ",
        )
    assert "theatre" in exc.value.detail

    count = (await session.execute(select(func.count()).select_from(AgentProposal))).scalar_one()
    assert count == 0


async def test_a_proposal_with_no_citations_is_refused(session, hr_user):
    with pytest.raises(HTTPException) as exc:
        await _propose(
            session,
            hr_user,
            action_key="expense_extraction.approve_claim",
            citations=(),
        )
    assert "able to disagree" in exc.value.detail


async def test_a_reversible_proposal_with_no_previous_state_is_refused(session, hr_user):
    with pytest.raises(HTTPException) as exc:
        await _propose(
            session,
            hr_user,
            action_key="expense_extraction.approve_claim",
            previous_state={},
        )
    assert "reconstruction" in exc.value.detail


async def test_every_queued_proposal_carries_reasoning_and_citations(session, hr_user):
    await _propose(session, hr_user, action_key="expense_extraction.approve_claim")
    await _propose(session, hr_user, action_key="lifecycle.raise_confirmation")

    listing = await autonomy.queue(session, hr_user, now=NOW)
    assert listing["proposals"]
    for item in listing["proposals"]:
        assert item["reasoning"].strip()
        assert item["citations"]
        assert 0 <= item["confidence"] <= 1


# ══════════════════════════════════════════════════════════════════════════════
# Gate: one consolidated queue
# ══════════════════════════════════════════════════════════════════════════════


async def test_the_queue_covers_every_agent_that_proposed(session, hr_user):
    for key in (
        "expense_extraction.approve_claim",
        "lifecycle.raise_confirmation",
        "retention_risk.flag_to_manager",
        "fit_score.advance_candidate",
    ):
        await _propose(session, hr_user, action_key=key)

    listing = await autonomy.queue(session, hr_user, now=NOW)
    assert set(listing["agents_represented"]) == {
        "expense_extraction",
        "lifecycle",
        "retention_risk",
        "fit_score",
    }
    assert len(listing["proposals"]) == 4


async def test_the_queue_filters_by_agent_impact_and_age(session, hr_user):
    await _propose(session, hr_user, action_key="expense_extraction.approve_claim")
    await _propose(session, hr_user, action_key="retention_risk.flag_to_manager")

    by_agent = await autonomy.queue(session, hr_user, agent_key="retention_risk", now=NOW)
    assert len(by_agent["proposals"]) == 1

    by_impact = await autonomy.queue(session, hr_user, impact="high", now=NOW)
    assert {p["agent_key"] for p in by_impact["proposals"]} == {"retention_risk"}

    by_age = await autonomy.queue(session, hr_user, min_age_days=5, now=NOW + timedelta(days=6))
    assert len(by_age["proposals"]) == 2
    too_young = await autonomy.queue(session, hr_user, min_age_days=5, now=NOW)
    assert too_young["proposals"] == []


async def test_the_queue_is_scoped_to_its_tenant(session, hr_user, other_hr_user):
    await _propose(session, hr_user, action_key="expense_extraction.approve_claim")
    theirs = await autonomy.queue(session, other_hr_user, now=NOW)
    assert theirs["proposals"] == []


# ══════════════════════════════════════════════════════════════════════════════
# Gate: expiry never silently applies
# ══════════════════════════════════════════════════════════════════════════════


async def test_an_expired_proposal_cannot_be_approved(session, hr_user):
    proposal = await _propose(
        session,
        hr_user,
        action_key="expense_extraction.approve_claim",
        now=NOW - timedelta(days=60),
    )
    with pytest.raises(HTTPException) as exc:
        await autonomy.decide_proposal(
            session, hr_user, proposal_id=proposal.id, approve=True, now=NOW
        )
    assert "expired" in exc.value.detail

    await session.refresh(proposal)
    assert proposal.status is ProposalStatusEnum.PENDING
    assert proposal.applied_at is None


async def test_expiring_a_batch_marks_them_and_applies_nothing(session, hr_user):
    stale = await _propose(
        session,
        hr_user,
        action_key="expense_extraction.approve_claim",
        now=NOW - timedelta(days=60),
    )
    fresh = await _propose(session, hr_user, action_key="expense_extraction.approve_claim")

    result = await autonomy.expire_stale(session, hr_user, now=NOW)
    assert result["expired"] == 1

    await session.refresh(stale)
    await session.refresh(fresh)
    assert stale.status is ProposalStatusEnum.EXPIRED
    assert stale.applied_at is None
    assert fresh.status is ProposalStatusEnum.PENDING


async def test_a_critical_proposal_expires_sooner_than_a_low_one(session, hr_user):
    critical = await _propose(
        session,
        hr_user,
        action_key="compliance_monitor.apply_payroll_rule_change",
        previous_state={},
    )
    low = await _propose(session, hr_user, action_key="attendance.regularise_missing_punch")
    assert critical.expires_at < low.expires_at


# ══════════════════════════════════════════════════════════════════════════════
# Gate: reversal, on each reversible action type
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("action_key", REVERSIBLE_ACTIONS)
async def test_every_reversible_action_reverts_cleanly_with_an_audit(session, hr_user, action_key):
    """Tested per reversible action type, as the checklist asks."""
    proposal = await _propose(
        session,
        hr_user,
        action_key=action_key,
        previous_state={"status": "before"},
    )
    if proposal.status is ProposalStatusEnum.PENDING:
        await autonomy.decide_proposal(
            session, hr_user, proposal_id=proposal.id, approve=True, now=NOW
        )

    reversal = await autonomy.revert(
        session,
        hr_user,
        proposal_id=proposal.id,
        reason="Approved in error; the underlying record was already correct.",
        now=NOW,
    )
    assert reversal.restored_state == {"status": "before"}
    assert reversal.reverted_by_user_id == hr_user.id

    await session.refresh(proposal)
    assert proposal.status is ProposalStatusEnum.REVERTED

    from app.models.audit import AuditLog

    entries = (
        (await session.execute(select(AuditLog).where(AuditLog.entity_id == proposal.id)))
        .scalars()
        .all()
    )
    assert any("reverted" in (e.summary or "") for e in entries), (
        "A reversal produced no audit entry."
    )


async def test_an_irreversible_action_cannot_be_reverted(session, hr_user):
    proposal = await _propose(
        session,
        hr_user,
        action_key="compliance_monitor.apply_payroll_rule_change",
        previous_state={},
    )
    await autonomy.decide_proposal(session, hr_user, proposal_id=proposal.id, approve=True, now=NOW)
    with pytest.raises(HTTPException) as exc:
        await autonomy.revert(
            session,
            hr_user,
            proposal_id=proposal.id,
            reason="Wrong rate.",
            now=NOW,
        )
    assert "not a reversible action" in exc.value.detail


async def test_an_unapplied_proposal_cannot_be_reverted(session, hr_user):
    proposal = await _propose(session, hr_user, action_key="expense_extraction.approve_claim")
    with pytest.raises(HTTPException) as exc:
        await autonomy.revert(session, hr_user, proposal_id=proposal.id, reason="No.", now=NOW)
    assert "never applied" in exc.value.detail


async def test_a_reversal_cannot_be_repeated(session, hr_user):
    proposal = await _propose(session, hr_user, action_key="expense_extraction.approve_claim")
    await autonomy.decide_proposal(session, hr_user, proposal_id=proposal.id, approve=True, now=NOW)
    await autonomy.revert(session, hr_user, proposal_id=proposal.id, reason="First.", now=NOW)
    with pytest.raises(HTTPException) as exc:
        await autonomy.revert(session, hr_user, proposal_id=proposal.id, reason="Again.", now=NOW)
    assert "already been reverted" in exc.value.detail

    count = (await session.execute(select(func.count()).select_from(ProposalReversal))).scalar_one()
    assert count == 1


async def test_a_reversal_needs_a_reason(session, hr_user):
    proposal = await _propose(session, hr_user, action_key="expense_extraction.approve_claim")
    await autonomy.decide_proposal(session, hr_user, proposal_id=proposal.id, approve=True, now=NOW)
    with pytest.raises(HTTPException) as exc:
        await autonomy.revert(session, hr_user, proposal_id=proposal.id, reason="  ", now=NOW)
    assert "somebody will ask" in exc.value.detail


# ══════════════════════════════════════════════════════════════════════════════
# Gate: the rejection rate surfaces as a product signal
# ══════════════════════════════════════════════════════════════════════════════


async def test_a_high_rejection_rate_surfaces_as_defective(session, hr_user):
    for index in range(10):
        proposal = await _propose(session, hr_user, action_key="fit_score.advance_candidate")
        await autonomy.decide_proposal(
            session,
            hr_user,
            proposal_id=proposal.id,
            approve=index == 0,
            note="" if index == 0 else "The candidate does not meet the bar.",
            now=NOW,
        )

    stats = await autonomy.decision_stats(session, hr_user)
    entry = next(s for s in stats["actions"] if s["action_key"] == "fit_score.advance_candidate")
    assert entry["rejection_rate"] == pytest.approx(0.9)
    assert entry["looks_defective"]
    assert "fit_score.advance_candidate" in stats["looks_defective"]


async def test_expiries_do_not_make_an_unworked_queue_look_defective(session, hr_user):
    for _ in range(20):
        await _propose(
            session,
            hr_user,
            action_key="expense_extraction.approve_claim",
            now=NOW - timedelta(days=60),
        )
    await autonomy.expire_stale(session, hr_user, now=NOW)

    for index in range(10):
        proposal = await _propose(session, hr_user, action_key="expense_extraction.approve_claim")
        await autonomy.decide_proposal(
            session,
            hr_user,
            proposal_id=proposal.id,
            approve=index < 8,
            note="" if index < 8 else "Duplicate receipt.",
            now=NOW,
        )

    stats = await autonomy.decision_stats(session, hr_user)
    entry = next(
        s for s in stats["actions"] if s["action_key"] == "expense_extraction.approve_claim"
    )
    assert entry["expired"] == 20
    assert entry["decided"] == 10
    assert entry["rejection_rate"] == pytest.approx(0.2)
    assert not entry["looks_defective"], "An unworked queue is being reported as a defective agent."


async def test_a_rejection_without_a_note_is_refused(session, hr_user):
    proposal = await _propose(session, hr_user, action_key="expense_extraction.approve_claim")
    with pytest.raises(HTTPException) as exc:
        await autonomy.decide_proposal(
            session, hr_user, proposal_id=proposal.id, approve=False, now=NOW
        )
    assert "defective agent" in exc.value.detail


# ══════════════════════════════════════════════════════════════════════════════
# Gate: the transparency surface
# ══════════════════════════════════════════════════════════════════════════════


async def test_the_transparency_surface_answers_the_security_review_question(session, hr_user):
    applied = await _propose(session, hr_user, action_key="expense_extraction.approve_claim")
    await autonomy.decide_proposal(session, hr_user, proposal_id=applied.id, approve=True, now=NOW)
    await autonomy.revert(session, hr_user, proposal_id=applied.id, reason="Duplicate.", now=NOW)
    await _propose(session, hr_user, action_key="lifecycle.raise_confirmation")

    surface = await autonomy.transparency_surface(session, hr_user, now=NOW)

    assert len(surface["permitted"]) == len(ACTIONS)
    assert {a["key"] for a in surface["protected"]} == PROTECTED_KEYS
    assert surface["activity"]["reverted"] == 1
    assert surface["activity"]["pending"] == 1
    assert surface["reversals_recorded"] == 1
    assert surface["pending_queue"]
    assert surface["not_built"], (
        "The transparency surface no longer lists what is not built. A page of "
        "green ticks is not an answer to a security review."
    )
    # Which agent creates proposals, and that the rest of the catalogue has none.
    assert any("probation agent" in gap for gap in surface["not_built"])
    assert any("no agent yet" in gap for gap in surface["not_built"])


async def test_the_transparency_surface_is_scoped_to_its_tenant(session, hr_user, other_hr_user):
    await _propose(session, hr_user, action_key="expense_extraction.approve_claim")
    theirs = await autonomy.transparency_surface(session, other_hr_user, now=NOW)
    assert theirs["activity"]["pending"] == 0
    assert theirs["pending_queue"] == []


async def test_an_auto_applied_proposal_records_when_it_was_applied(session, hr_user):
    await autonomy.configure(
        session,
        hr_user,
        action_key="attendance.regularise_missing_punch",
        autonomy=AutonomyLevel.AUTO_APPLY_SILENT,
        now=NOW,
    )
    proposal = await _propose(session, hr_user, action_key="attendance.regularise_missing_punch")
    assert proposal.status is ProposalStatusEnum.AUTO_APPLIED
    assert proposal.applied_at == NOW
    assert proposal.routing_reason
