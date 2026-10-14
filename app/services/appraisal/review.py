"""The review as a person sees it: the form, the live score, their list, their team.

═══════════════════════════════════════════════════════════════════════════════
THE SCORE IS COMPOSED HERE, FROM THINGS SOMEBODY CAN CHECK
═══════════════════════════════════════════════════════════════════════════════

A review score is `goal weight x goal score + competency weight x competency
score + values weight x values score`, per the cycle's appraisal template:

- the **goal score** comes from the goals module — key results measured from a
  start towards a target — and never from a number typed into the review;
- the **competency score** comes from the ratings on this step, weighted by the
  competency template, with non-applicable competencies excluded rather than
  scored zero (slice 16's engine);
- the **values score** is the mean of the value ratings on this step.
  Recognition is shown beside each value as evidence and does not score
  anything on its own.

Every figure returns with what produced it. A score that cannot be computed —
no measured goals where goals carry weight, an unrated competency — returns the
reason instead of a number, because a zero would be an invented rating.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, status
from pydantic import ValidationError
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
from app.models.calibration import CalibrationChange, CalibrationPolicy
from app.models.competency import (
    AppraisalTemplate,
    Competency,
    CompetencyLevelIndicator,
    CompetencyTemplateItem,
    CompetencyValueLink,
    RatingScale,
    RatingScaleLevel,
)
from app.models.culture import Shoutout, ShoutoutVisibility
from app.models.culture_depth import CompanyValue
from app.models.employee import Employee
from app.models.org import Department
from app.models.user import User, UserRole
from app.schemas.performance import ReviewRatings
from app.services.appraisal import access, scoring
from app.services.appraisal import evidence as evidence_engine
from app.services.goals import service as goals_service

__all__ = [
    "TemplateBundle",
    "compute_score",
    "load_bundle",
    "my_reviews",
    "parse_ratings",
    "review_detail",
    "score_preview",
    "team_overview",
    "validate_ratings",
]

ZERO = Decimal(0)
HUNDRED = Decimal(100)
DONE_STATUSES = (AppraisalStatus.COMPLETED, AppraisalStatus.RELEASED, AppraisalStatus.ACKNOWLEDGED)
RELEASED_STATUSES = (AppraisalStatus.RELEASED, AppraisalStatus.ACKNOWLEDGED)


def _d(value: Any) -> Decimal:
    return Decimal(str(value))


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


# ═══════════════════════════════════════════════════════════════════════════
# Loading
# ═══════════════════════════════════════════════════════════════════════════


async def load_appraisal(
    session: AsyncSession, user: User, appraisal_id: uuid.UUID
) -> tuple[Appraisal, AppraisalCycle, list[AppraisalStage], Employee]:
    appraisal = await session.scalar(
        tenant_scoped(select(Appraisal).where(Appraisal.id == appraisal_id), Appraisal, user)
    )
    if appraisal is None:
        raise _not_found("Appraisal not found")
    cycle = await session.scalar(
        tenant_scoped(
            select(AppraisalCycle).where(AppraisalCycle.id == appraisal.cycle_id),
            AppraisalCycle,
            user,
        )
    )
    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == appraisal.employee_id), Employee, user)
    )
    if cycle is None or employee is None:
        raise _not_found("Appraisal not found")
    stages = list(
        (
            await session.scalars(
                tenant_scoped(
                    select(AppraisalStage)
                    .where(AppraisalStage.cycle_id == cycle.id)
                    .order_by(AppraisalStage.sequence),
                    AppraisalStage,
                    user,
                )
            )
        ).all()
    )
    return appraisal, cycle, stages, employee


class TemplateBundle:
    """The cycle's template, its scale and the competencies this person is rated on."""

    def __init__(
        self,
        template: AppraisalTemplate | None,
        scale: RatingScale | None,
        levels: list[RatingScaleLevel],
        items: list[tuple[CompetencyTemplateItem, Competency]],
    ) -> None:
        self.template = template
        self.scale = scale
        self.levels = levels
        self.items = items

    @property
    def scale_max(self) -> Decimal | None:
        return _d(self.scale.max_value) if self.scale is not None else None


