"""Slice 27 — the eval harness.

The centre of this file is `test_the_suite_fails_the_original_fit_score_defect`.
Everything else exists to make that test trustworthy.
"""

from __future__ import annotations

import random

import pytest

from app.services.agenteval import drift, fixtures, gating, runner
from app.services.agenteval.assertions import (
    AssertionKind,
    AssertionSpec,
    MalformedAssertionError,
    check,
    kinds_without_handlers,
)
from app.services.agenteval.suites import SUITES, suite_for


def _spec(kind: AssertionKind, **config) -> AssertionSpec:
    return AssertionSpec(kind=kind, config=config, rationale="test")


# ══════════════════════════════════════════════════════════════════════════════
# The Fit Score defect — the reason this slice exists
# ══════════════════════════════════════════════════════════════════════════════


def test_the_suite_fails_the_original_fit_score_defect():
    """The regression case must fail against the old logic.

    A regression case that passes the defect it was written for is worse than no
    case: it is a green tick asserting the opposite of the truth.
    """
    report = runner.run_suite(
        suite_for("fit_score"),
        fixtures.fit_score_defective,
        agent_fingerprint="defective",
    )
    failed = {r.case_name for r in report.results if not r.passed}
    assert "fit_score_discrimination" in failed, (
        "The discrimination case passed against the implementation that shipped "
        "the defect. That case is the entire point of this slice."
    )

    decision = gating.evaluate_gate(report)
    assert decision.outcome is gating.GateOutcome.BLOCK
    assert not decision.outcome.allows_merge
    assert any(
        "INC-FITSCORE-01" in reason for reason in decision.blocking_reasons
    )


def test_the_suite_passes_the_corrected_fit_score():
    report = runner.run_suite(
        suite_for("fit_score"),
        fixtures.fit_score_working,
        agent_fingerprint="working",
    )
    assert report.fail_count == 0, [
        (r.case_name, r.failure_reason) for r in report.results if not r.passed
    ]
    assert gating.evaluate_gate(report).outcome is gating.GateOutcome.PASS


def test_the_defective_scorer_passes_every_per_output_check():
    """Why the defect got through: nothing wrong is visible one output at a time."""
    suite = suite_for("fit_score")
    report = runner.run_suite(
        suite, fixtures.fit_score_defective, agent_fingerprint="defective"
    )
    by_name = {r.case_name: r for r in report.results}

    assert by_name["fit_score_stays_in_range"].passed
    assert by_name["fit_score_carries_its_signals"].passed
    assert by_name["fit_score_monotonicity"].passed
    # And yet:
    assert not by_name["fit_score_discrimination"].passed


def test_the_discrimination_failure_names_the_measured_spread():
    report = runner.run_suite(
        suite_for("fit_score"),
        fixtures.fit_score_defective,
        agent_fingerprint="defective",
    )
    case = next(
        r for r in report.results if r.case_name == "fit_score_discrimination"
    )
    verdict = case.verdicts[0]
    assert verdict.measured["spread"] < 2
    assert verdict.measured["min_spread"] == 15
    assert "Fit Score failure" in verdict.reason


# ══════════════════════════════════════════════════════════════════════════════
# Assertions
# ══════════════════════════════════════════════════════════════════════════════


def test_every_assertion_kind_has_a_handler():
    assert kinds_without_handlers() == set()


def test_discrimination_needs_at_least_two_outputs():
    with pytest.raises(MalformedAssertionError) as exc:
        check(
            _spec(AssertionKind.DISCRIMINATION, path="score", min_spread=10),
            [{"score": 50}],
        )
    assert "pass vacuously" in str(exc.value)


def test_discrimination_catches_a_cluster_hidden_by_an_outlier():
    """A wide spread can hide four values the agent cannot tell apart."""
    spec = _spec(
        AssertionKind.DISCRIMINATION,
        path="score",
        min_spread=15,
        min_pairwise_gap=3,
    )
    outputs = [
        {"score": 10},
        {"score": 71},
        {"score": 71.5},
        {"score": 72},
        {"score": 72.2},
    ]
    verdict = check(spec, outputs)
    assert not verdict.passed
    assert "closest" in verdict.reason
    # The overall spread alone would have passed.
    assert verdict.measured["spread"] > 15


