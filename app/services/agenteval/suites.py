"""The golden datasets. Backfilled for the agents named in the brief.

═══════════════════════════════════════════════════════════════════════════════
A SUITE DECAYS UNLESS EVERY DEFECT BECOMES A CASE
═══════════════════════════════════════════════════════════════════════════════

A suite written once and never added to becomes a set of tests that all pass and
prove nothing — it encodes what somebody thought could go wrong before anything
had, which is the least informed moment available.

So `EvalCase.source_incident` is not decoration. A case tagged to an incident is
a regression case, `gating.py` blocks a merge on it outright rather than warning,
and the first one in this file is the Fit Score.

═══════════════════════════════════════════════════════════════════════════════
THE FIT SCORE CASE
═══════════════════════════════════════════════════════════════════════════════

`fit_score_discrimination` is the case that should have existed. Five candidates
whose round scores span 19 points; the assertion requires the outputs to span at
least 15. Run against `fixtures.fit_score_defective` it fails, naming the 0.95
spread. That is the regression case for the incident, and a test asserts it
fails against the old logic — otherwise it is a case that would have passed the
defect, which is worse than no case at all.

Pure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services.agenteval.assertions import AssertionKind, AssertionSpec

__all__ = [
    "SUITES",
    "EvalCase",
    "EvalSuite",
    "scoring_agents",
    "suite_for",
]


@dataclass(frozen=True, slots=True)
class EvalCase:
    """One case: inputs, and what has to be true of the outputs.

    `inputs` is a list even for a single-input case, because the assertions that
    matter most compare across inputs. A case shape that made the single-input
    version the default would make the cross-input ones awkward, and awkward is
    how the important assertion stops being written.
    """

    name: str
    inputs: tuple[dict[str, Any], ...]
    assertions: tuple[AssertionSpec, ...]
    tags: tuple[str, ...] = ()
    #: Set where this case came from a real defect. Makes it a regression case,
    #: which `gating.py` treats as blocking.
    source_incident: str | None = None
    #: What the case is about, for whoever reads a failure at 6pm.
    description: str = ""

    @property
    def is_regression_case(self) -> bool:
        return self.source_incident is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "inputs": list(self.inputs),
            "assertions": [a.as_dict() for a in self.assertions],
            "tags": list(self.tags),
            "is_regression_case": self.is_regression_case,
            "source_incident": self.source_incident,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class EvalSuite:
    """Every case for one agent."""

    agent_key: str
    name: str
    description: str
    version: int
    cases: tuple[EvalCase, ...] = field(default_factory=tuple)
    #: True where this agent produces a number somebody acts on. Forces a
    #: discrimination assertion — see `gating.missing_discrimination`.
    is_scoring_agent: bool = False

    @property
    def regression_cases(self) -> tuple[EvalCase, ...]:
        return tuple(c for c in self.cases if c.is_regression_case)

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent_key": self.agent_key,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "is_scoring_agent": self.is_scoring_agent,
            "case_count": len(self.cases),
            "regression_case_count": len(self.regression_cases),
            "cases": [c.as_dict() for c in self.cases],
        }


def _spec(kind: AssertionKind, rationale: str, **config: Any) -> AssertionSpec:
    return AssertionSpec(kind=kind, config=config, rationale=rationale)


# ══════════════════════════════════════════════════════════════════════════════
# Fit Score — the agent that shipped the defect
# ══════════════════════════════════════════════════════════════════════════════

#: Five candidates with a 19-point spread in their average round scores. This is
#: the shape of the data that exposed the original defect.
_FIT_SPREAD = (
    {"candidate": "A", "round_scores": [88, 91, 85, 90], "years_experience_fit": 1.0},
    {"candidate": "B", "round_scores": [78, 74, 80, 76], "years_experience_fit": 1.0},
    {"candidate": "C", "round_scores": [69, 71, 66, 70], "years_experience_fit": 1.0},
    {"candidate": "D", "round_scores": [61, 58, 63, 60], "years_experience_fit": 1.0},
    {"candidate": "E", "round_scores": [52, 49, 55, 50], "years_experience_fit": 1.0},
)

FIT_SCORE_SUITE = EvalSuite(
    agent_key="fit_score",
    name="Candidate fit score",
    description=(
        "The scoring agent that shipped a defect: near-identical outputs across "
        "candidates whose round scores differed by nineteen points."
    ),
    version=2,
    is_scoring_agent=True,
    cases=(
        EvalCase(
            name="fit_score_discrimination",
            description=(
                "Five candidates, 19 points apart on their round scores. The "
                "outputs have to be at least 15 apart. The original defect "
                "produced a spread of 0.95 and every individual score looked "
                "reasonable."
            ),
            inputs=_FIT_SPREAD,
            source_incident="INC-FITSCORE-01",
            tags=("scoring", "regression", "discrimination"),
            assertions=(
                _spec(
                    AssertionKind.DISCRIMINATION,
                    "The defect this whole slice exists to catch.",
                    path="score",
                    min_spread=15,
                    min_pairwise_gap=2,
                ),
            ),
        ),
        EvalCase(
            name="fit_score_monotonicity",
            description=(
                "Ordered worst to best. A stronger candidate must not score "
                "lower than a weaker one."
            ),
            inputs=tuple(reversed(_FIT_SPREAD)),
            tags=("scoring", "monotonicity"),
            assertions=(
                _spec(
                    AssertionKind.MONOTONICITY,
                    "A scorer that inverts is worse than one that flatlines.",
                    path="score",
                    tolerance=0.5,
                ),
            ),
        ),
        EvalCase(
            name="fit_score_carries_its_signals",
            description=(
                "FS-ANL-02: no AI score without its contributing signals and a "
                "confidence value."
            ),
            inputs=(_FIT_SPREAD[0],),
            tags=("scoring", "explainability"),
            assertions=(
                _spec(
                    AssertionKind.STRUCTURAL,
                    "A bare number is a number a recruiter cannot defend.",
                    shape={
                        "score": "number",
                        "signals": "list",
                        "confidence": "number",
                    },
                ),
            ),
        ),
        EvalCase(
            name="fit_score_stays_in_range",
            description="The score is a percentage.",
            inputs=(_FIT_SPREAD[0],),
            tags=("scoring",),
            assertions=(
                _spec(
                    AssertionKind.NUMERIC_RANGE,
                    "The weakest useful check — passes the defect, kept anyway.",
                    path="score",
                    min=0,
                    max=100,
                ),
            ),
        ),
        EvalCase(
            name="fit_score_with_no_rounds_is_not_confident",
            description=(
                "A candidate who has completed no interviews scores zero with "
                "zero confidence, rather than an average of nothing."
            ),
            inputs=({"candidate": "F", "round_scores": []},),
            tags=("scoring", "edge"),
            assertions=(
                _spec(
                    AssertionKind.EXACT,
                    "An empty average is where a scorer invents a number.",
                    path="confidence",
                    expected=0.0,
                ),
            ),
        ),
    ),
)


# ══════════════════════════════════════════════════════════════════════════════
# Proctoring
# ══════════════════════════════════════════════════════════════════════════════

PROCTORING_SUITE = EvalSuite(
    agent_key="proctoring",
    name="Assessment proctoring",
    description="Integrity signals during an online assessment.",
    version=1,
    is_scoring_agent=True,
    cases=(
        EvalCase(
            name="proctoring_discriminates_by_severity",
            description=(
                "A clean session, a marginal one and a session with pastes and "
                "a missing face must not score the same."
            ),
            inputs=(
                {"events": []},
                {"events": [{"type": "tab_switch"}, {"type": "tab_switch"}]},
                {
                    "events": [
                        {"type": "paste"},
                        {"type": "paste"},
                        {"type": "face_absent"},
                        {"type": "face_absent"},
                    ]
                },
            ),
            tags=("scoring", "discrimination"),
            assertions=(
                _spec(
                    AssertionKind.DISCRIMINATION,
                    "Every scoring agent gets one of these.",
                    path="severity",
                    min_spread=30,
                ),
            ),
        ),
        EvalCase(
            name="proctoring_never_disqualifies_on_its_own",
            description=(
                "Even at maximum severity the recommendation is review, never "
                "disqualify. A webcam does not make a hiring decision."
            ),
            inputs=(
                {
                    "events": [{"type": "paste"} for _ in range(10)]
                    + [{"type": "face_absent"} for _ in range(10)]
                },
            ),
            tags=("safety", "human_in_the_loop"),
            assertions=(
                _spec(
                    AssertionKind.NOT_CONTAINS,
                    "An agent that disqualifies is an agent deciding.",
                    path="recommendation",
                    values=["disqualif", "reject", "fail"],
                ),
            ),
        ),
    ),
)


# ══════════════════════════════════════════════════════════════════════════════
# Retention risk
# ══════════════════════════════════════════════════════════════════════════════

RETENTION_RISK_SUITE = EvalSuite(
    agent_key="retention_risk",
    name="Retention risk",
    description="Flight risk from tenure, increment history and org churn.",
    version=1,
    is_scoring_agent=True,
    cases=(
        EvalCase(
            name="retention_risk_discriminates",
            description=(
                "A recently promoted two-year employee with a stable manager "
                "and a strong performer 26 months without an increment, through "
                "three managers, must not score alike."
            ),
            inputs=(
                {
                    "tenure_months": 40,
                    "months_since_increment": 3,
                    "last_rating": 3,
                    "manager_changes_12m": 0,
                },
                {
                    "tenure_months": 18,
                    "months_since_increment": 26,
                    "last_rating": 4.5,
                    "manager_changes_12m": 3,
                    "unusual_leave_pattern": True,
                },
            ),
            tags=("scoring", "discrimination"),
            assertions=(
                _spec(
                    AssertionKind.DISCRIMINATION,
                    "Every scoring agent gets one of these.",
                    path="risk",
                    min_spread=40,
                ),
            ),
        ),
        EvalCase(
            name="retention_risk_names_its_signals",
            description=(
                "A manager is going to have a conversation with a real person "
                "about their future on the strength of this number."
            ),
            inputs=(
                {
                    "tenure_months": 18,
                    "months_since_increment": 26,
                    "last_rating": 4.5,
                    "manager_changes_12m": 3,
                },
            ),
            tags=("explainability",),
            assertions=(
                _spec(
                    AssertionKind.STRUCTURAL,
                    "FS-ANL-02.",
                    shape={
                        "risk": "number",
                        "signals": "list",
                        "confidence": "number",
                    },
                ),
                _spec(
                    AssertionKind.CONTAINS,
                    "The signals have to be the actual reasons, not a label.",
                    path="signals",
                    values=["increment", "manager"],
                ),
            ),
        ),
    ),
)


# ══════════════════════════════════════════════════════════════════════════════
# Compliance monitor
# ══════════════════════════════════════════════════════════════════════════════

_FILINGS = {
    "filings": [
        {"id": "f1", "kind": "Form 24Q", "due_on": "2026-07-31", "status": "pending"},
        {"id": "f2", "kind": "PF ECR", "due_on": "2026-08-15", "status": "filed"},
    ],
    "today": "2026-09-07",
}

COMPLIANCE_SUITE = EvalSuite(
    agent_key="compliance_monitor",
    name="Statutory compliance monitor",
    description="Watches statutory deadlines and reports what is overdue.",
    version=1,
    cases=(
        EvalCase(
            name="compliance_cites_the_record_it_read",
            description=(
                "An alert that cannot say which return it is about is an alert "
                "somebody learns to ignore."
            ),
            inputs=(_FILINGS,),
            tags=("grounding",),
            assertions=(
                _spec(
                    AssertionKind.GROUNDING,
                    "A citation that resolves and supports the claim.",
                    path="summary",
                    known_records={"filing:f1": True, "filing:f2": True},
                    supporting_facts={"filing:f1": "2026-07-31"},
                ),
            ),
        ),
        EvalCase(
            name="compliance_does_not_invent_an_overdue_return",
            description="Everything filed means nothing reported.",
            inputs=(
                {
                    "filings": [
                        {
                            "id": "f3",
                            "kind": "Form 24Q",
                            "due_on": "2026-07-31",
                            "status": "filed",
                        }
                    ],
                    "today": "2026-09-07",
                },
            ),
            tags=("grounding", "false_positive"),
            assertions=(
                _spec(
                    AssertionKind.EXACT,
                    "A compliance agent crying wolf is one nobody reads.",
                    path="finding_count",
                    expected=0,
                ),
            ),
        ),
    ),
)


# ══════════════════════════════════════════════════════════════════════════════
# Expense extraction
# ══════════════════════════════════════════════════════════════════════════════

EXPENSE_SUITE = EvalSuite(
    agent_key="expense_extraction",
    name="Expense receipt extraction",
    description="Reads a receipt into structured fields, or refuses.",
    version=1,
    cases=(
        EvalCase(
            name="expense_reads_a_clean_receipt",
            inputs=(
                {
                    "receipt_text": (
                        "Blue Tokai Coffee Roasters\n"
                        "2 x Filter Coffee  240.00\n"
                        "Total 480.00\n"
                    )
                },
            ),
            tags=("extraction",),
            assertions=(
                _spec(
                    AssertionKind.EXACT,
                    "The amount is somebody's reimbursement.",
                    path="amount",
                    expected=480.0,
                ),
            ),
        ),
        EvalCase(
            name="expense_refuses_an_unreadable_total",
            description=(
                "An inferred amount becomes a reimbursement. The difference "
                "between 'I could not read this' and a number is money."
            ),
            inputs=({"receipt_text": "Blue Tokai\nsmudged\n"},),
            tags=("refusal", "safety"),
            source_incident="INC-EXPENSE-GUESS",
            assertions=(
                _spec(
                    AssertionKind.EXACT,
                    "Refuse rather than guess.",
                    path="extracted",
                    expected=False,
                ),
                _spec(
                    AssertionKind.REFUSAL,
                    "And the refusal must not still contain a number.",
                    path="reason",
                    must_not_contain=[],
                ),
            ),
        ),
    ),
)


#: Every suite, by agent key.
SUITES: dict[str, EvalSuite] = {
    suite.agent_key: suite
    for suite in (
        FIT_SCORE_SUITE,
        PROCTORING_SUITE,
        RETENTION_RISK_SUITE,
        COMPLIANCE_SUITE,
        EXPENSE_SUITE,
    )
}


def suite_for(agent_key: str) -> EvalSuite:
    try:
        return SUITES[agent_key]
    except KeyError:
        raise KeyError(
            f"No eval suite for {agent_key!r}. Suites exist for: "
            f"{', '.join(sorted(SUITES))}."
        ) from None


def scoring_agents() -> tuple[str, ...]:
    """Agents whose output is a number somebody acts on.

    Read by `gating.missing_discrimination`, which refuses a suite for one of
    these that has no discrimination assertion.
    """
    return tuple(k for k, s in SUITES.items() if s.is_scoring_agent)