async def load_bundle(
    session: AsyncSession, user: User, appraisal: Appraisal, cycle: AppraisalCycle
) -> TemplateBundle:
    template = await session.scalar(
        tenant_scoped(
            select(AppraisalTemplate).where(AppraisalTemplate.cycle_id == cycle.id),
            AppraisalTemplate,
            user,
        )
    )
    if template is None:
        return TemplateBundle(None, None, [], [])
    scale = await session.scalar(
        tenant_scoped(
            select(RatingScale).where(RatingScale.id == template.rating_scale_id),
            RatingScale,
            user,
        )
    )
    levels = list(
        (
            await session.scalars(
                tenant_scoped(
                    select(RatingScaleLevel)
                    .where(RatingScaleLevel.scale_id == template.rating_scale_id)
                    .order_by(RatingScaleLevel.value),
                    RatingScaleLevel,
                    user,
                )
            )
        ).all()
    )
    competency_template_id = appraisal.competency_template_id or template.competency_template_id
    items: list[tuple[CompetencyTemplateItem, Competency]] = []
    if competency_template_id is not None:
        items = [
            (item, competency)
            for item, competency in (
                await session.execute(
                    tenant_scoped(
                        select(CompetencyTemplateItem, Competency)
                        .join(Competency, Competency.id == CompetencyTemplateItem.competency_id)
                        .where(CompetencyTemplateItem.template_id == competency_template_id)
                        .order_by(CompetencyTemplateItem.display_order),
                        CompetencyTemplateItem,
                        user,
                    )
                )
            ).all()
        ]
    return TemplateBundle(template, scale, levels, items)


# ═══════════════════════════════════════════════════════════════════════════
# Ratings
# ═══════════════════════════════════════════════════════════════════════════


def parse_ratings(raw: dict[str, Any] | None) -> ReviewRatings:
    try:
        return ReviewRatings.model_validate(raw or {})
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The ratings are not in the shape this review expects.",
        ) from exc


def is_empty(ratings: ReviewRatings) -> bool:
    return not ratings.competencies and not ratings.values


def validate_ratings(bundle: TemplateBundle, ratings: ReviewRatings) -> list[str]:
    """Problems with the ratings themselves, before any score is computed."""
    problems: list[str] = []
    scale = bundle.scale
    items = {competency.id: (item, competency) for item, competency in bundle.items}

    def _check_value(label: str, value: Decimal | None) -> None:
        if value is None or scale is None:
            return
        low, high = _d(scale.min_value), _d(scale.max_value)
        if not (low <= value <= high):
            problems.append(f"{label}: {value} is outside the scale of {low} to {high}.")
        elif not scale.allow_decimals and value != value.to_integral_value():
            problems.append(f"{label}: this scale takes whole numbers, not {value}.")

    for rating in ratings.competencies:
        match = items.get(rating.competency_id)
        if match is None:
            problems.append("A rating names a competency that is not on this person's template.")
            continue
        item, competency = match
        if not rating.is_applicable and item.is_mandatory:
            problems.append(
                f"'{competency.name}' is mandatory on this template and cannot be marked "
                "not applicable."
            )
        if not rating.is_applicable and not (rating.not_applicable_reason or "").strip():
            problems.append(f"'{competency.name}' is marked not applicable without a reason.")
        _check_value(competency.name, rating.rating)
    for value in ratings.values:
        _check_value(value.value_code, value.rating)
    return problems