def test_discrimination_passes_a_genuinely_discriminating_agent():
    spec = _spec(AssertionKind.DISCRIMINATION, path="score", min_spread=15)
    outputs = [{"score": v} for v in (88, 71, 54, 63, 79)]
    assert check(spec, outputs).passed


def test_monotonicity_allows_ties_but_not_inversions():
    spec = _spec(AssertionKind.MONOTONICITY, path="score")
    assert check(spec, [{"score": 1}, {"score": 1}, {"score": 2}]).passed
    verdict = check(spec, [{"score": 3}, {"score": 1}])
    assert not verdict.passed
    assert "a better input scored worse" in verdict.reason


def test_consistency_catches_excessive_variance():
    spec = _spec(
        AssertionKind.CONSISTENCY, path="score", max_coefficient_of_variation=0.05
    )
    steady = [{"score": v} for v in (70, 71, 70, 69, 70)]
    assert check(spec, steady).passed

    erratic = [{"score": v} for v in (20, 80, 45, 95, 10)]
    verdict = check(spec, erratic)
    assert not verdict.passed
    assert verdict.measured["coefficient_of_variation"] > 0.05


def test_consistency_needs_at_least_three_runs():
    spec = _spec(
        AssertionKind.CONSISTENCY, path="score", max_coefficient_of_variation=0.1
    )
    with pytest.raises(MalformedAssertionError) as exc:
        check(spec, [{"score": 1}, {"score": 2}])
    assert "mostly noise" in str(exc.value)


def test_grounding_catches_a_citation_to_a_record_that_does_not_exist():
    spec = _spec(
        AssertionKind.GROUNDING,
        known_records={"filing:f1": True},
        require_citation=True,
    )
    verdict = check(spec, ["The return is overdue [[filing:f9]]"])
    assert not verdict.passed
    assert "do not exist" in verdict.reason


def test_grounding_catches_a_citation_that_does_not_support_the_claim():
    """The dangerous one: the id resolves, and says something else."""
    spec = _spec(
        AssertionKind.GROUNDING,
        known_records={"filing:f1": True},
        supporting_facts={"filing:f1": "2026-07-31"},
    )
    verdict = check(spec, ["The return was due on 2026-09-30 [[filing:f1]]"])
    assert not verdict.passed
    assert "do not support the claim" in verdict.reason


def test_grounding_passes_a_citation_that_holds():
    spec = _spec(
        AssertionKind.GROUNDING,
        known_records={"filing:f1": True},
        supporting_facts={"filing:f1": "2026-07-31"},
    )
    assert check(
        spec, ["Form 24Q was due on 2026-07-31 and is unfiled [[filing:f1]]"]
    ).passed


def test_grounding_catches_a_missing_citation():
    spec = _spec(AssertionKind.GROUNDING, known_records={}, require_citation=True)
    verdict = check(spec, ["The return is overdue."])
    assert not verdict.passed
    assert "no citation" in verdict.reason


def test_refusal_catches_a_refusal_that_leaks_the_answer():
    spec = _spec(
        AssertionKind.REFUSAL,
        must_not_contain=["4200000"],
    )
    verdict = check(
        spec, ["I cannot share salaries, but hers is around 4200000."]
    )
    assert not verdict.passed
    assert "leaks the answer" in verdict.reason


def test_refusal_passes_a_clean_decline():
    spec = _spec(AssertionKind.REFUSAL, must_not_contain=["4200000"])
    assert check(spec, ["I cannot share another employee's salary."]).passed


def test_refusal_catches_an_answer_that_did_not_decline():
    spec = _spec(AssertionKind.REFUSAL, must_not_contain=[])
    verdict = check(spec, ["Her salary is 42 lakh."])
    assert not verdict.passed
    assert "not declined" in verdict.reason


def test_a_missing_field_raises_rather_than_comparing_none_to_none():
    with pytest.raises(MalformedAssertionError) as exc:
        check(_spec(AssertionKind.EXACT, path="score", expected=None), [{}])
    assert "not a pass" in str(exc.value)


