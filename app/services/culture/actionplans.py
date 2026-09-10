"""Survey action planning: the half that decides whether the survey survives.

═══════════════════════════════════════════════════════════════════════════════
SURVEYS WITHOUT ACTION PLANNING ARE WHY ENGAGEMENT TOOLS GET ABANDONED
═══════════════════════════════════════════════════════════════════════════════

The failure is completely predictable and it happens in the second year. Wave
one gets 80% participation because people believe something will change. Nothing
visible changes. Wave two gets 45%, mostly from people being chased. Wave three
is cancelled because "the data was not reliable anyway", and the company
concludes that engagement surveys do not work.

Nothing was wrong with the survey. The loop was never closed.

So an action plan here is not a text box. It has an owner who is a person, a due
date, and a status that can only reach `COMPLETED` through work that was
recorded — and `loop_closure` reports, per theme, whether anything was actually
done and whether the next wave moved. That report is the product.

═══════════════════════════════════════════════════════════════════════════════
AND "COMPLETED" HAS TO MEAN SOMETHING
═══════════════════════════════════════════════════════════════════════════════

A plan that can be marked complete with no action items done is a checkbox, and
a checkbox is what the second year looks like. `can_complete` says no, and names
what is outstanding.

Pure. `today` is always supplied.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date
from typing import Any

__all__ = [
    "ActionItem",
    "ActionPlan",
    "ActionPlanError",
    "PlanScope",
    "PlanStatus",
    "can_complete",
    "loop_closure",
    "overdue",
]


class ActionPlanError(ValueError):
    """An action-plan operation this module refuses."""


class PlanScope(str, enum.Enum):
    COMPANY = "company"
    DEPARTMENT = "department"
    TEAM = "team"


class PlanStatus(str, enum.Enum):
    DRAFT = "draft"
    #: Committed to, with an owner and a date. This is the state that matters:
    #: it is what an employee was told would happen.
    COMMITTED = "committed"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    #: Abandoned openly. Better than a plan that quietly sits at 20% forever -
    #: people can accept a reversed decision and cannot accept silence.
    DROPPED = "dropped"

    @property
    def is_open(self) -> bool:
        return self in (
            PlanStatus.DRAFT,
            PlanStatus.COMMITTED,
            PlanStatus.IN_PROGRESS,
        )


@dataclass(frozen=True, slots=True)
class ActionItem:
    """One thing somebody said they would do."""

    item_id: str
    description: str
    owner_id: str
    due_date: date
    done_on: date | None = None
    dropped_reason: str = ""

    def __post_init__(self) -> None:
        if not self.description.strip():
            raise ActionPlanError("An action item with no description is a tick box.")
        if not self.owner_id.strip():
            raise ActionPlanError(
                "An action item needs a named owner. 'The team' owns nothing, "
                "and an item owned by nobody is the one still open next year."
            )

    @property
    def is_done(self) -> bool:
        return self.done_on is not None

    @property
    def is_dropped(self) -> bool:
        return bool(self.dropped_reason.strip()) and not self.is_done

    @property
    def is_open(self) -> bool:
        return not self.is_done and not self.is_dropped

    def is_overdue(self, today: date) -> bool:
        return self.is_open and today > self.due_date


@dataclass(frozen=True, slots=True)
class ActionPlan:
    """What one group committed to after seeing its results."""

    plan_id: str
    survey_id: str
    scope: PlanScope
    scope_key: str
    theme: str
    owner_id: str
    due_date: date
    status: PlanStatus
    items: tuple[ActionItem, ...] = ()
    progress_notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def done_count(self) -> int:
        return sum(1 for i in self.items if i.is_done)

    @property
    def open_items(self) -> tuple[ActionItem, ...]:
        return tuple(i for i in self.items if i.is_open)

    @property
    def completion(self) -> float:
        """Dropped items count as resolved, not as done.

        Counting them as done inflates the number; counting them as open leaves
        a plan permanently at 80% because somebody sensibly changed their mind.
        """
        resolved = sum(1 for i in self.items if not i.is_open)
        return round(resolved / len(self.items), 2) if self.items else 0.0

    def as_dict(self, *, today: date) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "survey_id": self.survey_id,
            "scope": self.scope.value,
            "scope_key": self.scope_key,
            "theme": self.theme,
            "owner_id": self.owner_id,
            "due_date": self.due_date.isoformat(),
            "status": self.status.value,
            "item_count": len(self.items),
            "done_count": self.done_count,
            "completion": self.completion,
            "overdue_items": [
                i.item_id for i in self.items if i.is_overdue(today)
            ],
            "progress_notes": list(self.progress_notes),
        }


def can_complete(plan: ActionPlan) -> tuple[bool, str]:
    """Whether this plan may be marked complete, and what is missing.

    A plan completed with open items is a checkbox, and a checkbox is what the
    second year of a dying survey programme is made of. An item that will not
    happen can be dropped with a reason - which is honest and visible - but it
    cannot simply be left open while the plan is called done.
    """
    if not plan.items:
        return False, (
            "This plan has no action items. Completing it would record that "
            "something was done about the theme when nothing was written down, "
            "which is exactly the pattern that kills a survey programme."
        )

    open_items = plan.open_items
    if open_items:
        names = ", ".join(i.description[:48] for i in open_items[:3])
        more = f" and {len(open_items) - 3} more" if len(open_items) > 3 else ""
        return False, (
            f"{len(open_items)} item(s) are still open: {names}{more}. Mark them "
            "done, or drop them with a reason - a dropped item people can see is "
            "better than an open one nobody mentions."
        )

    return True, "Every item is done or explicitly dropped."


def overdue(plans: list[ActionPlan], *, today: date) -> list[dict[str, Any]]:
    """Plans past their date with work still open, oldest first.

    Named and dated rather than counted, because the useful output is a list
    somebody can work through.
    """
    rows = [
        {
            "plan_id": p.plan_id,
            "theme": p.theme,
            "scope": p.scope.value,
            "scope_key": p.scope_key,
            "owner_id": p.owner_id,
            "due_date": p.due_date.isoformat(),
            "days_late": (today - p.due_date).days,
            "open_items": len(p.open_items),
        }
        for p in plans
        if p.status.is_open and today > p.due_date and p.open_items
    ]
    return sorted(rows, key=lambda r: r["days_late"], reverse=True)


def loop_closure(
    *,
    plans: list[ActionPlan],
    movement: dict[str, float],
    today: date,
) -> dict[str, Any]:
    """Per theme: was anything done, and did the next wave move.

    This is the report the whole module exists to produce. Either half alone is
    misleading - action with no movement is effort in the wrong place, and
    movement with no action is something else that happened, quite possibly a
    reorganisation or a bonus.

    `movement` is the delta per theme from `surveys.compare_waves`. A theme with
    no entry there has not been re-surveyed, and is reported as such rather than
    as zero: not measured and did not move are different findings.
    """
    by_theme: dict[str, list[ActionPlan]] = {}
    for plan in plans:
        by_theme.setdefault(plan.theme, []).append(plan)

    rows = []
    for theme in sorted(set(by_theme) | set(movement)):
        theme_plans = by_theme.get(theme, [])
        items = [i for p in theme_plans for i in p.items]
        done = sum(1 for i in items if i.is_done)
        acted = done > 0
        delta = movement.get(theme)

        if delta is None:
            reading = (
                f"{done} action(s) completed. Not re-surveyed, so there is no "
                "movement to report - that is a gap in the measurement, not a "
                "flat result."
            )
        elif acted and delta > 0:
            reading = (
                f"{done} action(s) completed and the theme moved +{delta}. The "
                "loop closed."
            )
        elif acted and delta <= 0:
            reading = (
                f"{done} action(s) completed and the theme moved {delta}. Effort "
                "went somewhere; it did not go where the score is. Worth asking "
                "the group what they meant before doing more of the same."
            )
        elif not acted and delta > 0:
            reading = (
                f"Nothing was completed and the theme still moved +{delta}. "
                "Something else changed - do not book this as a win for a plan "
                "that did not happen."
            )
        else:
            reading = (
                f"Nothing was completed and the theme moved {delta}. This is the "
                "state that ends survey programmes: people answered, nothing "
                "happened, and they will notice before the next wave."
            )

        rows.append(
            {
                "theme": theme,
                "plans": len(theme_plans),
                "items": len(items),
                "items_done": done,
                "acted": acted,
                "movement": delta,
                "reading": reading,
            }
        )

    open_plans = [p for p in plans if p.status.is_open]
    return {
        "as_of": today.isoformat(),
        "themes": rows,
        "open_plans": len(open_plans),
        "overdue_plans": len(overdue(plans, today=today)),
        "note": (
            "Action and movement are reported together. Either on its own "
            "supports a conclusion the data does not."
        ),
    }
