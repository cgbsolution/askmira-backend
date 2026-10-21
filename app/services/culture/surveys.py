"""Survey scoring, eNPS, validated instruments and the anonymity threshold.

═══════════════════════════════════════════════════════════════════════════════
THE ANONYMITY THRESHOLD IS THE WHOLE PRODUCT
═══════════════════════════════════════════════════════════════════════════════

An engagement survey works because people answer it honestly, and they answer it
honestly exactly once — the first time a manager works out who said what, the
instrument is dead for that company and no amount of policy brings it back.

So a breakdown below `MIN_RESPONSES` is not shown, at any level, to anybody. Not
to HR, not to the CEO, not "just the average". A team of three has no average
that is not three named opinions.

═══════════════════════════════════════════════════════════════════════════════
AND SUPPRESSION HAS TO BE COMPLEMENTARY, OR IT IS THEATRE
═══════════════════════════════════════════════════════════════════════════════

This is the part most products get wrong. Suppose Engineering has 40 responses
across four teams and one team of four is hidden. The company shows the
Engineering total and the three visible teams — and the hidden team's score is
the remainder, recoverable with arithmetic a manager can do in their head.

So when one group in a breakdown is suppressed, the next-smallest is suppressed
too. It costs one extra group of visibility and it is the difference between a
threshold and the appearance of one.

═══════════════════════════════════════════════════════════════════════════════
eNPS IS THE STANDARD QUESTION OR IT IS NOT eNPS
═══════════════════════════════════════════════════════════════════════════════

0-10, promoters 9-10, passives 7-8, detractors 0-6, score = %promoters -
%detractors. A house variant on a five-point scale produces a number that cannot
be compared with anything, which defeats the only reason to use a standard
instrument.

Pure. No session, no models, no clock.
"""

from __future__ import annotations

import enum
import statistics
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "INSTRUMENTS",
    "MIN_RESPONSES",
    "BreakdownResult",
    "Instrument",
    "InstrumentItem",
    "ScaleKind",
    "Suppression",
    "SurveyError",
    "breakdown",
    "compare_waves",
    "enps",
    "instrument",
    "score_scale",
]


class SurveyError(ValueError):
    """A survey result this module refuses to produce."""


#: Below this many responses, a group's result is not shown to anybody. Five is
#: the common floor in the research-ethics literature and in the products people
#: trust; it is low enough that a genuine small team eventually gets a reading
#: and high enough that a single answer is not identifiable by elimination.
MIN_RESPONSES = 5


class ScaleKind(str, enum.Enum):
    """What the numbers on an item mean.

    Stored on the instrument rather than assumed, because a delta between a
    five-point and a seven-point scale is a fabricated number, and the two look
    identical once they are averages.
    """

    #: 1-5 agreement.
    LIKERT_5 = "likert_5"
    #: 1-7 agreement. Used by several validated wellbeing instruments.
    LIKERT_7 = "likert_7"
    #: 0-10. eNPS and single-item wellbeing scales.
    ZERO_TO_TEN = "zero_to_ten"
    #: Yes/no. Averaged as a proportion, never as a mean of 0 and 1 dressed up
    #: as a score out of five.
    BINARY = "binary"

    @property
    def bounds(self) -> tuple[int, int]:
        return {
            ScaleKind.LIKERT_5: (1, 5),
            ScaleKind.LIKERT_7: (1, 7),
            ScaleKind.ZERO_TO_TEN: (0, 10),
            ScaleKind.BINARY: (0, 1),
        }[self]


@dataclass(frozen=True, slots=True)
class InstrumentItem:
    """One question, and which way it points.

    `reverse_scored` matters: an item worded negatively ("I often think about
    leaving") has to be flipped before it is averaged with the rest, or a
    healthy team reads as a sick one.
    """

    key: str
    text: str
    reverse_scored: bool = False
    #: Which company value or theme this item belongs to, for the breakdown.
    theme: str = ""


@dataclass(frozen=True, slots=True)
class Instrument:
    """A named, versioned set of questions on one scale."""

    key: str
    name: str
    scale: ScaleKind
    items: tuple[InstrumentItem, ...]
    source: str
    #: Why this one, in a sentence a client can be shown.
    rationale: str = ""

    @property
    def item_count(self) -> int:
        return len(self.items)

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "scale": self.scale.value,
            "bounds": list(self.scale.bounds),
            "source": self.source,
            "rationale": self.rationale,
            "items": [
                {
                    "key": item.key,
                    "text": item.text,
                    "reverse_scored": item.reverse_scored,
                    "theme": item.theme,
                }
                for item in self.items
            ],
        }