def test_structural_allows_extra_keys_but_not_missing_ones():
    spec = _spec(
        AssertionKind.STRUCTURAL, shape={"score": "number", "signals": "list"}
    )
    assert check(spec, [{"score": 1, "signals": [], "extra": True}]).passed
    verdict = check(spec, [{"score": 1}])
    assert not verdict.passed
    assert "signals" in verdict.reason


def test_structural_rejects_a_boolean_where_a_number_is_required():
    spec = _spec(AssertionKind.STRUCTURAL, shape={"score": "number"})
    verdict = check(spec, [{"score": True}])
    assert not verdict.passed
    assert "boolean" in verdict.reason


def test_numeric_range_with_neither_bound_is_refused():
    with pytest.raises(MalformedAssertionError) as exc:
        check(_spec(AssertionKind.NUMERIC_RANGE), [1])
    assert "not a check" in str(exc.value)


def test_a_judge_with_no_score_supplied_fails_rather_than_skipping():
    spec = _spec(AssertionKind.LLM_JUDGE, min_score=4, rubric="Is it clear?")
    verdict = check(spec, ["some text"])
    assert not verdict.passed
    assert "checked nothing" in verdict.reason


def test_a_judge_with_no_rubric_is_refused():
    with pytest.raises(MalformedAssertionError) as exc:
        check(_spec(AssertionKind.LLM_JUDGE, min_score=4), ["text"])
    assert "unfalsifiable" in str(exc.value)


def test_an_assertion_with_no_outputs_is_refused():
    with pytest.raises(MalformedAssertionError) as exc:
        check(_spec(AssertionKind.EXACT, expected=1), [])
    assert "must not be reported as passing" in str(exc.value)


# ══════════════════════════════════════════════════════════════════════════════
# Gating
# ══════════════════════════════════════════════════════════════════════════════


def test_every_registered_suite_is_defect_free():
    assert gating.all_suite_defects() == []


def test_a_scoring_suite_without_a_discrimination_assertion_is_refused():
    """The check that would have prevented the whole incident."""
    from app.services.agenteval.suites import EvalCase, EvalSuite

    suite = EvalSuite(
        agent_key="some_scorer",
        name="A scorer",
        description="",
        version=1,
        is_scoring_agent=True,
        cases=(
            EvalCase(
                name="in_range",
                inputs=({"x": 1},),
                assertions=(
                    _spec(AssertionKind.NUMERIC_RANGE, min=0, max=100),
                ),
            ),
        ),
    )
    defect = gating.missing_discrimination(suite)
    assert defect is not None
    assert "reached a benchmark" in defect.problem


def test_a_non_scoring_suite_needs_no_discrimination_assertion():
    assert gating.missing_discrimination(suite_for("compliance_monitor")) is None


def test_a_cross_input_assertion_with_one_input_is_a_suite_defect():
    from app.services.agenteval.suites import EvalCase, EvalSuite

    suite = EvalSuite(
        agent_key="x",
        name="x",
        description="",
        version=1,
        cases=(
            EvalCase(
                name="bad",
                inputs=({"a": 1},),
                assertions=(
                    _spec(
                        AssertionKind.DISCRIMINATION, path="score", min_spread=5
                    ),
                ),
            ),
        ),
    )
    problems = [d.problem for d in gating.suite_defects(suite)]
    assert any("pass vacuously" in p for p in problems)


def test_a_case_checked_only_by_a_judge_is_a_suite_defect():
    from app.services.agenteval.suites import EvalCase, EvalSuite

    suite = EvalSuite(
        agent_key="x",
        name="x",
        description="",
        version=1,
        cases=(
            EvalCase(
                name="judged",
                inputs=({"a": 1},),
                assertions=(
                    _spec(
                        AssertionKind.LLM_JUDGE, min_score=4, rubric="Is it good?"
                    ),
                ),
            ),
        ),
    )
    problems = [d.problem for d in gating.suite_defects(suite)]
    assert any("never the only assertion" in p for p in problems)


def test_an_empty_suite_is_a_defect():
    from app.services.agenteval.suites import EvalSuite

    suite = EvalSuite(agent_key="x", name="x", description="", version=1)
    problems = [d.problem for d in gating.suite_defects(suite)]
    assert any("looks like coverage" in p for p in problems)


