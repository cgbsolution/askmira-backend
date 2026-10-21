"""Reference agent implementations, including the one that was defective.

═══════════════════════════════════════════════════════════════════════════════
WHY THIS FILE EXISTS AT ALL
═══════════════════════════════════════════════════════════════════════════════

**No AI agent is built in this codebase yet.** The Fit Score is a column on
`candidates` that nothing computes; the proctoring, retention-risk, compliance
and expense-extraction agents named in the brief do not exist. The agent layer
is slices 28 and 29.

That leaves a real question: how do you demonstrate that an eval harness catches
a defect, when the thing it would catch the defect in has not been written?

The answer here is to write the defect. `fit_score_defective` is a
reconstruction of the shipped behaviour — a scorer that returns something in the
right range for every candidate and tells you nothing about any of them.
`fit_score_working` is what it should have been. The suite in `suites.py` is run
against both, and the test asserts it **fails the first and passes the second**.

That is a genuine demonstration: the harness catches the class of defect,
proved against an implementation that has it. It is not a demonstration that the
production Fit Score is fixed, because there is no production Fit Score. The
BUILD_LOG says so.

These are deterministic functions, not model calls. That is the point — an eval
harness whose own tests need a live model is an eval harness that does not run
in CI.
"""

from __future__ import annotations

import hashlib
from typing import Any

__all__ = [
    "AGENTS",
    "compliance_monitor",
    "expense_extraction",
    "fit_score_defective",
    "fit_score_working",
    "proctoring_flags",
    "retention_risk",
]


def _rounds(case: dict[str, Any]) -> list[float]:
    return [float(v) for v in case.get("round_scores", [])]


def fit_score_defective(case: dict[str, Any]) -> dict[str, Any]:
    """The shipped defect, reconstructed.

    The mistake is one line: the round scores are averaged and then squashed
    into a narrow band around a constant. Every score lands between 71 and 73,
    which looks entirely reasonable on any single candidate — 72 is a plausible
    fit score — and is useless the moment you compare two.

    It passes a range check. It passes a structural check. It passes a human
    reviewing outputs one at a time. Only comparing across inputs catches it,
    which is why `DISCRIMINATION` exists.
    """
    rounds = _rounds(case)
    average = sum(rounds) / len(rounds) if rounds else 50.0
    # The defect: the signal is compressed to a twentieth of its range and added
    # to a constant, so a 19-point spread in the input becomes 0.95 in the
    # output.
    score = 72.0 + (average - 50.0) / 20.0
    return {
        "score": round(score, 2),
        "signals": [f"average round score {average:.1f}"],
        "confidence": 0.8,
    }


def fit_score_working(case: dict[str, Any]) -> dict[str, Any]:
    """What it should have been.

    The round scores drive the output across the full range, weighted by how
    much of the loop the candidate actually completed — a strong average over
    one round is weaker evidence than the same average over four, and the score
    says so rather than pretending the two are the same.
    """
    rounds = _rounds(case)
    if not rounds:
        return {
            "score": 0.0,
            "signals": ["no rounds completed"],
            "confidence": 0.0,
        }

    average = sum(rounds) / len(rounds)
    # Evidence weight: one round is thin, four is a full loop.
    completeness = min(len(rounds), 4) / 4
    experience_fit = float(case.get("years_experience_fit", 1.0))

    score = average * (0.6 + 0.4 * completeness) * experience_fit
    score = max(0.0, min(100.0, score))

    return {
        "score": round(score, 2),
        # FS-ANL-02: no score without its contributing signals and a confidence.
        "signals": [
            f"average round score {average:.1f} across {len(rounds)} round(s)",
            f"loop completeness {completeness:.0%}",
            f"experience fit {experience_fit:.2f}",
        ],
        "confidence": round(0.4 + 0.6 * completeness, 2),
    }


def proctoring_flags(case: dict[str, Any]) -> dict[str, Any]:
    """Assessment integrity signals, and the refusal that has to come with them.

    Deliberately returns `recommendation: "review"` rather than
    `"disqualify"` at every severity. A proctoring agent that disqualifies a
    candidate on its own is an agent making a hiring decision from a webcam, and
    the suite asserts it never does.
    """
    events = case.get("events", [])
    tab_switches = sum(1 for e in events if e.get("type") == "tab_switch")
    face_absent = sum(1 for e in events if e.get("type") == "face_absent")
    paste = sum(1 for e in events if e.get("type") == "paste")

    severity = min(100, tab_switches * 8 + face_absent * 12 + paste * 20)
    signals = []
    if tab_switches:
        signals.append(f"{tab_switches} tab switch(es)")
    if face_absent:
        signals.append(f"{face_absent} period(s) with no face detected")
    if paste:
        signals.append(f"{paste} paste event(s)")
    if not signals:
        signals.append("no integrity events recorded")

    return {
        "severity": severity,
        "signals": signals,
        # Never "disqualify". A human decides; this flags.
        "recommendation": "review" if severity >= 25 else "no_action",
        "confidence": 0.7 if events else 0.95,
    }


