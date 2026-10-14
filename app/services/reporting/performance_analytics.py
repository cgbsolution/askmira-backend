"""Performance analytics: ratings over cycles, goal attainment, rater spread.

Scoped like the rest of people analytics — HR sees the company, a line manager
their reporting line. Rater spread compares one manager's ratings with
everybody else's, so only HR sees it.

**Spread, not bias.** A manager whose ratings run above the company mean may
have a stronger team. The rater table shows the gap, the number of ratings
behind it and the rule that flagged it; what it means is a calibration
conversation, not a conclusion this code draws.
"""

from __future__ import annotations

import statistics
import uuid
from collections import Counter, defaultdict
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.appraisal import (
    Appraisal,
    AppraisalCycle,
    AppraisalStage,
    AppraisalStageEntry,
    AppraisalStatus,
    CycleStatus,
    StageRole,
)
from app.models.employee import Employee
from app.models.goals import Goal, GoalStatus
from app.models.org import Department
from app.models.user import User

__all__ = ["performance_overview"]

#: Cycles shown in the trend, most recent last.
MAX_CYCLES = 6
#: A rater needs this many submitted ratings before their spread is shown.
MIN_RATINGS_PER_RATER = 5
#: A rater is flagged when their mean is this many standard deviations from everyone's.
SPREAD_FLAG_SD = Decimal("0.5")

DONE = frozenset(
    {AppraisalStatus.COMPLETED, AppraisalStatus.RELEASED, AppraisalStatus.ACKNOWLEDGED}
)