async def compute_score(
    session: AsyncSession,
    user: User,
    *,
    appraisal: Appraisal,
    cycle: AppraisalCycle,
    bundle: TemplateBundle,
    ratings: ReviewRatings,
) -> dict[str, Any]:
    """The score these ratings produce, with the goal score and every signal behind it."""
    goal = await goals_service.goal_score_for(
        session, user, employee_id=appraisal.employee_id, cycle=cycle
    )
    result: dict[str, Any] = {
        "score": None,
        "rating": None,
        "breakdown": None,
        "goal": goal.as_dict(),
        "values_score": None,
        "problems": [],
    }
    template = bundle.template
    if template is None or bundle.scale_max is None:
        result["problems"].append(
            "This cycle has no appraisal template, so ratings do not produce a score."
        )
        return result

    problems = validate_ratings(bundle, ratings)
    split = scoring.WeightSplit(
        goal_weight=_d(template.goal_weight),
        competency_weight=_d(template.competency_weight),
        values_weight=_d(template.values_weight),
    )
    if split.goal_weight > ZERO and goal.score is None:
        problems.append(
            f"Goals carry {split.goal_weight} of the weight, but none of this person's goals "
            "has measured progress yet. Record progress on their key results first; the "
            "goal score is not guessed."
        )

    by_competency = {r.competency_id: r for r in ratings.competencies}
    competency_ratings = [
        scoring.CompetencyRating(
            code=competency.code,
            name=competency.name,
            weight=_d(item.weight),
            rating=by_competency[competency.id].rating if competency.id in by_competency else None,
            is_applicable=(
                by_competency[competency.id].is_applicable
                if competency.id in by_competency
                else True
            ),
            not_applicable_reason=(
                by_competency[competency.id].not_applicable_reason
                if competency.id in by_competency
                else None
            ),
        )
        for item, competency in bundle.items
    ]
    if split.competency_weight > ZERO and not bundle.items:
        problems.append(
            "Competencies carry weight on this template but no competency template is "
            "assigned, so there is nothing to rate."
        )

    values_score: Decimal | None = None
    rated_values = [v.rating for v in ratings.values if v.rating is not None]
    if split.values_weight > ZERO:
        if rated_values:
            mean = sum(rated_values, ZERO) / Decimal(len(rated_values))
            values_score = (mean / bundle.scale_max * HUNDRED).quantize(Decimal("0.01"))
            result["values_score"] = str(values_score)
        else:
            problems.append(
                f"Values carry {split.values_weight} of the weight and no value is rated."
            )

    if problems:
        result["problems"] = problems
        return result

    try:
        breakdown = scoring.compute_score(
            split=split,
            goal_score=goal.score if goal.score is not None else ZERO,
            competency_ratings=competency_ratings,
            scale_max=bundle.scale_max,
            values_score=values_score,
            precision=template.score_precision,
        )
    except scoring.ScoringError as exc:
        result["problems"] = [str(exc)]
        return result

    result["score"] = str(breakdown.weighted_total)
    result["breakdown"] = breakdown.as_dict()
    bands = [
        scoring.Band(
            label=b["label"],
            lower=_d(b["lower"]),
            upper=_d(b["upper"]),
            description=b.get("description"),
        )
        for b in (template.score_bands or [])
    ]
    if bands:
        try:
            band = scoring.map_to_band(breakdown.weighted_total, bands)
            result["rating"] = {"label": band.label, "description": band.description}
        except scoring.ScoringError as exc:
            result["problems"] = [str(exc)]
    return result


async def score_preview(
    session: AsyncSession, user: User, *, appraisal_id: uuid.UUID, ratings: dict[str, Any]
) -> dict[str, Any]:
    """The score these ratings would produce, without saving anything."""
    appraisal, cycle, _, _ = await load_appraisal(session, user, appraisal_id)
    if not permissions.is_hr(user):
        # The person may preview their own self-ratings; their line may preview theirs.
        await access.require_view(session, user, appraisal)
    bundle = await load_bundle(session, user, appraisal, cycle)
    return await compute_score(
        session,
        user,
        appraisal=appraisal,
        cycle=cycle,
        bundle=bundle,
        ratings=parse_ratings(ratings),
    )


# ═══════════════════════════════════════════════════════════════════════════
# Evidence
# ═══════════════════════════════════════════════════════════════════════════