#: The eNPS question, worded as the instrument defines it. Rewording it is how a
#: benchmark stops being a benchmark.
ENPS_QUESTION = (
    "How likely are you to recommend this company as a place to work to a friend or colleague?"
)


INSTRUMENTS: dict[str, Instrument] = {
    "enps": Instrument(
        key="enps",
        name="Employee Net Promoter Score",
        scale=ScaleKind.ZERO_TO_TEN,
        items=(InstrumentItem(key="enps", text=ENPS_QUESTION, theme="advocacy"),),
        source="Reichheld's NPS, applied to employees.",
        rationale=(
            "One question, universally understood, and comparable with every "
            "other company that asks it the same way. It says nothing about "
            "why, which is what the engagement instrument is for."
        ),
    ),
    "engagement_9": Instrument(
        key="engagement_9",
        name="Engagement (nine item)",
        scale=ScaleKind.LIKERT_5,
        items=(
            InstrumentItem("clarity", "I know what is expected of me at work.", theme="clarity"),
            InstrumentItem(
                "resources",
                "I have what I need to do my work well.",
                theme="enablement",
            ),
            InstrumentItem(
                "strengths",
                "I get to do what I am good at most days.",
                theme="enablement",
            ),
            InstrumentItem(
                "recognition",
                "I have been recognised for good work recently.",
                theme="recognition",
            ),
            InstrumentItem(
                "care",
                "Someone at work seems to care about me as a person.",
                theme="belonging",
            ),
            InstrumentItem(
                "growth",
                "There is someone here who encourages my development.",
                theme="growth",
            ),
            InstrumentItem("voice", "My opinions seem to count here.", theme="voice"),
            InstrumentItem(
                "purpose",
                "The purpose of this company makes my work feel important.",
                theme="purpose",
            ),
            InstrumentItem(
                "quality",
                "My colleagues are committed to doing good work.",
                theme="standards",
            ),
        ),
        source="Adapted from the Gallup Q12 item structure.",
        rationale=(
            "Nine items covering the drivers a manager can actually change. A "
            "twelve-item survey run quarterly gets abandoned by the third wave."
        ),
    ),
    "wellbeing_5": Instrument(
        key="wellbeing_5",
        name="Wellbeing (WHO-5 style)",
        scale=ScaleKind.LIKERT_5,
        items=(
            InstrumentItem("cheerful", "I have felt cheerful and in good spirits.", theme="affect"),
            InstrumentItem("calm", "I have felt calm and relaxed.", theme="affect"),
            InstrumentItem("active", "I have felt active and vigorous.", theme="energy"),
            InstrumentItem("rested", "I woke up feeling fresh and rested.", theme="energy"),
            InstrumentItem(
                "interest",
                "My daily life has been filled with things that interest me.",
                theme="interest",
            ),
        ),
        source="Structured after the WHO-5 Wellbeing Index.",
        rationale=(
            "Wellbeing is not engagement and a company that measures only "
            "engagement will read burnout as commitment. Five items, no "
            "clinical claim, and never reported for an individual."
        ),
    ),
    "inclusion_6": Instrument(
        key="inclusion_6",
        name="Inclusion (six item)",
        scale=ScaleKind.LIKERT_5,
        items=(
            InstrumentItem("belong", "I feel I belong here.", theme="belonging"),
            InstrumentItem(
                "voice_safe",
                "I can raise a difficult issue without it being held against me.",
                theme="safety",
            ),
            InstrumentItem(
                "fair_reward",
                "Decisions about pay and promotion here are made fairly.",
                theme="fairness",
            ),
            InstrumentItem(
                "respected",
                "I am treated with respect regardless of who I am.",
                theme="respect",
            ),
            InstrumentItem(
                "heard",
                "People like me are heard when decisions are made.",
                theme="voice",
            ),
            InstrumentItem(
                "hide",
                "I have to hide parts of who I am to fit in here.",
                reverse_scored=True,
                theme="authenticity",
            ),
        ),
        source="Composed from published inclusion and psychological-safety scales.",
        rationale=(
            "Inclusion questions are the ones people most need to answer "
            "anonymously, and the ones a small-team breakdown most obviously "
            "identifies. The threshold is not optional here."
        ),
    ),
}


def instrument(key: str) -> Instrument:
    if key not in INSTRUMENTS:
        raise SurveyError(
            f"No instrument {key!r}. Running an ad-hoc question set is fine, but "
            "it is not one of the validated instruments and must not be reported "
            f"as one. Known: {sorted(INSTRUMENTS)}."
        )
    return INSTRUMENTS[key]