def test_a_failing_regression_case_blocks_even_on_a_warning_kind():
    """A regression case blocks regardless of which assertion failed."""
    from app.services.agenteval.suites import EvalCase, EvalSuite

    suite = EvalSuite(
        agent_key="x",
        name="x",
        description="",
        version=1,
        cases=(
            EvalCase(
                name="regression",
                inputs=({"a": 1},),
                source_incident="INC-1",
                assertions=(
                    AssertionSpec(
                        kind=AssertionKind.CONTAINS,
                        config={"values": ["never present"]},
                        rationale="test",
                    ),
                ),
            ),
        ),
    )
    report = runner.run_suite(suite, lambda _: "x", agent_fingerprint="f")
    decision = gating.evaluate_gate(report)
    assert decision.outcome is gating.GateOutcome.BLOCK
    assert any("INC-1" in r for r in decision.blocking_reasons)


def test_a_judge_failure_alone_warns_rather_than_blocking():
    from app.services.agenteval.suites import EvalCase, EvalSuite

    suite = EvalSuite(
        agent_key="x",
        name="x",
        description="",
        version=1,
        cases=(
            EvalCase(
                name="open_ended",
                inputs=({"a": 1},),
                assertions=(
                    AssertionSpec(
                        kind=AssertionKind.LLM_JUDGE,
                        config={"min_score": 4, "rubric": "Is it clear?"},
                        rationale="test",
                    ),
                ),
            ),
        ),
    )
    report = runner.run_suite(
        suite, lambda _: "text", agent_fingerprint="f", judge_scores={"open_ended": 2}
    )
    decision = gating.evaluate_gate(report)
    assert decision.outcome is gating.GateOutcome.WARN
    assert decision.outcome.allows_merge


def test_an_agent_that_raises_blocks_rather_than_failing_quietly():
    from app.services.agenteval.suites import EvalCase, EvalSuite

    def crashing(_case):
        raise RuntimeError("boom")

    suite = EvalSuite(
        agent_key="x",
        name="x",
        description="",
        version=1,
        cases=(
            EvalCase(
                name="c",
                inputs=({"a": 1},),
                assertions=(_spec(AssertionKind.EXACT, expected=1),),
            ),
        ),
    )
    report = runner.run_suite(suite, crashing, agent_fingerprint="f")
    assert report.errored
    assert "RuntimeError" in report.results[0].error
    decision = gating.evaluate_gate(report)
    assert decision.outcome is gating.GateOutcome.BLOCK
    assert any("did not run is not a case that passed" in r for r in decision.blocking_reasons)


def test_a_malformed_assertion_is_an_error_not_a_failure():
    from app.services.agenteval.suites import EvalCase, EvalSuite

    suite = EvalSuite(
        agent_key="x",
        name="x",
        description="",
        version=1,
        cases=(
            EvalCase(
                name="c",
                inputs=({"a": 1}, {"a": 2}),
                assertions=(
                    AssertionSpec(
                        kind=AssertionKind.DISCRIMINATION,
                        config={"path": "score"},  # no min_spread
                        rationale="test",
                    ),
                ),
            ),
        ),
    )
    report = runner.run_suite(suite, lambda _: {"score": 1}, agent_fingerprint="f")
    assert report.results[0].error
    assert "could not be evaluated" in report.results[0].error


# ══════════════════════════════════════════════════════════════════════════════
# Running, cost and the diff
# ══════════════════════════════════════════════════════════════════════════════


def test_a_tag_filter_cannot_skip_a_regression_case():
    report = runner.run_suite(
        suite_for("fit_score"),
        fixtures.fit_score_working,
        agent_fingerprint="f",
        only_tags=("explainability",),
    )
    names = {r.case_name for r in report.results}
    assert "fit_score_discrimination" in names, (
        "A tag filter skipped a regression case. That would make 'I ran the "
        "suite' mean something different depending on the flags."
    )