async def _recognitions(
    session: AsyncSession, user: User, employee_id: uuid.UUID, *, include_private: bool
) -> list[evidence_engine.RecognitionItem]:
    stmt = tenant_scoped(
        select(Shoutout, Employee)
        .join(Employee, Shoutout.from_employee_id == Employee.id)
        .where(Shoutout.to_employee_id == employee_id),
        Shoutout,
        user,
    )
    if not include_private:
        stmt = stmt.where(Shoutout.visibility != ShoutoutVisibility.PRIVATE)
    return [
        evidence_engine.RecognitionItem(
            recognition_id=str(shoutout.id),
            value_code=shoutout.value_tag or "",
            from_name=giver.full_name,
            message=shoutout.message,
            given_on=shoutout.created_at.date(),
        )
        for shoutout, giver in (await session.execute(stmt)).all()
    ]


async def recognition_evidence(
    session: AsyncSession,
    user: User,
    *,
    appraisal: Appraisal,
    cycle: AppraisalCycle,
    competencies: list[Competency],
    viewer_is_subject: bool,
) -> dict[str, Any]:
    """Recognition beside each competency and each company value, for the review period."""
    recognitions = await _recognitions(
        session, user, appraisal.employee_id, include_private=viewer_is_subject
    )
    links: dict[uuid.UUID, list[str]] = {}
    if competencies:
        for competency_id, value_code in (
            await session.execute(
                tenant_scoped(
                    select(CompetencyValueLink.competency_id, CompetencyValueLink.value_code).where(
                        CompetencyValueLink.competency_id.in_([c.id for c in competencies])
                    ),
                    CompetencyValueLink,
                    user,
                )
            )
        ).all():
            links.setdefault(competency_id, []).append(value_code)

    by_competency = {
        str(c.id): evidence_engine.gather_evidence(
            competency_code=c.code,
            competency_name=c.name,
            linked_value_codes=links.get(c.id, []),
            recognitions=recognitions,
            period_start=cycle.period_start,
            period_end=cycle.period_end,
        ).as_dict()
        for c in competencies
    }

    values = (
        await session.scalars(
            tenant_scoped(
                select(CompanyValue)
                .where(CompanyValue.is_active.is_(True))
                .order_by(CompanyValue.display_order, CompanyValue.name),
                CompanyValue,
                user,
            )
        )
    ).all()
    by_value = []
    for value in values:
        gathered = evidence_engine.gather_evidence(
            competency_code=value.code,
            competency_name=value.name,
            linked_value_codes=[value.code],
            recognitions=recognitions,
            period_start=cycle.period_start,
            period_end=cycle.period_end,
        ).as_dict()
        by_value.append(
            {
                "code": value.code,
                "name": value.name,
                "description": value.description,
                "evidence": gathered,
            }
        )
    return {"by_competency": by_competency, "values": by_value}


# ═══════════════════════════════════════════════════════════════════════════
# The review, as one payload
# ═══════════════════════════════════════════════════════════════════════════


def _stage_state(
    stage: AppraisalStage,
    appraisal: Appraisal,
    stages: list[AppraisalStage],
    submitted_stage_ids: set[uuid.UUID],
) -> str:
    if appraisal.current_stage_id == stage.id:
        return "current"
    if stage.id in submitted_stage_ids:
        return "done"
    current = next((s for s in stages if s.id == appraisal.current_stage_id), None)
    if appraisal.status in DONE_STATUSES or (current and stage.sequence < current.sequence):
        return "skipped"
    return "upcoming"


def _overdue_days(stage: AppraisalStage | None, today: date) -> int:
    if stage is None or stage.due_date is None or stage.due_date >= today:
        return 0
    return (today - stage.due_date).days