# ══════════════════════════════════════════════════════════════════════════════
# eNPS
# ══════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class EnpsResult:
    """An eNPS with the counts behind it.

    The counts travel with the score because -12 from eleven people and -12 from
    eleven hundred are different facts, and a dashboard showing only the number
    invites the same reaction to both.
    """

    responses: int
    promoters: int
    passives: int
    detractors: int
    score: int
    suppressed: bool
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "responses": self.responses,
            "promoters": self.promoters,
            "passives": self.passives,
            "detractors": self.detractors,
            "score": None if self.suppressed else self.score,
            "suppressed": self.suppressed,
            "question": ENPS_QUESTION,
            "note": self.note,
        }


def enps(scores: list[int], *, min_responses: int = MIN_RESPONSES) -> EnpsResult:
    """The standard scoring, and nothing else.

    Promoters 9-10, passives 7-8, detractors 0-6, score = %promoters -
    %detractors, rounded to a whole number. A house variant produces a figure
    that cannot be compared with anything, which is the only reason to use a
    standard instrument in the first place.
    """
    for value in scores:
        if not 0 <= value <= 10:
            raise SurveyError(
                f"{value} is not an eNPS response. The scale is 0-10; a survey "
                "collecting anything else is not measuring eNPS and its number "
                "must not be published as one."
            )

    total = len(scores)
    promoters = sum(1 for s in scores if s >= 9)
    passives = sum(1 for s in scores if 7 <= s <= 8)
    detractors = sum(1 for s in scores if s <= 6)

    if total == 0:
        return EnpsResult(0, 0, 0, 0, 0, True, "No responses.")

    raw = round((promoters - detractors) * 100 / total)
    if total < min_responses:
        return EnpsResult(
            responses=total,
            promoters=promoters,
            passives=passives,
            detractors=detractors,
            score=raw,
            suppressed=True,
            note=(
                f"{total} response(s) — below the {min_responses} needed to "
                "report a score. With this few, the number is a list of named "
                "opinions with the names removed."
            ),
        )

    return EnpsResult(
        responses=total,
        promoters=promoters,
        passives=passives,
        detractors=detractors,
        score=raw,
        suppressed=False,
        note=(
            f"{promoters} promoter(s), {passives} passive(s) and "
            f"{detractors} detractor(s) of {total}."
        ),
    )


# ══════════════════════════════════════════════════════════════════════════════
# Scale scoring
# ══════════════════════════════════════════════════════════════════════════════


def score_scale(
    responses: list[dict[str, int]],
    *,
    instrument_key: str,
) -> dict[str, float]:
    """Mean per item, reverse-scored items flipped first.

    Averaging a negatively worded item alongside the rest reads a healthy team
    as a sick one, and it is invisible in the output — which is why the flag
    lives on the item rather than in a caller's memory.
    """
    inst = instrument(instrument_key)
    low, high = inst.scale.bounds

    per_item: dict[str, list[float]] = {item.key: [] for item in inst.items}
    for response in responses:
        for item in inst.items:
            if item.key not in response:
                continue
            value = response[item.key]
            if not low <= value <= high:
                raise SurveyError(
                    f"{value} is outside the {low}-{high} range of "
                    f"{inst.scale.value} for item {item.key!r}."
                )
            per_item[item.key].append(
                float(low + high - value) if item.reverse_scored else float(value)
            )

    return {key: round(statistics.fmean(values), 2) for key, values in per_item.items() if values}


# ══════════════════════════════════════════════════════════════════════════════
# Breakdown, and the suppression that makes it safe
# ══════════════════════════════════════════════════════════════════════════════


class Suppression(str, enum.Enum):
    """Why a group's result is not shown."""

    NONE = "none"
    #: Fewer responses than the threshold.
    BELOW_THRESHOLD = "below_threshold"
    #: Above the threshold, but shown alone it would let the suppressed group's
    #: result be recovered by subtraction.
    COMPLEMENTARY = "complementary"


@dataclass(frozen=True, slots=True)
class BreakdownResult:
    """One group's slice of a survey, or the reason it is withheld."""

    key: str
    responses: int
    scores: dict[str, float] = field(default_factory=dict)
    suppression: Suppression = Suppression.NONE
    note: str = ""

    @property
    def is_visible(self) -> bool:
        return self.suppression is Suppression.NONE

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            # The count is shown even when the scores are not: "too few to
            # report" with no number invites somebody to ask HR for the number.
            "responses": self.responses,
            "scores": self.scores if self.is_visible else {},
            "visible": self.is_visible,
            "suppression": self.suppression.value,
            "note": self.note,
        }


