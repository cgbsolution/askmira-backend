"""The hiring funnel, where it stalls, and what a hire actually costs.

═══════════════════════════════════════════════════════════════════════════════
A FUNNEL THAT DOES NOT RECONCILE IS WORSE THAN NO FUNNEL
═══════════════════════════════════════════════════════════════════════════════

The failure is specific and common: a candidate is fast-tracked straight to an
interview without a recorded screen, so "screened" is 40 and "interviewed" is 43.
A recruiter looks at that once, works out the numbers do not add up, and never
opens the dashboard again — and they are right to.

So the stages here are **cumulative and nested by construction**: reaching a
later stage counts you in every earlier one, whether or not somebody recorded
the step. `reconcile` then checks the invariant and reports a breach rather than
rendering it, because a chart that silently shows more interviews than screens
is the version that destroys trust.

═══════════════════════════════════════════════════════════════════════════════
WHERE IT STALLS IS THE USEFUL NUMBER, NOT TIME TO HIRE
═══════════════════════════════════════════════════════════════════════════════

"Time to hire is 47 days" is a number nobody can act on. "Candidates sit 19 days
between interview and offer, which is 40% of the total" is a conversation with
the hiring manager. So `stage_timings` reports the median per stage and names
the worst one.

Median rather than mean: one candidate who was on holiday for a month moves a
mean and does not move a median.

═══════════════════════════════════════════════════════════════════════════════
A MISSING COST COMPONENT IS REPORTED, NOT TREATED AS ZERO
═══════════════════════════════════════════════════════════════════════════════

Cost per hire is the number that wins the conversation, and a cost per hire that
omits agency fees looks excellent and is wrong. `cost_per_hire` names what is
missing and says the figure is a floor.

Pure. All dates and amounts are supplied.
"""

from __future__ import annotations

import enum
import statistics
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

__all__ = [
    "COST_COMPONENTS",
    "CandidateJourney",
    "FunnelError",
    "Stage",
    "cost_per_hire",
    "funnel",
    "reconcile",
    "source_effectiveness",
    "stage_timings",
]


class FunnelError(ValueError):
    """A funnel figure this module refuses to produce."""


def money(value: Decimal | int | str) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class Stage(str, enum.Enum):
    """The funnel, in order. Later implies earlier."""

    APPLIED = "applied"
    SCREENED = "screened"
    INTERVIEWED = "interviewed"
    OFFERED = "offered"
    ACCEPTED = "accepted"
    JOINED = "joined"

    @property
    def rank(self) -> int:
        return list(Stage).index(self)


@dataclass(frozen=True, slots=True)
class CandidateJourney:
    """One candidate's path, as dates per stage.

    A stage with no date was not recorded. `furthest_stage` is what decides the
    funnel, not the presence of every intermediate date - fast-tracking is
    normal and a funnel that punishes it does not reconcile.
    """

    candidate_id: str
    source: str
    stage_dates: dict[Stage, date]
    requisition_id: str = ""
    department_id: str = ""
    recruiter_id: str = ""
    rejected_at_stage: Stage | None = None

    @property
    def furthest_stage(self) -> Stage | None:
        reached = [s for s in Stage if s in self.stage_dates]
        return max(reached, key=lambda s: s.rank) if reached else None

    def reached(self, stage: Stage) -> bool:
        """Whether this candidate got at least this far.

        Cumulative by construction: somebody fast-tracked to an interview
        without a recorded screen has been screened, because they could not
        otherwise be at interview. Counting only the recorded date is what makes
        "interviewed" exceed "screened".
        """
        furthest = self.furthest_stage
        return furthest is not None and furthest.rank >= stage.rank


def funnel(
    journeys: list[CandidateJourney],
    *,
    group_by: str | None = None,
) -> dict[str, Any]:
    """Counts per stage with the drop-off between them.

    Drop-off is reported as both a count and a rate, and the rate is `None`
    where the previous stage is empty - "100% drop-off from zero applicants" is
    a number that makes a dashboard look broken.
    """
    if group_by is not None and group_by not in {
        "requisition_id",
        "department_id",
        "recruiter_id",
        "source",
    }:
        raise FunnelError(
            "A funnel groups by requisition, department, recruiter or source. "
            "Anything finer describes individuals rather than a process."
        )

    def counts(rows: list[CandidateJourney]) -> list[dict[str, Any]]:
        out = []
        previous: int | None = None
        for stage in Stage:
            count = sum(1 for j in rows if j.reached(stage))
            dropped = None if previous is None else previous - count
            rate = (
                None
                if previous is None or previous == 0
                else round(dropped / previous, 3)
            )
            out.append(
                {
                    "stage": stage.value,
                    "count": count,
                    "dropped_from_previous": dropped,
                    "drop_off_rate": rate,
                }
            )
            previous = count
        return out

    overall = counts(journeys)
    worst = max(
        (row for row in overall if row["drop_off_rate"] is not None),
        key=lambda r: r["drop_off_rate"],
        default=None,
    )

    result: dict[str, Any] = {
        "total_candidates": len(journeys),
        "stages": overall,
        "largest_drop_off": (
            {
                "into_stage": worst["stage"],
                "rate": worst["drop_off_rate"],
                "count": worst["dropped_from_previous"],
            }
            if worst
            else None
        ),
        "note": (
            "Stages are cumulative: reaching a later one counts in every "
            "earlier one, whether or not somebody recorded the step. A funnel "
            "that shows more interviews than screens is one nobody opens twice."
        ),
    }

    if group_by is not None:
        groups: dict[str, list[CandidateJourney]] = {}
        for journey in journeys:
            groups.setdefault(getattr(journey, group_by) or "unassigned", []).append(
                journey
            )
        result["grouped_by"] = group_by
        result["groups"] = [
            {"key": key, "total": len(rows), "stages": counts(rows)}
            for key, rows in sorted(groups.items())
        ]
    return result