def _one_dp(value: Decimal | float) -> str:
    return str(Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def _two_dp(value: Decimal | float) -> str:
    return str(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


async def _visible(session: AsyncSession, user: User) -> set[uuid.UUID] | None:
    """None means everybody (HR); otherwise the ids the caller may see."""
    return await permissions.visible_employee_ids(session, user)


async def _cycles(session: AsyncSession, user: User) -> list[AppraisalCycle]:
    rows = (
        await session.scalars(
            tenant_scoped(
                select(AppraisalCycle)
                .where(AppraisalCycle.status != CycleStatus.DRAFT)
                .order_by(AppraisalCycle.period_end.desc())
                .limit(MAX_CYCLES),
                AppraisalCycle,
                user,
            )
        )
    ).all()
    return list(reversed(rows))


async def _rating_trend(
    session: AsyncSession, user: User, cycles: list[AppraisalCycle], visible: set | None
) -> dict[str, Any]:
    """Per cycle: completion, the spread of final ratings, and the mean final score."""
    if not cycles:
        return {"ratings": [], "cycles": []}
    stmt = select(Appraisal).where(Appraisal.cycle_id.in_([c.id for c in cycles]))
    if visible is not None:
        stmt = stmt.where(Appraisal.employee_id.in_(visible or {uuid.uuid4()}))
    appraisals = (await session.scalars(tenant_scoped(stmt, Appraisal, user))).all()

    # Order rating labels by the scores that carried them, best first; a label
    # is only text, and "A" above "B" is a guess this avoids.
    label_scores: dict[str, list[Decimal]] = defaultdict(list)
    for a in appraisals:
        if a.final_rating and a.final_score is not None:
            label_scores[a.final_rating].append(Decimal(str(a.final_score)))
    labels = sorted(
        {a.final_rating for a in appraisals if a.final_rating},
        key=lambda label: (
            -(statistics.fmean(label_scores[label]) if label_scores[label] else 0),
            label,
        ),
    )

    by_cycle: dict[uuid.UUID, list[Appraisal]] = defaultdict(list)
    for a in appraisals:
        by_cycle[a.cycle_id].append(a)
    out = []
    for cycle in cycles:
        rows = by_cycle.get(cycle.id, [])
        done = [a for a in rows if a.status in DONE]
        scores = [Decimal(str(a.final_score)) for a in rows if a.final_score is not None]
        counts = Counter(a.final_rating for a in rows if a.final_rating)
        out.append(
            {
                "cycle_id": str(cycle.id),
                "cycle": cycle.name,
                "period_end": cycle.period_end.isoformat(),
                "status": cycle.status.value,
                "appraisal_count": len(rows),
                "completed": len(done),
                "completion_percent": _one_dp(
                    Decimal(len(done)) / Decimal(len(rows)) * 100 if rows else 0
                ),
                "mean_final_score": _two_dp(statistics.fmean(scores)) if scores else None,
                "ratings": {label: counts.get(label, 0) for label in labels},
            }
        )
    return {"ratings": labels, "cycles": out}


async def _goal_attainment(
    session: AsyncSession, user: User, cycles: list[AppraisalCycle], visible: set | None
) -> dict[str, Any]:
    """Per cycle and per department: goals completed, and average progress.

    Cancelled and draft goals are left out — a goal the business withdrew is
    not one the person failed.
    """
    if not cycles:
        return {"cycles": [], "departments": []}
    stmt = (
        select(Goal, Employee.department_id)
        .join(Employee, Employee.id == Goal.owner_employee_id)
        .where(
            Goal.appraisal_cycle_id.in_([c.id for c in cycles]),
            Goal.status.in_([GoalStatus.ACTIVE, GoalStatus.COMPLETED]),
        )
    )
    if visible is not None:
        stmt = stmt.where(Goal.owner_employee_id.in_(visible or {uuid.uuid4()}))
    rows = (await session.execute(tenant_scoped(stmt, Goal, user))).all()
    names = {
        d.id: d.name
        for d in (
            await session.scalars(tenant_scoped(select(Department), Department, user))
        ).all()
    }

    def summarise(goals: list[Goal]) -> dict[str, Any]:
        completed = sum(1 for g in goals if g.status is GoalStatus.COMPLETED)
        progress = [Decimal(str(g.progress_percent or 0)) for g in goals]
        return {
            "goals": len(goals),
            "completed": completed,
            "completed_percent": _one_dp(
                Decimal(completed) / Decimal(len(goals)) * 100 if goals else 0
            ),
            "average_progress_percent": _one_dp(statistics.fmean(progress)) if progress else "0.0",
        }

    by_cycle: dict[uuid.UUID, list[Goal]] = defaultdict(list)
    by_department: dict[str, list[Goal]] = defaultdict(list)
    latest = cycles[-1].id
    for goal, department_id in rows:
        by_cycle[goal.appraisal_cycle_id].append(goal)
        if goal.appraisal_cycle_id == latest:
            by_department[names.get(department_id, "No department")].append(goal)
    return {
        "cycles": [
            {"cycle_id": str(c.id), "cycle": c.name, **summarise(by_cycle.get(c.id, []))}
            for c in cycles
        ],
        "departments": [
            {"department": name, **summarise(goals)}
            for name, goals in sorted(by_department.items())
        ],
        "departments_cycle": cycles[-1].name,
    }


async def _rater_spread(
    session: AsyncSession, user: User, cycles: list[AppraisalCycle]
) -> dict[str, Any]:
    """Each manager-stage rater's mean score against everyone's, for the latest cycle."""
    rule = (
        f"Shown for raters with {MIN_RATINGS_PER_RATER}+ submitted manager-stage scores in the "
        f"cycle; flagged when their mean is {SPREAD_FLAG_SD}+ standard deviations from the mean "
        "of every manager-stage score in the cycle."
    )
    if not cycles:
        return {"cycle": None, "rule": rule, "raters": []}
    cycle = cycles[-1]
    rows = (
        await session.execute(
            tenant_scoped(
                select(AppraisalStageEntry.actor_user_id, AppraisalStageEntry.score)
                .join(AppraisalStage, AppraisalStage.id == AppraisalStageEntry.stage_id)
                .where(
                    AppraisalStage.cycle_id == cycle.id,
                    AppraisalStage.role == StageRole.MANAGER,
                    AppraisalStageEntry.submitted_at.is_not(None),
                    AppraisalStageEntry.score.is_not(None),
                    AppraisalStageEntry.actor_user_id.is_not(None),
                ),
                AppraisalStageEntry,
                user,
            )
        )
    ).all()
    scores = [Decimal(str(score)) for _, score in rows]
    if len(scores) < 2:
        return {"cycle": cycle.name, "rule": rule, "raters": []}
    overall = Decimal(str(statistics.fmean(scores)))
    spread = Decimal(str(statistics.pstdev(scores)))
    by_rater: dict[uuid.UUID, list[Decimal]] = defaultdict(list)
    for rater, score in rows:
        by_rater[rater].append(Decimal(str(score)))
    names = {
        u.id: u.full_name
        for u in (
            await session.scalars(
                tenant_scoped(select(User).where(User.id.in_(list(by_rater))), User, user)
            )
        ).all()
    }
    raters = []
    for rater, given in by_rater.items():
        if len(given) < MIN_RATINGS_PER_RATER:
            continue
        mean = Decimal(str(statistics.fmean(given)))
        gap = mean - overall
        flagged = spread > 0 and abs(gap) >= SPREAD_FLAG_SD * spread
        raters.append(
            {
                "rater": names.get(rater, "Former user"),
                "ratings": len(given),
                "mean_score": _two_dp(mean),
                "gap": _two_dp(gap),
                "flagged": flagged,
                "direction": ("higher" if gap > 0 else "lower") if flagged else None,
            }
        )
    raters.sort(key=lambda r: -abs(Decimal(r["gap"])))
    return {
        "cycle": cycle.name,
        "rule": rule,
        "overall_mean": _two_dp(overall),
        "standard_deviation": _two_dp(spread),
        "scores": len(scores),
        "raters": raters,
    }


async def performance_overview(session: AsyncSession, user: User) -> dict[str, Any]:
    """Ratings across recent cycles, goal attainment, and (for HR) rater spread."""
    visible = await _visible(session, user)
    cycles = await _cycles(session, user)
    result: dict[str, Any] = {
        "rating_trend": await _rating_trend(session, user, cycles, visible),
        "goal_attainment": await _goal_attainment(session, user, cycles, visible),
        "rater_spread": None,
    }
    if permissions.is_hr(user):
        result["rater_spread"] = await _rater_spread(session, user, cycles)
    return result

