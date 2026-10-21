"""What an HR agent declines, and how to decline half a question.

═══════════════════════════════════════════════════════════════════════════════
"HOW MUCH TAX WILL I PAY IF I RESIGN IN MARCH?"
═══════════════════════════════════════════════════════════════════════════════

That question has two halves and they need opposite treatment.

The **factual half** is computable and the employee is entitled to it: what
their year-to-date TDS is, what the notice period in their contract says, what
the March payroll cut-off date is. Refusing it because the sentence contains the
word "tax" is unhelpful theatre, and the employee will get a worse answer
somewhere else.

The **advisory half** — should you resign in March, will this leave you better
off — is tax advice. It depends on facts we do not hold, it is regulated, and if
it is wrong the person loses money and we told them to.

So `split_response` answers the first and declines the second **in the same
reply**, and names a human for the part it declined. A refusal with no route
onward is a dead end, and the person asking still needs the answer.

═══════════════════════════════════════════════════════════════════════════════
A PREDICTION ABOUT A NAMED PERSON IS NOT A FACT
═══════════════════════════════════════════════════════════════════════════════

"Priya is likely to resign in the next quarter" is a sentence a manager will act
on — a conversation, a project reassignment, an increment decision — and it is a
signal from a model, not a fact about Priya. `assert_not_certain` refuses the
certain phrasing and `hedge` rewrites it as what it is.

Pure.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from typing import Any

__all__ = [
    "RefusalOutcome",
    "RefusalReason",
    "assert_not_certain",
    "classify",
    "hedge",
    "split_response",
]


class RefusalReason(str, enum.Enum):
    """The four things this product's agents decline, and why."""

    #: The requester may not see the data the answer needs.
    OUT_OF_SCOPE = "out_of_scope"
    #: The tenant has not provided the data. Distinct from out-of-scope: the
    #: answer is "nobody has told us", not "you may not know", and conflating
    #: them tells an admin they lack permission for their own missing data.
    DATA_NOT_PROVIDED = "data_not_provided"
    #: Legal, medical or tax advice.
    ADVISORY = "advisory"
    #: A prediction about a named individual, stated as certainty.
    PREDICTION_AS_CERTAINTY = "prediction_as_certainty"
    #: The content is in a source no agent may retrieve from.
    FORBIDDEN_TOPIC = "forbidden_topic"

    @property
    def message(self) -> str:
        return {
            RefusalReason.OUT_OF_SCOPE: (
                "That is outside what you have access to. If you need it, your "
                "HR team can answer or grant the access."
            ),
            RefusalReason.DATA_NOT_PROVIDED: (
                "Your organisation has not set this up in AskMira, so I have "
                "nothing to answer from. This is a gap in the data rather than "
                "a limit on you."
            ),
            RefusalReason.ADVISORY: (
                "That part is advice rather than a fact about your record, and "
                "it depends on things I do not hold. I have answered what I can "
                "compute; for the rest, speak to a qualified adviser or your HR "
                "team."
            ),
            RefusalReason.PREDICTION_AS_CERTAINTY: (
                "I can show you a signal and what it is based on, but not a "
                "prediction about a person stated as fact."
            ),
            RefusalReason.FORBIDDEN_TOPIC: (
                "I have no access to that, deliberately. Confidential reports "
                "and disciplinary matters are outside every agent's reach — a "
                "person handles those."
            ),
        }[self]

    @property
    def route_to(self) -> str:
        """Who the person should ask instead. A refusal with no route is a wall."""
        return {
            RefusalReason.OUT_OF_SCOPE: "your HR team",
            RefusalReason.DATA_NOT_PROVIDED: "your HR team",
            RefusalReason.ADVISORY: (
                "a chartered accountant, a lawyer or a doctor, depending on the "
                "question, and your HR team for anything about your employment"
            ),
            RefusalReason.PREDICTION_AS_CERTAINTY: "the signals themselves",
            RefusalReason.FORBIDDEN_TOPIC: (
                "your HR team, or the confidential reporting channel directly"
            ),
        }[self]


#: Phrases that make a question advisory rather than factual. Matched on the
#: question, not the answer — the point is to notice before answering.
_ADVISORY_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\b(should i|would it be better|is it worth|advise me|what do you "
            r"recommend|am i better off)\b",
            re.IGNORECASE,
        ),
        "asks for a recommendation about a personal decision",
    ),
    (
        re.compile(
            r"\b(tax|deduction|80c|hra|capital gains?)\b.{0,60}\b(should|better|"
            r"save|minimi[sz]e|avoid|plan)\b",
            re.IGNORECASE,
        ),
        "asks for tax planning advice",
    ),
    (
        re.compile(
            r"\b(sue|legal action|tribunal|labour court|wrongful|my rights?)\b",
            re.IGNORECASE,
        ),
        "asks for a legal opinion",
    ),
    (
        re.compile(
            # Stems with `\w*`, not a bare `\b`. The first version wrote
            # `\bsymptom\b`, which does not match "symptoms" — the plural is how
            # anybody actually phrases it, so the medical guard matched almost
            # nothing.
            r"\b(diagnos\w*|symptom\w*|medication\w*|treatment\w*|"
            r"prescri\w*|am i (ill|unwell|sick))\b",
            re.IGNORECASE,
        ),
        "asks for a medical opinion",
    ),
)