def reconcile(
    journeys: list[CandidateJourney], *, expected_totals: dict[str, int]
) -> dict[str, Any]:
    """Check the funnel against the underlying record counts.

    Two checks. The **monotonic** one is structural: a later stage can never
    exceed an earlier one, and a violation means the funnel logic is wrong. The
    **totals** one compares against counts supplied by the caller from the
    source tables, and a mismatch means the query is wrong.

    Both report rather than raise, so a dashboard can render the discrepancy
    instead of a number nobody should trust.
    """
    computed = {
        stage.value: sum(1 for j in journeys if j.reached(stage)) for stage in Stage
    }

    monotonic_breaks = []
    previous_stage = None
    for stage in Stage:
        if previous_stage is not None and computed[stage.value] > computed[
            previous_stage.value
        ]:
            monotonic_breaks.append(
                {
                    "stage": stage.value,
                    "count": computed[stage.value],
                    "previous_stage": previous_stage.value,
                    "previous_count": computed[previous_stage.value],
                    "detail": (
                        "More candidates at this stage than the one before it. "
                        "The funnel logic is wrong; a recruiter will notice "
                        "within a minute and stop believing the dashboard."
                    ),
                }
            )
        previous_stage = stage

    total_breaks = [
        {
            "stage": stage,
            "computed": computed.get(stage),
            "expected": expected,
            "difference": computed.get(stage, 0) - expected,
            "detail": (
                "The funnel disagrees with the underlying records. Either the "
                "query is filtering something the records are not, or the "
                "records have moved since."
            ),
        }
        for stage, expected in sorted(expected_totals.items())
        if computed.get(stage) != expected
    ]

    return {
        "computed": computed,
        "reconciles": not monotonic_breaks and not total_breaks,
        "monotonic_breaks": monotonic_breaks,
        "total_breaks": total_breaks,
        "note": (
            "The funnel reconciles to the underlying records at every stage."
            if not monotonic_breaks and not total_breaks
            else "The funnel does not reconcile. Show the discrepancy rather "
            "than the chart - a number a recruiter can disprove costs more "
            "trust than a missing one."
        ),
    }


#: Below this many candidates a stage median is not a median. Five is low, and
#: it is a *timing* figure rather than a person-level one, so the floor is
#: about noise rather than about identification.
MIN_FOR_STAGE_MEDIAN = 5