def test_cost_and_latency_are_recorded_per_case():
    def cost_fn(_payload, _output):
        return 120, 0.0045

    report = runner.run_suite(
        suite_for("fit_score"),
        fixtures.fit_score_working,
        agent_fingerprint="f",
        cost_fn=cost_fn,
    )
    total_inputs = sum(len(c.inputs) for c in suite_for("fit_score").cases)
    assert report.total_tokens == 120 * total_inputs
    assert report.total_cost_inr == pytest.approx(0.0045 * total_inputs)
    assert all(r.latency_ms >= 0 for r in report.results)


def test_the_report_exposes_the_numbers_behind_each_verdict():
    report = runner.run_suite(
        suite_for("fit_score"),
        fixtures.fit_score_working,
        agent_fingerprint="f",
    )
    metrics = report.measurements()
    assert "fit_score_discrimination.discrimination.spread" in metrics
    assert metrics["fit_score_discrimination.discrimination.spread"] > 15


def test_the_diff_surfaces_a_metric_that_moved_while_still_passing():
    """The slow degradation. Both runs are green; the spread halved."""

    def wide(_case):
        return {"score": _case["v"] * 10}

    def narrow(_case):
        return {"score": 50 + _case["v"]}

    from app.services.agenteval.suites import EvalCase, EvalSuite

    suite = EvalSuite(
        agent_key="x",
        name="x",
        description="",
        version=1,
        cases=(
            EvalCase(
                name="spread",
                inputs=({"v": 1}, {"v": 5}, {"v": 9}),
                assertions=(
                    _spec(
                        AssertionKind.DISCRIMINATION, path="score", min_spread=5
                    ),
                ),
            ),
        ),
    )
    before = runner.run_suite(suite, wide, agent_fingerprint="a")
    after = runner.run_suite(suite, narrow, agent_fingerprint="b")

    assert before.fail_count == 0
    assert after.fail_count == 0  # still passing

    diff = runner.compare(before, after)
    assert diff.is_clean  # nothing newly failing
    moved = {m.key: m for m in diff.moved}
    key = "spread.discrimination.spread"
    assert key in moved, "the collapsing spread was not reported as a move"
    assert moved[key].before == 80
    assert moved[key].after == 8


def test_the_gate_warns_about_a_metric_that_moved():
    from app.services.agenteval.runner import MetricMove, RunDiff

    report = runner.run_suite(
        suite_for("fit_score"),
        fixtures.fit_score_working,
        agent_fingerprint="f",
    )
    diff = RunDiff(
        newly_failing=(),
        newly_passing=(),
        moved=(MetricMove(key="x.discrimination.spread", before=34, after=21),),
        cost_delta_inr=0.0,
        latency_delta_ms=0.0,
        token_delta=0,
    )
    decision = gating.evaluate_gate(report, diff=diff)
    assert decision.outcome is gating.GateOutcome.WARN
    assert any("slow degradation" in w for w in decision.warnings)


def test_a_fingerprint_changes_when_the_implementation_changes():
    assert fixtures.agent_fingerprint("fit_score") != fixtures.agent_fingerprint(
        "fit_score_defective"
    )
    assert fixtures.agent_fingerprint("fit_score") == fixtures.agent_fingerprint(
        "fit_score"
    )


# ══════════════════════════════════════════════════════════════════════════════
# The other backfilled suites
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("agent_key", sorted(SUITES))
def test_every_backfilled_suite_passes_its_reference_implementation(agent_key):
    report = runner.run_suite(
        suite_for(agent_key),
        fixtures.AGENTS[agent_key],
        agent_fingerprint=fixtures.agent_fingerprint(agent_key),
    )
    assert report.fail_count == 0, [
        (r.case_name, r.failure_reason) for r in report.results if not r.passed
    ]


def test_every_scoring_agent_has_a_discrimination_assertion():
    from app.services.agenteval.suites import scoring_agents

    for agent_key in scoring_agents():
        assert gating.missing_discrimination(suite_for(agent_key)) is None


def test_proctoring_never_recommends_disqualification():
    result = fixtures.proctoring_flags(
        {"events": [{"type": "paste"}] * 20 + [{"type": "face_absent"}] * 20}
    )
    assert result["severity"] == 100
    assert result["recommendation"] == "review"


