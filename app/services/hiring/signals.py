"""How interview scores, the Fit Score and proctoring notes reach a screen.

═══════════════════════════════════════════════════════════════════════════════
NO AI SCORE WITHOUT ITS SIGNALS AND A CONFIDENCE (FS-ANL-02, CLAUDE.md rule 7)
═══════════════════════════════════════════════════════════════════════════════

An AI round score that arrives without the signals that produced it, or without
a confidence, is withheld — the row is listed, the number is not. A recruiter
cannot argue with a bare 73, and will act on it.

The Fit Score is shown only beside the component round scores that back it.
**AskMira does not compute a Fit Score.** Where one is stored it came from an
imported record, and the screen says exactly that.

═══════════════════════════════════════════════════════════════════════════════
PROCTORING NOTES ARE OBSERVATIONS, NOT FINDINGS (FS-HIR-02 blocking gate)
═══════════════════════════════════════════════════════════════════════════════

The carried-over "AI-Cheating 10/10" badge is a named pre-launch blocker. What a
proctoring tool logs — a tab switch, a period with no face in frame — is
reported as a count of what was logged. Any judgement field the tool supplied
(a cheating score, a verdict, a risk rating) is not displayed at all, and the
statement never uses judgement words. A regression test holds that.

Pure.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

__all__ = [
    "JUDGEMENT_WORDS",
    "fit_score_view",
    "present_score",
    "proctoring_statement",
]

#: Words a proctoring statement never contains. Judgements about a person
#: belong to a human reviewer, not to a label generated from a webcam.
JUDGEMENT_WORDS: tuple[str, ...] = (
    "cheat",
    "suspicious",
    "suspect",
    "fraud",
    "violation",
    "dishonest",
    "malpractice",
    "misconduct",
    "integrity score",
    "flagged",
)

#: Keys whose values are judgements rather than observations. Never displayed.
_JUDGEMENT_KEYS = ("cheat", "verdict", "risk", "suspic", "integrity", "fraud", "score")

#: Observation keys we know how to phrase. Anything else is phrased generically.
_OBSERVATIONS: dict[str, tuple[str, str]] = {
    "tab_switches": ("tab switch recorded", "tab switches recorded"),
    "tab_switch": ("tab switch recorded", "tab switches recorded"),
    "face_absent": (
        "period with no face detected in frame",
        "periods with no face detected in frame",
    ),
    "multiple_faces": (
        "period with more than one face in frame",
        "periods with more than one face in frame",
    ),
    "paste_events": ("paste event recorded", "paste events recorded"),
    "paste": ("paste event recorded", "paste events recorded"),
    "window_blur": (
        "time the assessment window lost focus",
        "times the assessment window lost focus",
    ),
    "fullscreen_exits": ("exit from full screen", "exits from full screen"),
    "audio_detected": (
        "period with background speech detected",
        "periods with background speech detected",
    ),
}


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _is_judgement_key(key: str) -> bool:
    lowered = key.lower()
    return any(word in lowered for word in _JUDGEMENT_KEYS)


def proctoring_statement(notes: dict[str, Any] | None) -> dict[str, Any]:
    """Factual statements of what the proctoring tool logged."""
    notes = dict(notes or {})
    statements: list[str] = []
    withheld = 0

    for key in sorted(notes):
        value = notes[key]
        if _is_judgement_key(key):
            withheld += 1
            continue
        if isinstance(value, bool):
            if value:
                statements.append(f"{key.replace('_', ' ').capitalize()}: recorded")
            continue
        count = _decimal(value)
        if count is not None and count == count.to_integral_value():
            number = int(count)
            if number <= 0:
                continue
            singular, plural = _OBSERVATIONS.get(
                key, (key.replace("_", " "), key.replace("_", " "))
            )
            statements.append(f"{number} {singular if number == 1 else plural}")
            continue
        if isinstance(value, str) and value.strip():
            text = value.strip()
            if any(word in text.lower() for word in JUDGEMENT_WORDS):
                withheld += 1
                continue
            statements.append(f"{key.replace('_', ' ').capitalize()}: {text[:200]}")

    status = "observations_recorded" if statements else "no_observations"
    return {
        "status": status,
        "label": ("Observations recorded" if statements else "No observations recorded"),
        "statements": statements,
        "note": (
            "These are what the proctoring tool logged during the assessment. They are "
            "not a finding about the candidate, and nothing here is used to score or "
            "reject anyone — a person reviews them in context."
        ),
        "judgement_fields_not_shown": withheld,
    }


def present_score(row: dict[str, Any]) -> dict[str, Any]:
    """One round's score, or the reason it is withheld.

    `row` carries the `InterviewScore` fields as plain values.
    """
    is_ai = bool(row.get("is_ai_generated"))
    signals = row.get("signals") or {}
    confidence = _decimal(row.get("confidence"))
    score = _decimal(row.get("score"))

    base = {
        "id": str(row.get("id") or ""),
        "round_type": row.get("round_type"),
        "round_number": row.get("round_number"),
        "is_ai_generated": is_ai,
        "summary": row.get("summary"),
        "proctoring": proctoring_statement(row.get("proctoring_notes")),
    }

    if is_ai and (not signals or confidence is None):
        missing = []
        if not signals:
            missing.append("the signals that produced it")
        if confidence is None:
            missing.append("a confidence value")
        return base | {
            "score": None,
            "max_score": None,
            "confidence": None,
            "signals": [],
            "withheld": True,
            "withheld_reason": (
                f"This AI score arrived without {' and '.join(missing)}, so it is not "
                "shown. An AI number nobody can explain is one people act on anyway."
            ),
        }

    return base | {
        "score": None if score is None else str(score),
        "max_score": str(_decimal(row.get("max_score")) or Decimal(100)),
        "confidence": None if confidence is None else str(confidence),
        "signals": [
            {"name": str(name).replace("_", " "), "value": str(value)}
            for name, value in sorted(signals.items())
        ],
        "withheld": False,
        "withheld_reason": None,
    }


def fit_score_view(*, fit_score: Any, presented_scores: list[dict[str, Any]]) -> dict[str, Any]:
    """The stored Fit Score, only beside the component scores that back it."""
    value = _decimal(fit_score)
    components = [
        s for s in presented_scores if not s.get("withheld") and s.get("score") is not None
    ]
    if value is None:
        return {
            "value": None,
            "status": "not_computed",
            "components": [],
            "confidence": None,
            "note": (
                "No Fit Score is recorded for this candidate. AskMira does not compute "
                "one; the round scores below are shown on their own."
            ),
        }
    if not components:
        return {
            "value": None,
            "status": "withheld",
            "components": [],
            "confidence": None,
            "note": (
                "A Fit Score is stored for this candidate, but no round scores with "
                "their signals back it, so it is not shown."
            ),
        }
    confidences = [_decimal(s["confidence"]) for s in components if s.get("confidence") is not None]
    return {
        "value": str(value),
        "status": "shown_with_components",
        "components": [
            {
                "round_type": s["round_type"],
                "round_number": s["round_number"],
                "score": s["score"],
                "confidence": s["confidence"],
            }
            for s in components
        ],
        "confidence": str(min(confidences)) if confidences else None,
        "note": (
            "Stored with the imported record rather than computed by AskMira. Read it "
            "with the round scores and their signals, never on its own."
        ),
    }
