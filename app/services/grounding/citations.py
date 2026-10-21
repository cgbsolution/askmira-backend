"""Citation verification. Fail closed, or do not ship the answer.

═══════════════════════════════════════════════════════════════════════════════
AN UNCITED NUMBER IN AN HR SYSTEM IS TREATED AS FACT
═══════════════════════════════════════════════════════════════════════════════

A manager reads "Priya has taken 14 days of leave this year" in an agent
response and acts on it. They do not check. Nobody checks — that is the entire
value proposition of the assistant, and it is also why an unverifiable claim
must not render at all.

So the rule is not "flag low-confidence citations". It is: a factual claim whose
citation does not verify means **the response is withheld** and replaced with a
plain statement that it could not be verified. `verify` returns a `Withheld`
outcome, not a warning, and an invariant checks nothing downstream can render a
response whose verification failed.

═══════════════════════════════════════════════════════════════════════════════
THREE CHECKS, AND THE THIRD IS THE ONE PEOPLE SKIP
═══════════════════════════════════════════════════════════════════════════════

1. **The record exists.** Catches an invented identifier.
2. **It was in the retrieved set.** Catches a real identifier the model knew
   from somewhere else — training data, an earlier turn, a guess that happened
   to land. A citation to a record that exists but was never retrieved is not
   grounding; it is a coincidence.
3. **It is inside the requester's scope.** Catches the case where the retrieval
   was too wide. This is the one that gets skipped, because if retrieval is
   scoped correctly it can never fire — which is exactly the argument for
   keeping it, since it fires precisely when something upstream is wrong.

Pure.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "CITATION_PATTERN",
    "CitationProblem",
    "VerificationOutcome",
    "VerifiedResponse",
    "extract_citations",
    "verify",
]

#: `[[source:record_id]]`. Matched anywhere in the response text.
CITATION_PATTERN = re.compile(r"\[\[([a-z_][a-z0-9_]*):([A-Za-z0-9\-_]+)\]\]")

#: A claim that needs a citation looks like a statement about a person or a
#: number. Deliberately generous: the cost of demanding a citation for a
#: sentence that did not need one is a slightly noisier answer, and the cost of
#: missing one is a fabricated fact somebody acts on.
_FACTUAL_MARKERS = re.compile(
    r"\b(\d[\d,.]*|days?|leave|salary|ctc|rating|score|joined|resigned|"
    r"increment|balance|attendance|absent|present|approved|rejected|filed)\b",
    re.IGNORECASE,
)


class CitationProblem(str, enum.Enum):
    """Why one citation did not verify."""

    #: Cited an identifier no record has.
    RECORD_NOT_FOUND = "record_not_found"
    #: The record exists and was never retrieved for this question.
    NOT_IN_RETRIEVED_SET = "not_in_retrieved_set"
    #: The record is outside the requester's permission scope.
    OUT_OF_SCOPE = "out_of_scope"
    #: A factual claim with no citation at all.
    UNCITED_CLAIM = "uncited_claim"
    #: A citation to a source no agent may retrieve from. Should be impossible
    #: and is checked anyway — it would mean the forbidden-source guard was
    #: bypassed somewhere.
    FORBIDDEN_SOURCE = "forbidden_source"

    @property
    def explanation(self) -> str:
        return {
            CitationProblem.RECORD_NOT_FOUND: ("the response cited a record that does not exist"),
            CitationProblem.NOT_IN_RETRIEVED_SET: (
                "the response cited a record that was never retrieved for this "
                "question — it exists, which makes this worse rather than "
                "better: the claim was not grounded in anything that was read"
            ),
            CitationProblem.OUT_OF_SCOPE: (
                "the response cited a record outside the requester's permission "
                "scope, which means the retrieval was wider than it should have "
                "been"
            ),
            CitationProblem.UNCITED_CLAIM: ("the response made a factual claim with no citation"),
            CitationProblem.FORBIDDEN_SOURCE: (
                "the response cited a source no agent may retrieve from"
            ),
        }[self]


class VerificationOutcome(str, enum.Enum):
    """What happens to the response."""

    VERIFIED = "verified"
    #: Every claim verified and there were no claims needing one.
    NO_CLAIMS = "no_claims"
    #: Withheld. The user sees the fallback, never the response.
    WITHHELD = "withheld"

    @property
    def may_render(self) -> bool:
        return self is not VerificationOutcome.WITHHELD


@dataclass(frozen=True, slots=True)
class CitationFailure:
    """One problem, with enough detail to debug it."""

    problem: CitationProblem
    citation_id: str | None
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "problem": self.problem.value,
            "citation_id": self.citation_id,
            "detail": self.detail,
        }


#: What the user sees instead of an unverifiable answer. Deliberately plain and
#: deliberately not an apology with a guess attached — "I could not verify this,
#: but roughly..." is the failure mode this whole module exists to prevent.
WITHHELD_MESSAGE = (
    "I could not verify this answer against your records, so I am not showing "
    "it. Nothing here is a substitute for checking the record itself, and an "
    "unverified number is worse than no number."
)


@dataclass(frozen=True, slots=True)
class VerifiedResponse:
    """The result of checking one agent response.

    `text` is the **rendered** text: the original when verified, the withheld
    message when not. A caller that reads `.text` cannot accidentally show an
    unverified answer, which is a better shape than returning both and trusting
    every call site to pick correctly.
    """

    outcome: VerificationOutcome
    text: str
    citations: tuple[str, ...]
    failures: tuple[CitationFailure, ...] = field(default_factory=tuple)
    #: The response as the agent produced it. Kept for the invocation log and
    #: for debugging; never rendered when the outcome is WITHHELD.
    original_text: str = ""

    @property
    def may_render(self) -> bool:
        return self.outcome.may_render

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "may_render": self.may_render,
            "text": self.text,
            "citations": list(self.citations),
            "failures": [f.as_dict() for f in self.failures],
        }


def extract_citations(text: str) -> list[str]:
    """Every `[[source:id]]` in the response, in order, without duplicates."""
    seen: dict[str, None] = {}
    for source, record_id in CITATION_PATTERN.findall(text):
        seen.setdefault(f"{source}:{record_id}", None)
    return list(seen)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def _uncited_factual_sentences(text: str) -> list[str]:
    """Sentences that assert something checkable and cite nothing.

    Sentences that are questions, or that decline, are exempt — "I cannot tell
    you her salary" contains the word salary and asserts nothing.
    """
    out: list[str] = []
    for sentence in _sentences(text):
        if CITATION_PATTERN.search(sentence):
            continue
        if sentence.rstrip().endswith("?"):
            continue
        lowered = sentence.lower()
        if any(
            marker in lowered for marker in ("i cannot", "i can't", "i could not", "i am not able")
        ):
            continue
        if _FACTUAL_MARKERS.search(sentence):
            out.append(sentence)
    return out


def verify(
    text: str,
    *,
    retrieved_ids: frozenset[str],
    existing_ids: frozenset[str],
    in_scope_ids: frozenset[str] | None = None,
    forbidden_sources: frozenset[str] = frozenset(),
    require_citations: bool = True,
) -> VerifiedResponse:
    """Check every citation, and withhold the response if any fails.

    `in_scope_ids` defaults to `retrieved_ids` — if retrieval was scoped
    correctly the two are the same set, and the check only fires when something
    upstream went wrong, which is when it is worth having.

    `require_citations=False` is for an agent whose output is not a factual
    claim about records — a drafting assistant, a rephrasing. It does not skip
    verification of citations that *are* present; it only stops an uncited
    sentence from being a failure.
    """
    scope_ids = retrieved_ids if in_scope_ids is None else in_scope_ids
    citations = extract_citations(text)
    failures: list[CitationFailure] = []

    for citation_id in citations:
        source = citation_id.split(":", 1)[0]
        if source in forbidden_sources:
            failures.append(
                CitationFailure(
                    problem=CitationProblem.FORBIDDEN_SOURCE,
                    citation_id=citation_id,
                    detail=(
                        f"{source!r} is a forbidden source. Reaching this means "
                        "the retrieval guard was bypassed, which is a more "
                        "serious problem than the citation."
                    ),
                )
            )
            continue
        if citation_id not in existing_ids:
            failures.append(
                CitationFailure(
                    problem=CitationProblem.RECORD_NOT_FOUND,
                    citation_id=citation_id,
                    detail="no record has this identifier",
                )
            )
            continue
        if citation_id not in retrieved_ids:
            failures.append(
                CitationFailure(
                    problem=CitationProblem.NOT_IN_RETRIEVED_SET,
                    citation_id=citation_id,
                    detail=(
                        "the record exists but was not retrieved for this "
                        "question, so the claim was not grounded in it"
                    ),
                )
            )
            continue
        if citation_id not in scope_ids:
            failures.append(
                CitationFailure(
                    problem=CitationProblem.OUT_OF_SCOPE,
                    citation_id=citation_id,
                    detail=("the record is outside the requester's permission scope"),
                )
            )

    uncited: list[str] = []
    if require_citations:
        uncited = _uncited_factual_sentences(text)
        failures.extend(
            CitationFailure(
                problem=CitationProblem.UNCITED_CLAIM,
                citation_id=None,
                detail=f"uncited factual claim: {sentence[:160]!r}",
            )
            for sentence in uncited
        )

    if failures:
        return VerifiedResponse(
            outcome=VerificationOutcome.WITHHELD,
            text=WITHHELD_MESSAGE,
            citations=tuple(citations),
            failures=tuple(failures),
            original_text=text,
        )

    return VerifiedResponse(
        outcome=(VerificationOutcome.VERIFIED if citations else VerificationOutcome.NO_CLAIMS),
        text=text,
        citations=tuple(citations),
        original_text=text,
    )