def test_expense_extraction_refuses_rather_than_guessing():
    result = fixtures.expense_extraction({"receipt_text": "Blue Tokai\nsmudged"})
    assert result["extracted"] is False
    assert result["amount"] is None
    assert result["confidence"] == 0.0


def test_the_compliance_monitor_cites_the_filing_it_read():
    result = fixtures.compliance_monitor(
        {
            "filings": [
                {
                    "id": "f1",
                    "kind": "Form 24Q",
                    "due_on": "2026-07-31",
                    "status": "pending",
                }
            ],
            "today": "2026-09-07",
        }
    )
    assert "[[filing:f1]]" in result["summary"]


# ══════════════════════════════════════════════════════════════════════════════
# Drift
# ══════════════════════════════════════════════════════════════════════════════


def _window(label, values):
    return drift.ScoreWindow(label, tuple(values))


def test_drift_catches_a_collapsing_score_distribution():
    """The Fit Score defect as it would have looked in production."""
    rng = random.Random(11)
    baseline = _window("b", [rng.uniform(40, 95) for _ in range(200)])
    collapsed = _window("c", [rng.uniform(71.5, 72.5) for _ in range(200)])

    alerts = drift.detect_drift(
        baseline_scores=baseline, current_scores=collapsed, sample_count=200
    )
    assert alerts
    assert alerts[0].signal is drift.DriftSignal.DISTRIBUTION_COLLAPSE
    assert alerts[0].severity is drift.DriftSeverity.CRITICAL
    assert "stopped telling inputs apart" in alerts[0].message


def test_drift_stays_quiet_on_a_healthy_distribution():
    rng = random.Random(3)
    baseline = _window("b", [rng.uniform(40, 95) for _ in range(200)])
    current = _window("c", [rng.uniform(40, 95) for _ in range(200)])
    assert (
        drift.detect_drift(
            baseline_scores=baseline, current_scores=current, sample_count=200
        )
        == []
    )


def test_drift_stays_quiet_on_a_window_too_small_to_mean_anything():
    baseline = _window("b", [10, 50, 90])
    collapsed = _window("c", [72, 72, 72])
    assert (
        drift.detect_drift(
            baseline_scores=baseline, current_scores=collapsed, sample_count=3
        )
        == []
    )


def test_drift_catches_a_refusal_rate_spike():
    alerts = drift.detect_drift(
        baseline_refusal_rate=0.02,
        current_refusal_rate=0.41,
        sample_count=400,
    )
    assert alerts[0].signal is drift.DriftSignal.REFUSAL_RATE_SPIKE
    assert alerts[0].severity is drift.DriftSeverity.CRITICAL


def test_drift_catches_a_refusal_rate_collapse():
    """The more dangerous direction: it stopped declining what it should."""
    alerts = drift.detect_drift(
        baseline_refusal_rate=0.35,
        current_refusal_rate=0.01,
        sample_count=400,
    )
    signals = {a.signal for a in alerts}
    assert drift.DriftSignal.REFUSAL_RATE_COLLAPSE in signals


def test_drift_catches_rising_grounding_failures():
    alerts = drift.detect_drift(
        baseline_grounding_failure_rate=0.01,
        current_grounding_failure_rate=0.22,
        sample_count=300,
    )
    assert alerts[0].signal is drift.DriftSignal.GROUNDING_FAILURE_RISE


def test_drift_reports_a_shift_separately_from_a_collapse():
    rng = random.Random(5)
    baseline = _window("b", [rng.gauss(70, 10) for _ in range(200)])
    shifted = _window("c", [rng.gauss(85, 10) for _ in range(200)])
    alerts = drift.detect_drift(
        baseline_scores=baseline, current_scores=shifted, sample_count=200
    )
    assert alerts[0].signal is drift.DriftSignal.DISTRIBUTION_SHIFT
    assert "recalibration" in alerts[0].message


def test_drift_reports_a_cost_rise():
    alerts = drift.detect_drift(
        baseline_cost_inr=0.004, current_cost_inr=0.013, sample_count=100
    )
    assert alerts[0].signal is drift.DriftSignal.COST_RISE


def test_an_unmeasured_signal_produces_no_alert():
    """None means 'not measured', which is different from zero."""
    assert drift.detect_drift(sample_count=500) == []