def retention_risk(case: dict[str, Any]) -> dict[str, Any]:
    """Flight risk, from signals a manager could have noticed themselves.

    Every contributing signal is named, because a retention score with no
    signals is a number a manager will act on and cannot explain — and acting
    on it is a conversation with a real person about their future.
    """
    tenure = float(case.get("tenure_months", 12))
    last_increment = float(case.get("months_since_increment", 12))
    rating = float(case.get("last_rating", 3))
    manager_changes = int(case.get("manager_changes_12m", 0))
    leave_spike = bool(case.get("unusual_leave_pattern", False))

    risk = 0.0
    signals: list[str] = []

    if 9 <= tenure <= 30:
        risk += 22
        signals.append(f"{tenure:.0f} months' tenure — the common exit window")
    if last_increment >= 18:
        risk += 25
        signals.append(f"{last_increment:.0f} months since the last increment")
    if rating >= 4:
        risk += 18
        signals.append(f"rated {rating:.1f} — strong performers have options")
    if manager_changes >= 2:
        risk += 20
        signals.append(f"{manager_changes} manager changes in twelve months")
    if leave_spike:
        risk += 15
        signals.append("an unusual leave pattern in the last quarter")

    if not signals:
        signals.append("no elevated risk signals")

    return {
        "risk": round(min(100.0, risk), 2),
        "signals": signals,
        "confidence": round(min(0.95, 0.5 + 0.1 * len(signals)), 2),
    }


def compliance_monitor(case: dict[str, Any]) -> dict[str, Any]:
    """Statutory deadline monitoring, which must cite what it is asserting.

    Every finding carries a `[[record:id]]` citation into the filing record it
    read. A compliance alert that cannot say which return it is about is an
    alert somebody will learn to ignore.
    """
    filings = case.get("filings", [])
    today = case.get("today", "2026-09-07")

    findings: list[str] = []
    for filing in filings:
        due = str(filing.get("due_on", ""))
        if filing.get("status") == "filed":
            continue
        if due and due < today:
            findings.append(
                f"{filing.get('kind', 'A return')} was due on {due} and has not "
                f"been filed [[filing:{filing.get('id')}]]"
            )

    return {
        "summary": (
            " ".join(findings)
            if findings
            else "Every statutory return in scope is filed and up to date."
        ),
        "finding_count": len(findings),
        "confidence": 1.0,
    }


def expense_extraction(case: dict[str, Any]) -> dict[str, Any]:
    """Reads an expense receipt into structured fields.

    Refuses rather than guessing where a field is unreadable. A total silently
    inferred from a partial line becomes a reimbursement, and the difference
    between "I could not read this" and "₹2,340" is somebody's money.
    """
    text = str(case.get("receipt_text", ""))

    amount: float | None = None
    for line in text.splitlines():
        lowered = line.lower()
        if "total" in lowered:
            digits = "".join(ch for ch in line if ch.isdigit() or ch == ".").strip(".")
            if digits:
                try:
                    amount = float(digits)
                except ValueError:
                    amount = None

    vendor = next((line.strip() for line in text.splitlines() if line.strip()), "")

    if amount is None:
        return {
            "extracted": False,
            "reason": (
                "The total could not be read from this receipt. It has not been "
                "guessed — an inferred amount becomes a reimbursement."
            ),
            "amount": None,
            "vendor": vendor,
            "confidence": 0.0,
        }

    return {
        "extracted": True,
        "amount": amount,
        "vendor": vendor,
        "currency": "INR",
        "confidence": 0.85,
    }


#: The agents a suite can be run against, by key.
#:
#: `fit_score_defective` is registered deliberately: the regression case for the
#: original incident is run against it, and the test asserts it fails. Removing
#: it would remove the proof that the harness catches that defect.
AGENTS: dict[str, Any] = {
    "fit_score": fit_score_working,
    "fit_score_defective": fit_score_defective,
    "proctoring": proctoring_flags,
    "retention_risk": retention_risk,
    "compliance_monitor": compliance_monitor,
    "expense_extraction": expense_extraction,
}


def agent_fingerprint(agent_key: str) -> str:
    """A stable identifier for the implementation currently registered.

    Hashes the function's source so an `EvalRun` can record *which* version of
    an agent it ran against. "The suite passed" is not useful six weeks later
    without knowing what it passed against, and a version string somebody
    remembers to bump is a version string that is wrong.
    """
    import inspect

    fn = AGENTS.get(agent_key)
    if fn is None:
        raise KeyError(f"No agent {agent_key!r}. Registered: {', '.join(sorted(AGENTS))}.")
    source = inspect.getsource(fn).encode("utf-8")
    return hashlib.sha256(source).hexdigest()[:16]
