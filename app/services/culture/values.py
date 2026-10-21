"""The values framework, and the analytics that make it more than a poster.

═══════════════════════════════════════════════════════════════════════════════
WHICH VALUES ARE ACTUALLY LIVED, BY WHOM, TRENDING WHICH WAY
═══════════════════════════════════════════════════════════════════════════════

Every company has values on a wall. Almost none can say which of them shows up
in what people actually recognise each other for. Tagging shoutouts, award
nominations and appraisal competencies to the same small set of values turns
that into a measurement: "Customer obsession" appearing on 2% of recognition in
a company that says it is the first value is a finding, and it is a finding
nobody can get from a survey because a survey asks people what they believe
rather than watching what they do.

═══════════════════════════════════════════════════════════════════════════════
AND IT IS EVIDENCE, NOT A SCORE
═══════════════════════════════════════════════════════════════════════════════

The same rule as `CompetencyValueLink`: recognition counts are shown *beside* a
rating, never converted into one. A value tally turned into a performance number
is a popularity metric wearing a value's name — it rewards visibility over
contribution and is trivially gamed the moment anybody works out it counts.

So nothing here returns a per-person score. `coverage` and `trend` describe the
company and its teams; `for_person` returns the tagged items themselves so a
manager reads the citations rather than a number.

Pure.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date
from typing import Any

__all__ = [
    "MIN_GROUP_FOR_VALUE_BREAKDOWN",
    "TagSource",
    "TaggedItem",
    "ValueDefinition",
    "ValuesError",
    "by_group",
    "coverage",
    "for_person",
    "trend",
    "unlived_values",
]


class ValuesError(ValueError):
    """A values operation this module refuses."""


#: A value breakdown for a team is a breakdown of who recognised whom, and in a
#: team of three it names people. The same floor as the survey threshold, for
#: the same reason.
MIN_GROUP_FOR_VALUE_BREAKDOWN = 5


class TagSource(str, enum.Enum):
    """Where a value tag came from.

    Kept separate because they are not equivalent evidence. A peer shoutout is
    cheap and frequent; an award nomination is scarce and adjudicated; an
    appraisal competency is a manager's judgement. Summing them into one count
    lets a hundred shoutouts outweigh every award in the company.
    """

    SHOUTOUT = "shoutout"
    NOMINATION = "nomination"
    COMPETENCY = "competency"


@dataclass(frozen=True, slots=True)
class ValueDefinition:
    """One company value."""

    code: str
    name: str
    description: str
    icon: str = ""
    colour: str = ""

    def __post_init__(self) -> None:
        if not self.code.strip():
            raise ValuesError("A value needs a stable code; the name can change.")
        if not self.description.strip():
            raise ValuesError(
                f"Value {self.code!r} has no description. A one-word value on a "
                "dropdown gets tagged by whoever is guessing hardest, and the "
                "analytics then measure the guessing."
            )


@dataclass(frozen=True, slots=True)
class TaggedItem:
    """One thing tagged to a value: a shoutout, a nomination, a competency."""

    item_id: str
    source: TagSource
    value_code: str
    subject_id: str
    occurred_on: date
    group_key: str = ""
    excerpt: str = ""


def coverage(
    items: list[TaggedItem],
    *,
    values: list[ValueDefinition],
    sources: tuple[TagSource, ...] = (TagSource.SHOUTOUT, TagSource.NOMINATION),
) -> dict[str, Any]:
    """How much of the company's recognition names each value.

    Counted per source rather than pooled: a hundred shoutouts and one award
    are not a hundred and one pieces of evidence, and pooling them lets the
    cheap signal drown the scarce one.
    """
    known = {v.code for v in values}
    unknown = sorted({i.value_code for i in items} - known)
    if unknown:
        raise ValuesError(
            f"Tagged to value code(s) {unknown} that the framework does not "
            "define. A tally over codes nobody defined is a tally of typos."
        )

    per_value: dict[str, dict[str, int]] = {v.code: {s.value: 0 for s in sources} for v in values}
    totals = {s.value: 0 for s in sources}

    for item in items:
        if item.source not in sources:
            continue
        per_value[item.value_code][item.source.value] += 1
        totals[item.source.value] += 1

    return {
        "values": [
            {
                "code": v.code,
                "name": v.name,
                "counts": per_value[v.code],
                "share": {
                    source: (
                        round(per_value[v.code][source] / totals[source], 3)
                        if totals[source]
                        else None
                    )
                    for source in totals
                },
            }
            for v in values
        ],
        "totals": totals,
        "note": (
            "Counted per source. A shoutout is cheap and frequent, an award "
            "nomination is scarce and adjudicated; pooling them lets the first "
            "drown the second."
        ),
    }


def unlived_values(
    items: list[TaggedItem],
    *,
    values: list[ValueDefinition],
    floor: float = 0.05,
) -> list[dict[str, Any]]:
    """Values almost nothing is tagged to, which is the interesting finding.

    A value on the wall that appears in 1% of recognition is either the wrong
    value or one nobody has been shown how to act on. Both are worth knowing
    and neither shows up in a survey, because a survey asks what people believe
    rather than watching what they do.
    """
    report = coverage(items, values=values)
    total = sum(report["totals"].values())
    out: list[dict[str, Any]] = []

    for row in report["values"]:
        count = sum(row["counts"].values())
        share = count / total if total else 0.0
        if share < floor:
            out.append(
                {
                    "code": row["code"],
                    "name": row["name"],
                    "count": count,
                    "share": round(share, 3),
                    "reading": (
                        f"{row['name']} appears on {round(share * 100, 1)}% of "
                        "tagged recognition. Either it is not how this company "
                        "actually works, or nobody has been shown what it looks "
                        "like in practice. Both are worth knowing; neither is a "
                        "reason to remove it this quarter."
                    ),
                }
            )
    return out


def trend(
    items: list[TaggedItem],
    *,
    value_code: str,
    periods: list[tuple[str, date, date]],
) -> dict[str, Any]:
    """One value's count across named periods.

    Counts rather than rates, with the period labels supplied by the caller. A
    "trending up" arrow computed from two months where one had a company
    offsite is a claim the data does not support, so the periods are named and
    the reader can see what they are comparing.
    """
    buckets = []
    for label, start, end in periods:
        if end < start:
            raise ValuesError(f"Period {label!r} ends before it starts.")
        count = sum(
            1 for i in items if i.value_code == value_code and start <= i.occurred_on <= end
        )
        buckets.append(
            {
                "label": label,
                "from": start.isoformat(),
                "to": end.isoformat(),
                "count": count,
            }
        )

    return {
        "value_code": value_code,
        "periods": buckets,
        "note": (
            "Counts per named period. Direction is left to the reader - two "
            "months where one had an offsite is not a trend."
        ),
    }


def by_group(
    items: list[TaggedItem],
    *,
    values: list[ValueDefinition],
    group_sizes: dict[str, int],
    min_group: int = MIN_GROUP_FOR_VALUE_BREAKDOWN,
) -> dict[str, Any]:
    """Per-team value coverage, with small teams withheld.

    A values breakdown for a team of three is a list of who recognised whom.
    The same floor as the survey threshold, for the same reason - and the count
    is still shown, because "withheld" with no number sends somebody to ask HR.
    """
    grouped: dict[str, list[TaggedItem]] = {}
    for item in items:
        grouped.setdefault(item.group_key, []).append(item)

    out = []
    for key in sorted(set(grouped) | set(group_sizes)):
        size = group_sizes.get(key, 0)
        if size < min_group:
            out.append(
                {
                    "group_key": key,
                    "headcount": size,
                    "visible": False,
                    "values": [],
                    "note": (
                        f"{size} people, below the {min_group} needed. A values "
                        "breakdown for a team this size is a list of who "
                        "recognised whom."
                    ),
                }
            )
            continue
        report = coverage(grouped.get(key, []), values=values)
        out.append(
            {
                "group_key": key,
                "headcount": size,
                "visible": True,
                "values": report["values"],
                "note": "",
            }
        )
    return {"groups": out, "min_group": min_group}


def for_person(
    items: list[TaggedItem], *, subject_id: str, value_code: str | None = None
) -> dict[str, Any]:
    """The tagged items themselves, for a manager to read.

    Deliberately not a score. Recognition converted into a rating is a
    popularity metric wearing a value's name: it rewards visibility over
    contribution and is gamed the moment anybody works out it counts. Shown as
    the citations themselves, it is context a manager weighs - which is what it
    actually is.
    """
    rows = [
        i
        for i in items
        if i.subject_id == subject_id and (value_code is None or i.value_code == value_code)
    ]
    return {
        "subject_id": subject_id,
        "items": [
            {
                "item_id": i.item_id,
                "source": i.source.value,
                "value_code": i.value_code,
                "occurred_on": i.occurred_on.isoformat(),
                "excerpt": i.excerpt,
            }
            for i in sorted(rows, key=lambda i: i.occurred_on, reverse=True)
        ],
        "note": (
            "Evidence, not a score. These sit beside a rating for a manager to "
            "weigh; converted into one they would reward being visible over "
            "doing the work."
        ),
    }