def stage_timings(
    journeys: list[CandidateJourney], *, minimum: int = MIN_FOR_STAGE_MEDIAN
) -> dict[str, Any]:
    """Median days in each stage, and where the process actually stalls.

    Median rather than mean: one candidate who was on holiday for a month moves
    a mean by a week and does not move a median at all.

    A reschedule does not reset the clock. The elapsed time is measured between
    the *stage dates*, so a candidate who was rescheduled three times shows the
    full delay - which is precisely the delay a hiring manager needs to see.
    """
    pairs = list(zip(list(Stage)[:-1], list(Stage)[1:], strict=True))
    rows = []
    for earlier, later in pairs:
        gaps = [
            (j.stage_dates[later] - j.stage_dates[earlier]).days
            for j in journeys
            if earlier in j.stage_dates and later in j.stage_dates
        ]
        usable = [g for g in gaps if g >= 0]
        rows.append(
            {
                "from_stage": earlier.value,
                "to_stage": later.value,
                "sample": len(usable),
                "median_days": (
                    round(statistics.median(usable), 1)
                    if len(usable) >= minimum
                    else None
                ),
                "note": (
                    ""
                    if len(usable) >= minimum
                    else f"{len(usable)} candidate(s) - too few for a median."
                ),
            }
        )

    measured = [r for r in rows if r["median_days"] is not None]
    total = sum(r["median_days"] for r in measured) if measured else 0
    worst = max(measured, key=lambda r: r["median_days"], default=None)

    joined = [
        (j.stage_dates[Stage.JOINED] - j.stage_dates[Stage.APPLIED]).days
        for j in journeys
        if Stage.JOINED in j.stage_dates and Stage.APPLIED in j.stage_dates
    ]

    return {
        "transitions": rows,
        "median_time_to_hire_days": (
            round(statistics.median(joined), 1) if len(joined) >= minimum else None
        ),
        "stalls_at": (
            {
                "from_stage": worst["from_stage"],
                "to_stage": worst["to_stage"],
                "median_days": worst["median_days"],
                "share_of_total": (
                    round(worst["median_days"] / total, 3) if total else None
                ),
                "reading": (
                    f"Candidates sit {worst['median_days']} days between "
                    f"{worst['from_stage']} and {worst['to_stage']}"
                    + (
                        f", which is {round(worst['median_days'] / total * 100)}% "
                        "of the whole process."
                        if total
                        else "."
                    )
                ),
            }
            if worst
            else None
        ),
        "note": (
            "Time to hire is a number nobody can act on. Where it stalls is a "
            "conversation with the hiring manager."
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Cost per hire
# ══════════════════════════════════════════════════════════════════════════════

#: The components a complete cost per hire needs. A figure missing any of them
#: is a floor, not a cost, and says so.
COST_COMPONENTS: tuple[str, ...] = (
    "job_board_spend",
    "agency_fees",
    "referral_bonuses",
    "recruiter_time",
)


def cost_per_hire(
    *,
    hires: int,
    components: dict[str, Decimal],
    period_label: str = "",
) -> dict[str, Any]:
    """Total recruitment cost divided by hires, with what is missing named.

    A cost per hire that omits agency fees looks excellent and is wrong, and it
    is the number a client will repeat to their board. So the missing
    components are listed and the figure is described as a floor.
    """
    if hires < 0:
        raise FunnelError("A negative hire count is a data error.")

    supplied = {k: money(v) for k, v in components.items() if v is not None}
    unknown = sorted(set(supplied) - set(COST_COMPONENTS))
    if unknown:
        raise FunnelError(
            f"Unrecognised cost component(s) {unknown}. A component nobody "
            "defined silently changes the headline number - add it to "
            f"COST_COMPONENTS deliberately. Known: {list(COST_COMPONENTS)}."
        )

    missing = [c for c in COST_COMPONENTS if c not in supplied]
    total = money(sum(supplied.values(), Decimal("0")))

    if hires == 0:
        return {
            "period": period_label,
            "hires": 0,
            "total_cost": str(total),
            "cost_per_hire": None,
            "components": {k: str(v) for k, v in sorted(supplied.items())},
            "missing_components": missing,
            "is_complete": not missing,
            "note": (
                f"{total} spent and nobody hired. A cost per hire is undefined "
                "rather than infinite, and the spend is the number that matters "
                "here."
            ),
        }

    per_hire = money(total / Decimal(hires))
    return {
        "period": period_label,
        "hires": hires,
        "total_cost": str(total),
        "cost_per_hire": str(per_hire),
        "components": {k: str(v) for k, v in sorted(supplied.items())},
        "missing_components": missing,
        "is_complete": not missing,
        "note": (
            f"{per_hire} per hire across {hires} hire(s), from all "
            f"{len(COST_COMPONENTS)} components."
            if not missing
            else f"{per_hire} per hire is a **floor**: {', '.join(missing)} "
            "were not supplied. A cost per hire that omits a component looks "
            "excellent and is wrong, and it is the number a client repeats to "
            "their board."
        ),
    }


def source_effectiveness(
    journeys: list[CandidateJourney],
    *,
    spend_by_source: dict[str, Decimal] | None = None,
    minimum: int = 10,
) -> dict[str, Any]:
    """Volume, quality, conversion and cost per source.

    Quality is the interview pass rate, not the application count. A source that
    sends four hundred applications and no hires is not a good source, and
    ranking by volume is how a recruiter ends up defending the job board that
    wastes the most of their time.
    """
    spend = {k: money(v) for k, v in (spend_by_source or {}).items()}
    by_source: dict[str, list[CandidateJourney]] = {}
    for journey in journeys:
        by_source.setdefault(journey.source or "unknown", []).append(journey)

    rows = []
    for source, rows_for in sorted(by_source.items()):
        applied = len(rows_for)
        interviewed = sum(1 for j in rows_for if j.reached(Stage.INTERVIEWED))
        offered = sum(1 for j in rows_for if j.reached(Stage.OFFERED))
        joined = sum(1 for j in rows_for if j.reached(Stage.JOINED))
        source_spend = spend.get(source)

        rows.append(
            {
                "source": source,
                "applied": applied,
                "interviewed": interviewed,
                "offered": offered,
                "joined": joined,
                "interview_pass_rate": (
                    round(offered / interviewed, 3)
                    if interviewed >= minimum
                    else None
                ),
                "conversion_to_hire": (
                    round(joined / applied, 3) if applied >= minimum else None
                ),
                "spend": str(source_spend) if source_spend is not None else None,
                "cost_per_hire": (
                    str(money(source_spend / Decimal(joined)))
                    if source_spend is not None and joined > 0
                    else None
                ),
                "note": (
                    ""
                    if applied >= minimum
                    else f"{applied} candidate(s) - too few for a rate."
                ),
            }
        )

    return {
        "sources": rows,
        "note": (
            "Quality is the interview pass rate, not the application count. A "
            "source that sends four hundred applications and no hires is not a "
            "good source, and ranking by volume is how a recruiter ends up "
            "defending the job board that wastes the most of their time."
        ),
    }