#: Phrasings that turn a signal into a claim about a person.
_CERTAINTY_PATTERNS = (
    re.compile(r"\b(will|is going to|is about to)\s+(resign|quit|leave|fail)\b", re.I),
    re.compile(r"\bis (a )?(flight risk|underperform\w*|going to be let go)\b", re.I),
    re.compile(r"\bdefinitely\b|\bcertainly\b|\bno doubt\b", re.I),
)


@dataclass(frozen=True, slots=True)
class RefusalOutcome:
    """What is declined, what is still answerable, and who to ask."""

    reasons: tuple[RefusalReason, ...]
    #: The part of the question that can still be answered, in words. Empty
    #: where the whole question is refused.
    answerable_part: str = ""
    declined_part: str = ""

    @property
    def is_total(self) -> bool:
        return bool(self.reasons) and not self.answerable_part

    def message(self) -> str:
        if not self.reasons:
            return ""
        parts = [reason.message for reason in self.reasons]
        return " ".join(parts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "reasons": [r.value for r in self.reasons],
            "is_total": self.is_total,
            "answerable_part": self.answerable_part,
            "declined_part": self.declined_part,
            "message": self.message(),
            "route_to": [r.route_to for r in self.reasons],
        }


def classify(
    question: str,
    *,
    requested_employee_id: str | None = None,
    scope_covers: bool = True,
    data_available: bool = True,
    touches_forbidden_topic: bool = False,
) -> RefusalOutcome:
    """Which refusals apply to this question, before it is answered.

    Order matters. A forbidden topic is refused before scope is even considered,
    because "you do not have access" implies access is grantable — and for a
    Your Voice case it is not, to anybody.
    """
    reasons: list[RefusalReason] = []
    declined_notes: list[str] = []

    if touches_forbidden_topic:
        reasons.append(RefusalReason.FORBIDDEN_TOPIC)
        declined_notes.append("the confidential-reporting part")

    if requested_employee_id is not None and not scope_covers:
        reasons.append(RefusalReason.OUT_OF_SCOPE)
        declined_notes.append("the part about somebody outside your access")

    if not data_available:
        reasons.append(RefusalReason.DATA_NOT_PROVIDED)
        declined_notes.append("the part that needs data nobody has entered")

    for pattern, note in _ADVISORY_PATTERNS:
        if pattern.search(question):
            reasons.append(RefusalReason.ADVISORY)
            declined_notes.append(f"the part that {note}")
            break

    answerable = ""
    if RefusalReason.ADVISORY in reasons and len(reasons) == 1:
        # The advisory case is the one where half the question survives. A
        # question refused for scope or missing data has no factual half we can
        # answer, because we do not have the facts.
        answerable = (
            "the factual, computable part of your question — what your record actually says"
        )

    return RefusalOutcome(
        reasons=tuple(dict.fromkeys(reasons)),
        answerable_part=answerable,
        declined_part="; ".join(declined_notes),
    )


def split_response(
    *,
    factual_answer: str,
    outcome: RefusalOutcome,
) -> str:
    """Answer the factual half and decline the advisory half, in one reply.

    Refusing the whole question because part of it was advisory is the failure
    that sends somebody to a worse source. Answering the whole question is the
    failure that gives them tax advice from an HR product. This does neither.
    """
    if not outcome.reasons:
        return factual_answer

    if outcome.is_total or not factual_answer.strip():
        return outcome.message()

    return f"{factual_answer.strip()}\n\n{outcome.message()}"


def assert_not_certain(text: str) -> None:
    """Refuse a prediction about a person phrased as a fact.

    Raises. A manager reading "Priya will resign this quarter" acts on it —
    reassigns her project, skips her increment, has a conversation that changes
    her mind about staying. The model produced a probability; the sentence
    produced a decision.
    """
    for pattern in _CERTAINTY_PATTERNS:
        match = pattern.search(text)
        if match:
            raise ValueError(
                f"This states a prediction about a person as a certainty "
                f"({match.group(0)!r}). A manager will act on it. Rewrite it as "
                "the signal it is, with what it is based on and how confident "
                "the model is — hedge() does this."
            )


def hedge(text: str, *, confidence: float) -> str:
    """Rewrite a prediction as the signal it actually is.

    Not a disclaimer bolted on the end — the sentence itself changes, because a
    confident sentence followed by a caveat is read as a confident sentence.
    """
    rewritten = text
    replacements = (
        (re.compile(r"\bwill resign\b", re.I), "shows signals associated with resigning"),
        (re.compile(r"\bwill quit\b", re.I), "shows signals associated with leaving"),
        (re.compile(r"\bwill leave\b", re.I), "shows signals associated with leaving"),
        (re.compile(r"\bis a flight risk\b", re.I), "shows elevated flight-risk signals"),
        (re.compile(r"\bis going to\b", re.I), "may"),
        (re.compile(r"\bdefinitely\b|\bcertainly\b", re.I), "possibly"),
    )
    for pattern, replacement in replacements:
        rewritten = pattern.sub(replacement, rewritten)

    return (
        f"{rewritten.rstrip('.')}. This is a model signal at "
        f"{confidence:.0%} confidence, not a prediction — it describes patterns "
        "in the record, and the person is the only one who knows their plans."
    )
