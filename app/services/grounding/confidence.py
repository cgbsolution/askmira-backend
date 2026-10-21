"""Confidence that is rendered, and a floor below which there is no score.

═══════════════════════════════════════════════════════════════════════════════
A 51% CONFIDENT AGENT AND A 95% CONFIDENT ONE MUST NOT LOOK THE SAME
═══════════════════════════════════════════════════════════════════════════════

They do look the same in almost every product that ships one, because the
confidence is stored and the score is displayed. A manager reads "retention risk
72" and acts; nothing on the screen distinguishes a 72 built on four signals
across two years from a 72 built on one signal and a guess.

So `Confidence` carries a band and a label meant for a screen, and
`present` returns the thing the UI renders rather than a number the UI decorates.

═══════════════════════════════════════════════════════════════════════════════
BELOW THE FLOOR THERE IS NO SCORE, NOT A WEAK ONE
═══════════════════════════════════════════════════════════════════════════════

A retention flag on thin evidence does not just mislead once. It damages the
credibility of every subsequent flag, including the well-evidenced one about
somebody who really is about to leave — and by then the manager has learned to
ignore them.

So below `threshold` the output is `INSUFFICIENT_SIGNAL` and **carries no
score at all**. Not a greyed-out number, not a number with a warning: the field
is `None`, so a UI cannot render it by mistake and a downstream consumer cannot
average it into something.

Pure.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any

__all__ = [
    "DEFAULT_THRESHOLD",
    "ConfidenceBand",
    "ScoredOutput",
    "band_for",
    "present",
]

#: Below this, no score is shown. 0.6 is a judgement: high enough that a coin
#: flip dressed as an insight cannot render, low enough that a genuinely useful
#: signal on partial evidence still reaches somebody.
DEFAULT_THRESHOLD = 0.6


class ConfidenceBand(str, enum.Enum):
    """How sure, in a form a screen can render distinctly.

    Bands rather than a raw percentage on the surface, because "68%" invites a
    precision nobody has. The number is kept and shown alongside; the band is
    what makes two outputs look different at a glance.
    """

    INSUFFICIENT = "insufficient"
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"

    @property
    def label(self) -> str:
        return {
            ConfidenceBand.INSUFFICIENT: "Not enough signal",
            ConfidenceBand.LOW: "Low confidence",
            ConfidenceBand.MODERATE: "Moderate confidence",
            ConfidenceBand.HIGH: "High confidence",
        }[self]

    @property
    def guidance(self) -> str:
        """What a person should do with an output at this confidence."""
        return {
            ConfidenceBand.INSUFFICIENT: (
                "There is not enough in the record to say anything useful here. "
                "No score is shown, deliberately — a weak number would be acted "
                "on as though it were a strong one."
            ),
            ConfidenceBand.LOW: (
                "Treat this as a prompt to look, not as a finding. Check the "
                "signals below before you act on it."
            ),
            ConfidenceBand.MODERATE: (
                "Worth attention. The signals are real; whether they add up to "
                "what this suggests is a judgement you are better placed to "
                "make."
            ),
            ConfidenceBand.HIGH: (
                "Well evidenced. It is still a pattern in the data rather than "
                "a fact about the person."
            ),
        }[self]

    @property
    def renders_a_score(self) -> bool:
        return self is not ConfidenceBand.INSUFFICIENT


def band_for(confidence: float, *, threshold: float = DEFAULT_THRESHOLD) -> ConfidenceBand:
    """Which band a confidence value falls in.

    The threshold moves the INSUFFICIENT boundary and nothing else. A tenant
    that raises it gets fewer scores, not differently-labelled ones.
    """
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(
            f"Confidence has to be between 0 and 1; got {confidence}. A value "
            "outside that range usually means a percentage was passed where a "
            "fraction was expected, which would put a 72% output in the highest "
            "band by accident."
        )
    if confidence < threshold:
        return ConfidenceBand.INSUFFICIENT
    if confidence < 0.75:
        return ConfidenceBand.LOW
    if confidence < 0.9:
        return ConfidenceBand.MODERATE
    return ConfidenceBand.HIGH


@dataclass(frozen=True, slots=True)
class ScoredOutput:
    """A score, its confidence, and its signals — or none of the three.

    `score` is `None` below the threshold. Not zero, not the raw value with a
    flag: `None`, so nothing downstream can render or average it. A field that
    holds a number the UI is asked not to show is a number some UI will show.
    """

    band: ConfidenceBand
    confidence: float
    score: float | None
    signals: tuple[str, ...]
    #: What is missing, where the confidence was too low. The useful half of an
    #: "insufficient signal" answer: it tells somebody what would fix it.
    missing: tuple[str, ...] = ()

    @property
    def renders_a_score(self) -> bool:
        return self.score is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "band": self.band.value,
            "band_label": self.band.label,
            "guidance": self.band.guidance,
            "confidence": round(self.confidence, 3),
            "score": self.score,
            "renders_a_score": self.renders_a_score,
            "signals": list(self.signals),
            "missing": list(self.missing),
        }


def present(
    *,
    score: float,
    confidence: float,
    signals: tuple[str, ...],
    missing: tuple[str, ...] = (),
    threshold: float = DEFAULT_THRESHOLD,
) -> ScoredOutput:
    """Turn a raw score into what a screen should show.

    Refuses a score with no signals, whatever the confidence. FS-ANL-02: no AI
    score without its contributing signals. A number a manager cannot interrogate
    is a number they will either over-trust or ignore, and both are worse than
    the number not existing.
    """
    if not signals:
        raise ValueError(
            "A score with no contributing signals must not be presented. A "
            "manager cannot interrogate a bare number, so they will either "
            "over-trust it or ignore it — and the first one changes somebody's "
            "job."
        )

    band = band_for(confidence, threshold=threshold)
    if band is ConfidenceBand.INSUFFICIENT:
        return ScoredOutput(
            band=band,
            confidence=confidence,
            score=None,
            signals=signals,
            missing=missing or ("more history than the record currently holds",),
        )

    return ScoredOutput(
        band=band,
        confidence=confidence,
        score=score,
        signals=signals,
        missing=missing,
    )