def breakdown(
    groups: dict[str, list[dict[str, int]]],
    *,
    instrument_key: str,
    min_responses: int = MIN_RESPONSES,
    parent_total_shown: bool = True,
) -> list[BreakdownResult]:
    """Per-group results, with everything below the threshold withheld.

    Complementary suppression is the part that matters. If Engineering's total
    is shown and one of its four teams is hidden, the hidden team's score is the
    remainder — arithmetic a manager can do in their head. So when a group is
    suppressed and the parent total is visible, the next-smallest visible group
    is suppressed too.

    `parent_total_shown=False` says the caller is not publishing a total that
    the suppressed group could be subtracted from, and the second suppression is
    unnecessary.
    """
    results = [
        BreakdownResult(
            key=key,
            responses=len(rows),
            scores=score_scale(rows, instrument_key=instrument_key) if rows else {},
        )
        for key, rows in sorted(groups.items())
    ]

    below = [r for r in results if r.responses < min_responses]
    out: dict[str, BreakdownResult] = {
        r.key: (
            r
            if r.responses >= min_responses
            else BreakdownResult(
                key=r.key,
                responses=r.responses,
                scores={},
                suppression=Suppression.BELOW_THRESHOLD,
                note=(
                    f"{r.responses} response(s), below the {min_responses} "
                    "needed. A team this size has no average that is not a few "
                    "named opinions."
                ),
            )
        )
        for r in results
    }

    if below and parent_total_shown:
        visible = sorted((r for r in out.values() if r.is_visible), key=lambda r: r.responses)
        if visible:
            victim = visible[0]
            out[victim.key] = BreakdownResult(
                key=victim.key,
                responses=victim.responses,
                scores={},
                suppression=Suppression.COMPLEMENTARY,
                note=(
                    "Withheld so the suppressed group's result cannot be "
                    "recovered by subtracting the visible groups from the "
                    "total. Suppressing only the small group would be the "
                    "appearance of a threshold rather than one."
                ),
            )

    return [out[r.key] for r in results]


# ══════════════════════════════════════════════════════════════════════════════
# Movement between waves
# ══════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class WaveComparison:
    """What moved between two runs of the same instrument."""

    instrument_key: str
    before_responses: int
    after_responses: int
    deltas: dict[str, float]
    comparable: bool
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "instrument_key": self.instrument_key,
            "before_responses": self.before_responses,
            "after_responses": self.after_responses,
            "deltas": self.deltas if self.comparable else {},
            "comparable": self.comparable,
            "note": self.note,
        }


def compare_waves(
    *,
    before: list[dict[str, int]],
    after: list[dict[str, int]],
    before_instrument: str,
    after_instrument: str,
    min_responses: int = MIN_RESPONSES,
) -> WaveComparison:
    """Movement, but only where the two waves can honestly be compared.

    Refuses across instruments and across scales: a delta between a five-point
    and a seven-point average is a fabricated number, and once both are printed
    as "3.8 → 4.4" nothing in the output shows that they measured different
    things.

    Also refuses where either wave is below the threshold, because a movement
    computed from four responses is the same disclosure risk as a score from
    four responses, arrived at more slowly.
    """
    if before_instrument != after_instrument:
        return WaveComparison(
            instrument_key=after_instrument,
            before_responses=len(before),
            after_responses=len(after),
            deltas={},
            comparable=False,
            note=(
                f"The first wave used {before_instrument!r} and the second "
                f"{after_instrument!r}. A movement between two different "
                "instruments measures the change of instrument."
            ),
        )

    if len(before) < min_responses or len(after) < min_responses:
        return WaveComparison(
            instrument_key=after_instrument,
            before_responses=len(before),
            after_responses=len(after),
            deltas={},
            comparable=False,
            note=(
                f"One of the waves is below {min_responses} responses. A "
                "movement computed from a handful of answers carries the same "
                "disclosure risk as the scores themselves."
            ),
        )

    first = score_scale(before, instrument_key=before_instrument)
    second = score_scale(after, instrument_key=after_instrument)
    shared = sorted(set(first) & set(second))

    return WaveComparison(
        instrument_key=after_instrument,
        before_responses=len(before),
        after_responses=len(after),
        deltas={key: round(second[key] - first[key], 2) for key in shared},
        comparable=True,
        note=(
            f"{len(shared)} item(s) present in both waves. Movement is against "
            "the same instrument on the same scale."
        ),
    )
