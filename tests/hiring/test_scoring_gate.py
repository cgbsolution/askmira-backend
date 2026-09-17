"""The FS-HIR-02 blocking gate: Fit Score parity and proctoring wording.

**Finding.** AskMira does not compute a Fit Score anywhere. `Candidate.fit_score`
is a stored column (carried from imported AnytimeHire records) and nothing in
`app/` assigns it. So the carried-over parity defect — identical Fit Scores
despite a spread in AI Round Scores — cannot occur in this codebase today; what
can go wrong is the *display* collapsing distinct scores, or somebody adding a
computation without a guard. Both are pinned here.

**Proctoring.** Notes are reported as counts of what was logged. Judgement fields
are dropped and judgement words never appear.
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.hiring import signals

APP = Path(__file__).resolve().parents[2] / "app"


def _score(round_score: str, *, confidence: str = "0.80", number: int = 1) -> dict:
    return {
        "id": f"r{number}",
        "round_type": "ai_interview",
        "round_number": number,
        "score": Decimal(round_score),
        "max_score": Decimal(100),
        "confidence": Decimal(confidence),
        "is_ai_generated": True,
        "summary": None,
        "signals": {"communication": "7", "problem_solving": round_score},
        "proctoring_notes": {},
    }


def test_fit_score_view_varies_with_ai_round_score() -> None:
    """The regression test for the parity defect.

    Candidates with different AI round scores and different stored Fit Scores
    must come out different. If the view (or a future computation feeding it)
    ever clamps, rounds or defaults them to one value, this fails.
    """
    spread = ["41.50", "58.00", "73.25", "88.00", "96.75"]
    views = []
    for value in spread:
        presented = [signals.present_score(_score(value))]
        views.append(signals.fit_score_view(fit_score=Decimal(value), presented_scores=presented))

    shown = [v["value"] for v in views]
    components = [v["components"][0]["score"] for v in views]
    assert len(set(shown)) == len(spread), f"Fit Scores collapsed: {shown}"
    assert len(set(components)) == len(spread), f"Round scores collapsed: {components}"
    # And they move in the same direction as the round score.
    assert [Decimal(s) for s in shown] == sorted(Decimal(s) for s in shown)
    for view, value in zip(views, spread, strict=True):
        assert Decimal(view["value"]) == Decimal(value)
        assert view["status"] == "shown_with_components"


def test_no_fit_score_computation_exists_without_this_gate() -> None:
    """Nothing in the application assigns a Fit Score.

    If a computation is added, this test fails on purpose: whoever adds it must
    also add a variance test that runs the computation itself across a spread of
    AI round scores, and then update this guard to point at it.
    """
    offenders = []
    for path in APP.rglob("*.py"):
        if path.parts[-2] == "models":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign | ast.AugAssign):
                targets = [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr == "fit_score":
                    offenders.append(f"{path.relative_to(APP)}:{node.lineno}")
    assert not offenders, (
        f"A Fit Score is now computed at {offenders}. Add a regression test that the "
        "computed score varies with AI Round Score (FS-HIR-02) before shipping it."
    )


def test_a_bare_fit_score_is_never_shown() -> None:
    view = signals.fit_score_view(fit_score=Decimal("82.5"), presented_scores=[])
    assert view["value"] is None
    assert view["status"] == "withheld"


def test_no_stored_fit_score_says_so() -> None:
    view = signals.fit_score_view(fit_score=None, presented_scores=[])
    assert view["status"] == "not_computed"
    assert "does not compute" in view["note"]


@pytest.mark.parametrize(
    ("signals_value", "confidence"),
    [({}, "0.9"), ({"x": "1"}, None), ({}, None)],
)
def test_an_ai_score_without_signals_or_confidence_is_withheld(signals_value, confidence) -> None:
    row = _score("70")
    row["signals"] = signals_value
    row["confidence"] = None if confidence is None else Decimal(confidence)
    presented = signals.present_score(row)
    assert presented["withheld"] is True
    assert presented["score"] is None
    assert presented["withheld_reason"]


def test_a_withheld_round_does_not_back_a_fit_score() -> None:
    row = _score("70")
    row["signals"] = {}
    view = signals.fit_score_view(
        fit_score=Decimal("70"), presented_scores=[signals.present_score(row)]
    )
    assert view["value"] is None


def test_a_human_score_needs_no_confidence() -> None:
    row = _score("6")
    row["is_ai_generated"] = False
    row["confidence"] = None
    assert signals.present_score(row)["withheld"] is False


def test_proctoring_reports_counts_not_verdicts() -> None:
    result = signals.proctoring_statement(
        {
            "tab_switches": 3,
            "face_absent": 1,
            "paste_events": 0,
            "ai_cheating_score": 10,
            "verdict": "suspicious",
            "risk_level": "high",
        }
    )
    assert result["statements"] == [
        "1 period with no face detected in frame",
        "3 tab switches recorded",
    ]
    assert result["judgement_fields_not_shown"] == 3
    assert result["label"] == "Observations recorded"


def test_proctoring_wording_never_judges() -> None:
    notes = {
        "tab_switches": 12,
        "face_absent": 4,
        "multiple_faces": 2,
        "window_blur": 7,
        "comment": "Candidate appears to be cheating",
        "cheat_probability": "0.97",
        "integrity": "flagged",
    }
    result = signals.proctoring_statement(notes)
    text = " ".join([result["label"], result["note"], *result["statements"]]).lower()
    for word in signals.JUDGEMENT_WORDS:
        if word == "flagged":
            continue
        assert word not in text, f"'{word}' appeared in the proctoring wording"
    assert "cheating" not in text


def test_no_observations_is_said_plainly() -> None:
    result = signals.proctoring_statement({"tab_switches": 0})
    assert result["status"] == "no_observations"
    assert result["statements"] == []
