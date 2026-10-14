"""Slice 27 — the eval harness against a real database.

The checklist gates:

- suites exist for every agent named in the brief, backfilled
- the original Fit Score defect is a regression case that fails the old logic
- discrimination assertions on every scoring agent
- a regression failure blocks — verified by asking the gate
- drift alerting tested with a synthetic drift
- cost and latency recorded per run
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.models.agenteval import (
    AgentFeedback,
    EvalCaseRow,
    EvalGateOutcome,
    EvalResultRow,
    EvalRunRow,
    EvalSuiteRow,
    EvalTrigger,
)
from app.services.agenteval import service as evals
from app.services.agenteval.suites import SUITES, EvalCase, EvalSuite, suite_for
from tests.integration.conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]


# ══════════════════════════════════════════════════════════════════════════════
# Gate: suites backfilled for every agent named in the brief
# ══════════════════════════════════════════════════════════════════════════════


async def test_the_canonical_suites_register_for_every_named_agent(
    session, hr_user
):
    written = await evals.seed_canonical_suites(session, hr_user)
    assert set(written) == set(SUITES)

    for expected in (
        "fit_score",
        "proctoring",
        "retention_risk",
        "compliance_monitor",
        "expense_extraction",
    ):
        assert expected in written, (
            f"{expected} was named in the brief as an agent to backfill and has "
            "no suite."
        )

    # Every reference agent is covered. The production agents that call the
    # live model have no suite yet, and the coverage view lists them as such
    # rather than presenting them as nothing to do.
    coverage = await evals.coverage(session, hr_user)
    assert coverage["agents_without_suites"] == sorted(evals.PRODUCTION_AGENTS)
    assert "none of them has an eval suite" in coverage["not_built"]


async def test_seeding_is_idempotent(session, hr_user):
    await evals.seed_canonical_suites(session, hr_user)
    await evals.seed_canonical_suites(session, hr_user)

    suites = (
        await session.execute(select(func.count()).select_from(EvalSuiteRow))
    ).scalar_one()
    cases = (
        await session.execute(select(func.count()).select_from(EvalCaseRow))
    ).scalar_one()
    assert suites == len(SUITES)
    assert cases == sum(len(s.cases) for s in SUITES.values())


async def test_a_scoring_suite_without_discrimination_is_refused_at_write_time(
    session, hr_user
):
    """The gate that would have prevented the incident, enforced on write."""
    from app.services.agenteval.assertions import AssertionKind, AssertionSpec

    bad = EvalSuite(
        agent_key="some_new_scorer",
        name="A new scorer",
        description="",
        version=1,
        is_scoring_agent=True,
        cases=(
            EvalCase(
                name="in_range",
                inputs=({"x": 1},),
                assertions=(
                    AssertionSpec(
                        kind=AssertionKind.NUMERIC_RANGE,
                        config={"min": 0, "max": 100},
                        rationale="range",
                    ),
                ),
            ),
        ),
    )
    with pytest.raises(HTTPException) as exc:
        await evals.register_suite(session, hr_user, suite=bad)
    assert "reached a benchmark" in exc.value.detail

    count = (
        await session.execute(select(func.count()).select_from(EvalSuiteRow))
    ).scalar_one()
    assert count == 0, "A refused suite was still written."


async def test_suites_are_scoped_to_their_tenant(session, hr_user, other_hr_user):
    await evals.seed_canonical_suites(session, hr_user)
    theirs = await evals.coverage(session, other_hr_user)
    assert theirs["suites"] == []
    assert "fit_score" in theirs["agents_without_suites"]


# ══════════════════════════════════════════════════════════════════════════════
# Gate: the Fit Score regression case blocks against the old logic
# ══════════════════════════════════════════════════════════════════════════════


async def test_the_fit_score_defect_blocks_a_merge(session, hr_user):
    """The whole slice, end to end, against a real run record."""
    await evals.seed_canonical_suites(session, hr_user)

    run = await evals.execute_suite(
        session,
        hr_user,
        agent_key="fit_score",
        triggered_by=EvalTrigger.CI,
        agent_override="fit_score_defective",
    )

    assert run.gate_outcome is EvalGateOutcome.BLOCK
    assert run.fail_count >= 1
    assert any("INC-FITSCORE-01" in r for r in run.blocking_reasons), (
        "The block did not cite the incident the regression case came from."
    )

    results = (
        (
            await session.execute(
                select(EvalResultRow).where(
                    EvalResultRow.case_name == "fit_score_discrimination"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(results) == 1
    failed = results[0]
    assert not failed.passed
    assert failed.is_regression_case
    assert failed.source_incident_id == "INC-FITSCORE-01"
    # The measured spread is stored, not just the verdict.
    assert failed.measurements["discrimination.spread"] < 2


async def test_the_corrected_fit_score_passes_the_same_suite(session, hr_user):
    await evals.seed_canonical_suites(session, hr_user)
    run = await evals.execute_suite(
        session, hr_user, agent_key="fit_score", triggered_by=EvalTrigger.CI
    )
    assert run.gate_outcome is EvalGateOutcome.PASS
    assert run.fail_count == 0
    assert run.metrics["fit_score_discrimination.discrimination.spread"] > 15


async def test_the_two_runs_have_different_fingerprints(session, hr_user):
    """A run has to be able to say what it ran against."""
    await evals.seed_canonical_suites(session, hr_user)
    bad = await evals.execute_suite(
        session,
        hr_user,
        agent_key="fit_score",
        agent_override="fit_score_defective",
    )
    good = await evals.execute_suite(session, hr_user, agent_key="fit_score")
    assert bad.agent_fingerprint != good.agent_fingerprint


async def test_every_backfilled_suite_passes_its_reference_implementation(
    session, hr_user
):
    await evals.seed_canonical_suites(session, hr_user)
    for agent_key in SUITES:
        run = await evals.execute_suite(session, hr_user, agent_key=agent_key)
        assert run.gate_outcome is EvalGateOutcome.PASS, (
            f"{agent_key}: {run.blocking_reasons}"
        )


async def test_running_an_unregistered_agent_is_refused(session, hr_user):
    with pytest.raises(HTTPException) as exc:
        await evals.execute_suite(session, hr_user, agent_key="fit_score")
    assert exc.value.status_code == 404
    assert "Seed the canonical suites" in exc.value.detail


async def test_an_unknown_implementation_is_refused(session, hr_user):
    await evals.seed_canonical_suites(session, hr_user)
    with pytest.raises(HTTPException) as exc:
        await evals.execute_suite(
            session,
            hr_user,
            agent_key="fit_score",
            agent_override="does_not_exist",
        )
    assert exc.value.status_code == 400


# ══════════════════════════════════════════════════════════════════════════════
# Gate: cost, latency and the diff
# ══════════════════════════════════════════════════════════════════════════════


async def test_a_run_records_cost_latency_and_every_measurement(session, hr_user):
    await evals.seed_canonical_suites(session, hr_user)
    run = await evals.execute_suite(session, hr_user, agent_key="fit_score")

    assert float(run.total_latency_ms) >= 0
    assert run.total_tokens == 0  # No cost function supplied; not estimated.
    assert run.metrics, "the run stored no measurements"

    results = (
        (
            await session.execute(
                select(EvalResultRow).where(EvalResultRow.run_id == run.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(results) == len(suite_for("fit_score").cases)
    assert all(float(r.latency_ms) >= 0 for r in results)


async def test_the_second_run_diffs_against_the_first(session, hr_user):
    """A degradation between two runs shows up as newly failing."""
    await evals.seed_canonical_suites(session, hr_user)
    await evals.execute_suite(session, hr_user, agent_key="fit_score")
    second = await evals.execute_suite(
        session,
        hr_user,
        agent_key="fit_score",
        agent_override="fit_score_defective",
    )

    diff = second.diff_from_previous
    assert diff, "the second run recorded no diff against the first"
    assert "fit_score_discrimination" in diff["newly_failing"], (
        "A case that passed and now fails was not reported as newly failing."
    )


async def test_the_run_history_shows_whether_each_run_allowed_a_merge(
    session, hr_user
):
    await evals.seed_canonical_suites(session, hr_user)
    await evals.execute_suite(
        session,
        hr_user,
        agent_key="fit_score",
        agent_override="fit_score_defective",
    )
    await evals.execute_suite(session, hr_user, agent_key="fit_score")

    history = await evals.run_history(session, hr_user, agent_key="fit_score")
    assert len(history["runs"]) == 2
    # Newest first.
    assert history["runs"][0]["allows_merge"] is True
    assert history["runs"][1]["allows_merge"] is False
    assert history["runs"][1]["blocking_reasons"]


async def test_a_run_is_scoped_to_its_tenant(session, hr_user, other_hr_user):
    await evals.seed_canonical_suites(session, hr_user)
    await evals.execute_suite(session, hr_user, agent_key="fit_score")

    theirs = await evals.run_history(
        session, other_hr_user, agent_key="fit_score"
    )
    assert theirs["runs"] == []


# ══════════════════════════════════════════════════════════════════════════════
# Gate: feedback becomes cases
# ══════════════════════════════════════════════════════════════════════════════


async def test_feedback_can_be_promoted_into_a_regression_case(session, hr_user):
    """How the suite stops being what somebody imagined could go wrong."""
    await evals.seed_canonical_suites(session, hr_user)

    feedback = await evals.capture_feedback(
        session,
        hr_user,
        agent_key="fit_score",
        agent_input={"candidate": "Z", "round_scores": [95, 94, 96, 95]},
        agent_output={"score": 72.1},
        was_useful=False,
        comment="Scored a clearly outstanding candidate the same as an average one.",
    )
    assert feedback.promoted_to_case_id is None

    case = await evals.promote_feedback_to_case(
        session,
        hr_user,
        feedback_id=feedback.id,
        case_name="fit_score_outstanding_candidate",
        assertions=[
            {
                "kind": "numeric_range",
                "config": {"path": "score", "min": 80},
                "rationale": "From INC-2 — an outstanding candidate scored 72.",
            }
        ],
        source_incident_id="INC-2",
    )
    assert case.is_regression_case
    assert case.source_incident_id == "INC-2"
    assert case.inputs == [feedback.agent_input]

    await session.refresh(feedback)
    assert feedback.promoted_to_case_id == case.id

    suite_row = (
        await session.execute(
            select(EvalSuiteRow).where(EvalSuiteRow.agent_key == "fit_score")
        )
    ).scalar_one()
    assert suite_row.version > suite_for("fit_score").version, (
        "Promoting a case did not bump the suite version, so a run cannot say "
        "which set of cases it ran."
    )


async def test_a_promoted_case_runs_and_can_block(session, hr_user):
    """The promoted case is not decoration: it gates the next run."""
    await evals.seed_canonical_suites(session, hr_user)
    feedback = await evals.capture_feedback(
        session,
        hr_user,
        agent_key="fit_score",
        agent_input={"candidate": "Z", "round_scores": [95, 94, 96, 95]},
        agent_output={"score": 72.1},
        was_useful=False,
    )
    await evals.promote_feedback_to_case(
        session,
        hr_user,
        feedback_id=feedback.id,
        case_name="fit_score_outstanding_candidate",
        assertions=[
            {
                "kind": "numeric_range",
                "config": {"path": "score", "min": 80},
                "rationale": "From INC-2.",
            }
        ],
        source_incident_id="INC-2",
    )

    blocked = await evals.execute_suite(
        session,
        hr_user,
        agent_key="fit_score",
        agent_override="fit_score_defective",
    )
    names = {
        r.case_name
        for r in (
            await session.execute(
                select(EvalResultRow).where(EvalResultRow.run_id == blocked.id)
            )
        )
        .scalars()
        .all()
    }
    assert "fit_score_outstanding_candidate" in names
    assert blocked.gate_outcome is EvalGateOutcome.BLOCK

    passing = await evals.execute_suite(session, hr_user, agent_key="fit_score")
    # WARN rather than PASS: every case passes, and the diff against the blocked
    # run reports the discrimination spread moving from 1.84 to 34. That is the
    # gate doing its job — the run allows a merge and says what changed.
    assert passing.gate_outcome is not EvalGateOutcome.BLOCK
    assert passing.fail_count == 0


async def test_promoting_a_case_with_no_assertions_is_refused(session, hr_user):
    await evals.seed_canonical_suites(session, hr_user)
    feedback = await evals.capture_feedback(
        session,
        hr_user,
        agent_key="fit_score",
        agent_input={"round_scores": [90]},
        agent_output={"score": 72},
        was_useful=False,
    )
    with pytest.raises(HTTPException) as exc:
        await evals.promote_feedback_to_case(
            session,
            hr_user,
            feedback_id=feedback.id,
            case_name="empty",
            assertions=[],
        )
    assert "looks like coverage" in exc.value.detail


async def test_feedback_is_scoped_to_its_tenant(session, hr_user, other_hr_user):
    await evals.seed_canonical_suites(session, hr_user)
    await evals.seed_canonical_suites(session, other_hr_user)
    feedback = await evals.capture_feedback(
        session,
        hr_user,
        agent_key="fit_score",
        agent_input={},
        agent_output={},
        was_useful=False,
    )
    with pytest.raises(HTTPException) as exc:
        await evals.promote_feedback_to_case(
            session,
            other_hr_user,
            feedback_id=feedback.id,
            case_name="x",
            assertions=[{"kind": "exact", "config": {"expected": 1}}],
        )
    assert exc.value.status_code == 404


async def test_feedback_rows_survive_and_are_countable(session, hr_user):
    for useful in (True, False, False):
        await evals.capture_feedback(
            session,
            hr_user,
            agent_key="retention_risk",
            agent_input={},
            agent_output={},
            was_useful=useful,
        )
    count = (
        await session.execute(select(func.count()).select_from(AgentFeedback))
    ).scalar_one()
    assert count == 3


# ══════════════════════════════════════════════════════════════════════════════
# Gate: a stored case that cannot be parsed is not silently skipped
# ══════════════════════════════════════════════════════════════════════════════


async def test_a_stored_case_with_an_unknown_assertion_kind_is_refused(
    session, hr_user
):
    """A case that cannot be parsed would be a silent gap in the suite."""
    await evals.seed_canonical_suites(session, hr_user)
    suite_row = (
        await session.execute(
            select(EvalSuiteRow).where(EvalSuiteRow.agent_key == "fit_score")
        )
    ).scalar_one()
    session.add(
        EvalCaseRow(
            tenant_id=hr_user.tenant_id,
            suite_id=suite_row.id,
            name="broken",
            inputs=[{"round_scores": [50]}],
            assertions=[{"kind": "semantic_vibes", "config": {}}],
        )
    )
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await evals.execute_suite(session, hr_user, agent_key="fit_score")
    assert "silent gap" in exc.value.detail


async def test_the_run_row_records_the_suite_version_it_ran(session, hr_user):
    await evals.seed_canonical_suites(session, hr_user)
    run = await evals.execute_suite(session, hr_user, agent_key="fit_score")
    assert run.suite_version == suite_for("fit_score").version

    runs = (
        (await session.execute(select(EvalRunRow))).scalars().all()
    )
    assert len(runs) == 1
    assert runs[0].completed_at is not None