async def review_detail(
    session: AsyncSession, user: User, appraisal_id: uuid.UUID
) -> dict[str, Any]:
    """Everything the review screen needs, filtered to what this caller may see."""
    appraisal, cycle, stages, employee = await load_appraisal(session, user, appraisal_id)
    await access.require_view(session, user, appraisal)
    is_subject = appraisal.employee_id == user.employee_id
    is_hr = permissions.is_hr(user)
    today = date.today()
    stage_by_id = {s.id: s for s in stages}
    current = stage_by_id.get(appraisal.current_stage_id) if appraisal.current_stage_id else None

    entries = list(
        (
            await session.scalars(
                tenant_scoped(
                    select(AppraisalStageEntry)
                    .where(AppraisalStageEntry.appraisal_id == appraisal.id)
                    .order_by(AppraisalStageEntry.created_at),
                    AppraisalStageEntry,
                    user,
                )
            )
        ).all()
    )
    submitted = {
        e.stage_id for e in entries if e.submitted_at is not None and e.returned_to_stage_id is None
    }

    # ── Who may do what, right now ─────────────────────────────────────────
    can_act = False
    act_reason = None
    if current is not None and cycle.status is not CycleStatus.CLOSED:
        try:
            actor = await access.stage_actor(session, user, appraisal, current, stages)
            can_act = True
            act_reason = actor.reason
        except HTTPException as exc:
            act_reason = str(exc.detail)
    return_targets = (
        [
            {"id": str(s.id), "label": s.label}
            for s in stages
            if current and s.sequence < current.sequence
        ]
        if can_act and current is not None and current.role is not StageRole.SELF
        else []
    )

    # ── Entries this caller may read ───────────────────────────────────────
    released = appraisal.status in RELEASED_STATUSES
    visible_entries: list[AppraisalStageEntry] = []
    for entry in entries:
        stage = stage_by_id.get(entry.stage_id)
        if stage is None or entry.submitted_at is None:
            continue
        if is_hr and not is_subject:
            visible_entries.append(entry)
        elif is_subject:
            if stage.role is StageRole.SELF or released or stage.visible_to_employee_before_release:
                visible_entries.append(entry)
        else:
            hidden_by_stage = (
                current is not None
                and not current.sees_previous_entries
                and stage.sequence < current.sequence
                and entry.actor_user_id != user.id
            )
            if not hidden_by_stage:
                visible_entries.append(entry)

    actor_ids = {e.actor_user_id for e in visible_entries if e.actor_user_id} | {
        e.delegated_from_user_id for e in visible_entries if e.delegated_from_user_id
    }
    actor_names: dict[uuid.UUID, str] = {}
    if actor_ids:
        actor_names = {
            row.id: row.full_name
            for row in (
                await session.scalars(
                    tenant_scoped(select(User).where(User.id.in_(actor_ids)), User, user)
                )
            ).all()
        }

    draft = next(
        (
            e
            for e in entries
            if e.submitted_at is None
            and e.actor_user_id == user.id
            and current is not None
            and e.stage_id == current.id
        ),
        None,
    )

    # ── The form ───────────────────────────────────────────────────────────
    bundle = await load_bundle(session, user, appraisal, cycle)
    competencies = [competency for _, competency in bundle.items]
    indicators: dict[uuid.UUID, list[dict[str, str]]] = {}
    if competencies:
        for row in (
            await session.scalars(
                tenant_scoped(
                    select(CompetencyLevelIndicator)
                    .where(CompetencyLevelIndicator.competency_id.in_([c.id for c in competencies]))
                    .order_by(CompetencyLevelIndicator.scale_value),
                    CompetencyLevelIndicator,
                    user,
                )
            )
        ).all():
            indicators.setdefault(row.competency_id, []).append(
                {"scale_value": str(row.scale_value), "behaviour": row.behaviour_description}
            )
    evidence = await recognition_evidence(
        session,
        user,
        appraisal=appraisal,
        cycle=cycle,
        competencies=competencies,
        viewer_is_subject=is_subject,
    )
    goal = await goals_service.goal_score_for(
        session, user, employee_id=appraisal.employee_id, cycle=cycle
    )
    from app.services.performance import feedback as feedback_service

    feedback_360 = await feedback_service.feedback_for_review(
        session, user, employee_id=appraisal.employee_id, cycle_id=cycle.id
    )

    # ── The outcome, where the caller may see it ───────────────────────────
    outcome = None
    if not is_subject or released:
        policy = await session.scalar(
            tenant_scoped(
                select(CalibrationPolicy).where(CalibrationPolicy.cycle_id == cycle.id),
                CalibrationPolicy,
                user,
            )
        )
        calibrated = (
            await session.scalar(
                tenant_scoped(
                    select(CalibrationChange.id)
                    .where(CalibrationChange.appraisal_id == appraisal.id)
                    .limit(1),
                    CalibrationChange,
                    user,
                )
            )
            is not None
        )
        disclose = not is_subject or bool(policy and policy.disclose_calibration_to_employee)
        outcome = {
            "final_rating": appraisal.final_rating,
            "final_score": str(appraisal.final_score)
            if appraisal.final_score is not None
            else None,
            "calibrated": calibrated if disclose else None,
        }

    department = None
    manager_name = None
    if employee.department_id:
        department = await session.scalar(
            tenant_scoped(
                select(Department.name).where(Department.id == employee.department_id),
                Department,
                user,
            )
        )
    if employee.manager_id:
        manager = await session.scalar(
            tenant_scoped(
                select(Employee).where(Employee.id == employee.manager_id), Employee, user
            )
        )
        manager_name = manager.full_name if manager else None

    return {
        "feedback_360": feedback_360,
        "appraisal": {
            "id": str(appraisal.id),
            "cycle_id": str(cycle.id),
            "cycle_name": cycle.name,
            "cycle_type": cycle.cycle_type.value,
            "cycle_status": cycle.status.value,
            "period_start": cycle.period_start.isoformat(),
            "period_end": cycle.period_end.isoformat(),
            "employee_id": str(employee.id),
            "employee_name": employee.full_name,
            "employee_code": employee.employee_code,
            "designation": employee.designation,
            "department": department,
            "manager_name": manager_name,
            "status": appraisal.status.value,
            "current_stage_id": str(current.id) if current else None,
            "current_stage_label": current.label if current else None,
            "current_stage_role": current.role.value if current else None,
            "due_date": current.due_date.isoformat() if current and current.due_date else None,
            "days_overdue": _overdue_days(current, today),
            "escalated": appraisal.escalated_at is not None,
            "released_at": appraisal.released_at.isoformat() if appraisal.released_at else None,
            "acknowledged_at": (
                appraisal.acknowledged_at.isoformat() if appraisal.acknowledged_at else None
            ),
            "acknowledgement_type": (
                appraisal.acknowledgement_type.value if appraisal.acknowledgement_type else None
            ),
            "objection": appraisal.objection if (is_subject or is_hr) else None,
            "reopened_count": appraisal.reopened_count,
        },
        "stages": [
            {
                "id": str(s.id),
                "sequence": s.sequence,
                "role": s.role.value,
                "label": s.label,
                "due_date": s.due_date.isoformat() if s.due_date else None,
                "is_mandatory": s.is_mandatory,
                "state": _stage_state(s, appraisal, stages, submitted),
            }
            for s in stages
        ],
        "viewer": {
            "is_subject": is_subject,
            "is_hr": is_hr,
            "can_act": can_act,
            "act_reason": act_reason,
            "return_targets": return_targets,
            "can_release": is_hr
            and not is_subject
            and appraisal.status is AppraisalStatus.COMPLETED,
            "can_acknowledge": is_subject and appraisal.status is AppraisalStatus.RELEASED,
            "can_reopen": (
                user.role in (UserRole.ADMIN, UserRole.SUPER_ADMIN)
                and not is_subject
                and appraisal.status in DONE_STATUSES
            ),
        },
        "template": (
            {
                "goal_weight": str(bundle.template.goal_weight),
                "competency_weight": str(bundle.template.competency_weight),
                "values_weight": str(bundle.template.values_weight),
                "score_precision": bundle.template.score_precision,
                "bands": bundle.template.score_bands or [],
                "scale": {
                    "id": str(bundle.scale.id),
                    "name": bundle.scale.name,
                    "min_value": str(bundle.scale.min_value),
                    "max_value": str(bundle.scale.max_value),
                    "allow_decimals": bundle.scale.allow_decimals,
                    "levels": [
                        {
                            "value": str(level.value),
                            "label": level.label,
                            "description": level.description,
                        }
                        for level in bundle.levels
                    ],
                }
                if bundle.scale
                else None,
            }
            if bundle.template
            else None
        ),
        "competencies": [
            {
                "competency_id": str(competency.id),
                "code": competency.code,
                "name": competency.name,
                "description": competency.description,
                "category": competency.category.value,
                "weight": str(item.weight),
                "is_mandatory": item.is_mandatory,
                "indicators": indicators.get(competency.id, []),
                "evidence": evidence["by_competency"].get(str(competency.id)),
            }
            for item, competency in bundle.items
        ],
        "values": evidence["values"],
        "goals": goal.as_dict(),
        "entries": [
            {
                "id": str(e.id),
                "stage_id": str(e.stage_id),
                "stage_label": stage_by_id[e.stage_id].label,
                "stage_role": stage_by_id[e.stage_id].role.value,
                "actor_name": actor_names.get(e.actor_user_id) if e.actor_user_id else None,
                "delegated_from_name": (
                    actor_names.get(e.delegated_from_user_id) if e.delegated_from_user_id else None
                ),
                "is_escalated": e.is_escalated,
                "ratings": e.ratings or {},
                "comments": e.comments,
                "score": str(e.score) if e.score is not None else None,
                "submitted_at": e.submitted_at.isoformat() if e.submitted_at else None,
                "returned_to_label": (
                    stage_by_id[e.returned_to_stage_id].label
                    if e.returned_to_stage_id in stage_by_id
                    else None
                ),
                "returned_reason": e.returned_reason,
            }
            for e in visible_entries
        ],
        "draft": ({"ratings": draft.ratings or {}, "comments": draft.comments} if draft else None),
        "outcome": outcome,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Lists
# ═══════════════════════════════════════════════════════════════════════════


async def my_reviews(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """The caller's own reviews, newest cycle first."""
    employee_id = permissions.own_employee_id(user)
    rows = (
        await session.execute(
            tenant_scoped(
                select(Appraisal, AppraisalCycle)
                .join(AppraisalCycle, AppraisalCycle.id == Appraisal.cycle_id)
                .where(Appraisal.employee_id == employee_id)
                .order_by(AppraisalCycle.period_start.desc()),
                Appraisal,
                user,
            )
        )
    ).all()
    stage_ids = {a.current_stage_id for a, _ in rows if a.current_stage_id}
    stages: dict[uuid.UUID, AppraisalStage] = {}
    if stage_ids:
        stages = {
            s.id: s
            for s in (
                await session.scalars(
                    tenant_scoped(
                        select(AppraisalStage).where(AppraisalStage.id.in_(stage_ids)),
                        AppraisalStage,
                        user,
                    )
                )
            ).all()
        }
    today = date.today()
    out = []
    for appraisal, cycle in rows:
        stage = stages.get(appraisal.current_stage_id) if appraisal.current_stage_id else None
        released = appraisal.status in RELEASED_STATUSES
        out.append(
            {
                "appraisal_id": str(appraisal.id),
                "cycle_id": str(cycle.id),
                "cycle_name": cycle.name,
                "cycle_type": cycle.cycle_type.value,
                "cycle_status": cycle.status.value,
                "period_start": cycle.period_start.isoformat(),
                "period_end": cycle.period_end.isoformat(),
                "status": appraisal.status.value,
                "stage_label": stage.label if stage else None,
                "stage_role": stage.role.value if stage else None,
                "due_date": stage.due_date.isoformat() if stage and stage.due_date else None,
                "days_overdue": _overdue_days(stage, today),
                "waiting_on_me": bool(stage and stage.role is StageRole.SELF),
                "final_rating": appraisal.final_rating if released else None,
                "acknowledged_at": (
                    appraisal.acknowledged_at.isoformat() if appraisal.acknowledged_at else None
                ),
            }
        )
    return out


async def team_overview(
    session: AsyncSession, user: User, *, cycle_id: uuid.UUID | None = None
) -> list[dict[str, Any]]:
    """Reviews in the caller's reporting line (everybody's for HR), with who holds each."""
    is_hr = permissions.is_hr(user)
    line = await permissions.reporting_line(session, user)

    cycle_stmt = tenant_scoped(select(AppraisalCycle), AppraisalCycle, user)
    if cycle_id is not None:
        cycle_stmt = cycle_stmt.where(AppraisalCycle.id == cycle_id)
    else:
        cycle_stmt = cycle_stmt.where(
            AppraisalCycle.status.in_([CycleStatus.ACTIVE, CycleStatus.IN_CALIBRATION])
        )
    cycles = {c.id: c for c in (await session.scalars(cycle_stmt)).all()}
    if not cycles:
        return []

    delegated_subjects: set[uuid.UUID] = set()
    if not is_hr:
        for cid in cycles:
            for delegator in await access.delegated_subject_filter(session, user, cid):
                if delegator.employee_id:
                    delegated_subjects |= await permissions.reporting_line(session, delegator)

    stmt = tenant_scoped(
        select(Appraisal, Employee)
        .join(Employee, Employee.id == Appraisal.employee_id)
        .where(Appraisal.cycle_id.in_(list(cycles))),
        Appraisal,
        user,
    )
    rows = [
        (a, e)
        for a, e in (await session.execute(stmt)).all()
        if a.employee_id != user.employee_id
        and (
            is_hr
            or a.employee_id in line
            or a.employee_id in delegated_subjects
            or a.escalated_to_user_id == user.id
        )
    ]
    if not rows:
        return []

    stage_rows = (
        await session.scalars(
            tenant_scoped(
                select(AppraisalStage).where(AppraisalStage.cycle_id.in_(list(cycles))),
                AppraisalStage,
                user,
            )
        )
    ).all()
    stages = {s.id: s for s in stage_rows}
    manager_ids = {e.manager_id for _, e in rows if e.manager_id}
    department_ids = {e.department_id for _, e in rows if e.department_id}
    managers = {}
    if manager_ids:
        managers = {
            m.id: m.full_name
            for m in (
                await session.scalars(
                    tenant_scoped(
                        select(Employee).where(Employee.id.in_(manager_ids)), Employee, user
                    )
                )
            ).all()
        }
    departments = {}
    if department_ids:
        departments = {
            d.id: d.name
            for d in (
                await session.scalars(
                    tenant_scoped(
                        select(Department).where(Department.id.in_(department_ids)),
                        Department,
                        user,
                    )
                )
            ).all()
        }

    today = date.today()
    out = []
    for appraisal, employee in rows:
        cycle = cycles[appraisal.cycle_id]
        stage = stages.get(appraisal.current_stage_id) if appraisal.current_stage_id else None
        awaiting_me = False
        if stage is not None and cycle.status is not CycleStatus.CLOSED:
            if stage.role in access.LINE_ROLES:
                awaiting_me = (
                    appraisal.employee_id in line
                    or appraisal.employee_id in delegated_subjects
                    or appraisal.escalated_to_user_id == user.id
                    or (is_hr and employee.manager_id is None)
                )
            elif stage.role in access.HR_STEP_ROLES:
                awaiting_me = is_hr
        days = _overdue_days(stage, today)
        out.append(
            {
                "appraisal_id": str(appraisal.id),
                "cycle_id": str(cycle.id),
                "cycle_name": cycle.name,
                "employee_id": str(employee.id),
                "employee_name": employee.full_name,
                "employee_code": employee.employee_code,
                "department": departments.get(employee.department_id),
                "manager_name": managers.get(employee.manager_id),
                "status": appraisal.status.value,
                "stage_label": stage.label if stage else None,
                "stage_role": stage.role.value if stage else None,
                "due_date": stage.due_date.isoformat() if stage and stage.due_date else None,
                "days_overdue": days,
                "escalation_due": bool(
                    stage and stage.due_date and days > cycle.escalation_after_days
                ),
                "escalated": appraisal.escalated_at is not None,
                "awaiting_me": awaiting_me,
                "final_rating": appraisal.final_rating,
                "final_score": (
                    str(appraisal.final_score) if appraisal.final_score is not None else None
                ),
            }
        )
    out.sort(key=lambda r: (not r["awaiting_me"], -r["days_overdue"], r["employee_name"]))
    return out
