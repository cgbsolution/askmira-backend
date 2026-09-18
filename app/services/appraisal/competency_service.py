"""Rating scales, competencies, competency templates and appraisal templates.

The rules are the ones slice 16 settled, now in the service layer with an audit
row beside every change:

- a scale level carries a description, and a competency carries a behavioural
  indicator per level — without them ratings are noise;
- template weights sum to 100 and score bands are contiguous, checked at save;
- an appraisal template changes only while its cycle is a draft, because
  changing how scores are composed after managers have started rating changes
  the meaning of ratings already given.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.appraisal import AppraisalCycle, CycleStatus
from app.models.audit import AuditAction
from app.models.competency import (
    AppraisalTemplate,
    Competency,
    CompetencyCategory,
    CompetencyLevelIndicator,
    CompetencyTemplate,
    CompetencyTemplateItem,
    CompetencyValueLink,
    RatingScale,
    RatingScaleLevel,
)
from app.models.employee import Employee
from app.models.user import User
from app.schemas.performance import (
    AppraisalTemplateCreate,
    CompetencyCreate,
    CompetencyTemplateCreate,
    CompetencyUpdate,
    ScaleCreate,
)
from app.services import audit
from app.services.appraisal import review, scoring

__all__ = [
    "create_appraisal_template",
    "create_competency",
    "create_competency_template",
    "create_scale",
    "employee_evidence",
    "get_appraisal_template",
    "list_competencies",
    "list_competency_templates",
    "list_scales",
    "update_competency",
]


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


# ── Scales ──────────────────────────────────────────────────────────────────


async def create_scale(
    session: AsyncSession, user: User, payload: ScaleCreate, *, request: Request | None = None
) -> dict[str, Any]:
    permissions.require_hr(user, detail="Rating scales are configured by HR.")
    duplicate = await session.scalar(
        tenant_scoped(
            select(RatingScale.id).where(RatingScale.name == payload.name), RatingScale, user
        )
    )
    if duplicate is not None:
        raise _conflict(f"A scale called '{payload.name}' already exists.")
    scale = RatingScale(
        tenant_id=user.tenant_id,
        name=payload.name,
        scale_type=payload.scale_type,
        min_value=payload.min_value,
        max_value=payload.max_value,
        allow_decimals=payload.allow_decimals,
    )
    session.add(scale)
    await session.flush()
    for level in payload.levels:
        session.add(
            RatingScaleLevel(tenant_id=user.tenant_id, scale_id=scale.id, **level.model_dump())
        )
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="rating_scale",
        entity_id=scale.id,
        summary=f"Created rating scale '{payload.name}' with {len(payload.levels)} level(s)",
        changes=payload.model_dump(mode="json"),
        request=request,
    )
    return (await list_scales(session, user, scale_id=scale.id))[0]


async def list_scales(
    session: AsyncSession, user: User, *, scale_id: uuid.UUID | None = None
) -> list[dict[str, Any]]:
    stmt = tenant_scoped(select(RatingScale), RatingScale, user).order_by(RatingScale.name)
    if scale_id is not None:
        stmt = stmt.where(RatingScale.id == scale_id)
    scales = list((await session.scalars(stmt)).all())
    levels: dict[uuid.UUID, list[RatingScaleLevel]] = {}
    if scales:
        for level in (
            await session.scalars(
                tenant_scoped(
                    select(RatingScaleLevel)
                    .where(RatingScaleLevel.scale_id.in_([s.id for s in scales]))
                    .order_by(RatingScaleLevel.value),
                    RatingScaleLevel,
                    user,
                )
            )
        ).all():
            levels.setdefault(level.scale_id, []).append(level)
    return [
        {
            "id": s.id,
            "name": s.name,
            "scale_type": s.scale_type,
            "min_value": s.min_value,
            "max_value": s.max_value,
            "allow_decimals": s.allow_decimals,
            "active": s.active,
            "levels": [
                {
                    "value": lv.value,
                    "label": lv.label,
                    "description": lv.description,
                    "colour_hint": lv.colour_hint,
                    "is_default": lv.is_default,
                }
                for lv in levels.get(s.id, [])
            ],
        }
        for s in scales
    ]


# ── Competencies ────────────────────────────────────────────────────────────


async def create_competency(
    session: AsyncSession, user: User, payload: CompetencyCreate, *, request: Request | None = None
) -> dict[str, Any]:
    """A competency and what each of its levels looks like."""
    permissions.require_hr(user, detail="Competencies are configured by HR.")
    scale = await session.scalar(
        tenant_scoped(
            select(RatingScale).where(RatingScale.id == payload.scale_id), RatingScale, user
        )
    )
    if scale is None:
        raise _not_found("Rating scale not found")
    duplicate = await session.scalar(
        tenant_scoped(
            select(Competency.id).where(Competency.code == payload.code), Competency, user
        )
    )
    if duplicate is not None:
        raise _conflict(f"A competency with code '{payload.code}' already exists.")

    low, high = Decimal(str(scale.min_value)), Decimal(str(scale.max_value))
    outside = [str(i.scale_value) for i in payload.indicators if not (low <= i.scale_value <= high)]
    if outside:
        raise _unprocessable(
            f"These indicator values fall outside the '{scale.name}' scale: {', '.join(outside)}."
        )
    values = [i.scale_value for i in payload.indicators]
    if len(set(values)) != len(values):
        raise _unprocessable("Two indicators describe the same scale value.")

    competency = Competency(
        tenant_id=user.tenant_id,
        code=payload.code,
        name=payload.name,
        category=payload.category,
        scale_id=payload.scale_id,
        description=payload.description,
    )
    session.add(competency)
    await session.flush()
    for indicator in payload.indicators:
        session.add(
            CompetencyLevelIndicator(
                tenant_id=user.tenant_id, competency_id=competency.id, **indicator.model_dump()
            )
        )
    for value_code in dict.fromkeys(payload.linked_value_codes):
        session.add(
            CompetencyValueLink(
                tenant_id=user.tenant_id, competency_id=competency.id, value_code=value_code
            )
        )
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="competency",
        entity_id=competency.id,
        summary=f"Created competency '{payload.name}' ({payload.code})",
        changes=payload.model_dump(mode="json"),
        request=request,
    )
    return (await list_competencies(session, user, competency_id=competency.id))[0]


async def update_competency(
    session: AsyncSession,
    user: User,
    competency_id: uuid.UUID,
    payload: CompetencyUpdate,
    *,
    request: Request | None = None,
) -> dict[str, Any]:
    permissions.require_hr(user, detail="Competencies are configured by HR.")
    competency = await session.scalar(
        tenant_scoped(select(Competency).where(Competency.id == competency_id), Competency, user)
    )
    if competency is None:
        raise _not_found("Competency not found")
    changes: dict[str, Any] = {}
    if payload.description is not None:
        changes["description"] = payload.description
        competency.description = payload.description
    if payload.active is not None:
        changes["active"] = payload.active
        competency.active = payload.active
    if payload.linked_value_codes is not None:
        existing = (
            await session.scalars(
                tenant_scoped(
                    select(CompetencyValueLink).where(
                        CompetencyValueLink.competency_id == competency.id
                    ),
                    CompetencyValueLink,
                    user,
                )
            )
        ).all()
        for link in existing:
            await session.delete(link)
        await session.flush()
        for code in dict.fromkeys(payload.linked_value_codes):
            session.add(
                CompetencyValueLink(
                    tenant_id=user.tenant_id, competency_id=competency.id, value_code=code
                )
            )
        changes["linked_value_codes"] = payload.linked_value_codes
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="competency",
        entity_id=competency.id,
        summary=f"Updated competency '{competency.name}'",
        changes=changes,
        request=request,
    )
    return (await list_competencies(session, user, competency_id=competency.id))[0]


async def list_competencies(
    session: AsyncSession,
    user: User,
    *,
    category: CompetencyCategory | None = None,
    competency_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    stmt = tenant_scoped(select(Competency), Competency, user).order_by(Competency.code)
    if category is not None:
        stmt = stmt.where(Competency.category == category)
    if competency_id is not None:
        stmt = stmt.where(Competency.id == competency_id)
    rows = list((await session.scalars(stmt)).all())
    ids = [c.id for c in rows]
    indicators: dict[uuid.UUID, list[CompetencyLevelIndicator]] = {}
    links: dict[uuid.UUID, list[str]] = {}
    if ids:
        for row in (
            await session.scalars(
                tenant_scoped(
                    select(CompetencyLevelIndicator)
                    .where(CompetencyLevelIndicator.competency_id.in_(ids))
                    .order_by(CompetencyLevelIndicator.scale_value),
                    CompetencyLevelIndicator,
                    user,
                )
            )
        ).all():
            indicators.setdefault(row.competency_id, []).append(row)
        for row in (
            await session.scalars(
                tenant_scoped(
                    select(CompetencyValueLink).where(CompetencyValueLink.competency_id.in_(ids)),
                    CompetencyValueLink,
                    user,
                )
            )
        ).all():
            links.setdefault(row.competency_id, []).append(row.value_code)
    return [
        {
            "id": c.id,
            "code": c.code,
            "name": c.name,
            "category": c.category,
            "scale_id": c.scale_id,
            "description": c.description,
            "active": c.active,
            "indicators": [
                {"scale_value": i.scale_value, "behaviour": i.behaviour_description}
                for i in indicators.get(c.id, [])
            ],
            "linked_value_codes": sorted(links.get(c.id, [])),
        }
        for c in rows
    ]


# ── Competency templates ────────────────────────────────────────────────────


async def create_competency_template(
    session: AsyncSession,
    user: User,
    payload: CompetencyTemplateCreate,
    *,
    request: Request | None = None,
) -> dict[str, Any]:
    permissions.require_hr(user, detail="Competency templates are configured by HR.")
    duplicate = await session.scalar(
        tenant_scoped(
            select(CompetencyTemplate.id).where(CompetencyTemplate.name == payload.name),
            CompetencyTemplate,
            user,
        )
    )
    if duplicate is not None:
        raise _conflict(f"A template called '{payload.name}' already exists.")
    competency_ids = [item.competency_id for item in payload.items]
    if len(set(competency_ids)) != len(competency_ids):
        raise _unprocessable("A competency appears on the template twice.")
    found = set(
        (
            await session.scalars(
                tenant_scoped(
                    select(Competency.id).where(Competency.id.in_(competency_ids)), Competency, user
                )
            )
        ).all()
    )
    if found != set(competency_ids):
        raise _not_found("A competency on this template was not found.")
    total = sum((item.weight for item in payload.items), Decimal(0))
    if total != Decimal(100):
        raise _unprocessable(
            f"The competency weights sum to {total}, not 100. Checked now rather than when a "
            "manager tries to submit a rating against the template."
        )

    template = CompetencyTemplate(
        tenant_id=user.tenant_id,
        name=payload.name,
        description=payload.description,
        applicable_roles=payload.applicable_roles,
        applicable_grades=payload.applicable_grades,
    )
    session.add(template)
    await session.flush()
    for item in payload.items:
        session.add(
            CompetencyTemplateItem(
                tenant_id=user.tenant_id, template_id=template.id, **item.model_dump()
            )
        )
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="competency_template",
        entity_id=template.id,
        summary=f"Created competency template '{payload.name}' with {len(payload.items)} item(s)",
        changes=payload.model_dump(mode="json"),
        request=request,
    )
    return (await list_competency_templates(session, user, template_id=template.id))[0]


async def list_competency_templates(
    session: AsyncSession, user: User, *, template_id: uuid.UUID | None = None
) -> list[dict[str, Any]]:
    stmt = tenant_scoped(select(CompetencyTemplate), CompetencyTemplate, user).order_by(
        CompetencyTemplate.name
    )
    if template_id is not None:
        stmt = stmt.where(CompetencyTemplate.id == template_id)
    templates = list((await session.scalars(stmt)).all())
    items: dict[uuid.UUID, list[tuple[CompetencyTemplateItem, Competency]]] = {}
    if templates:
        for item, competency in (
            await session.execute(
                tenant_scoped(
                    select(CompetencyTemplateItem, Competency)
                    .join(Competency, Competency.id == CompetencyTemplateItem.competency_id)
                    .where(CompetencyTemplateItem.template_id.in_([t.id for t in templates]))
                    .order_by(CompetencyTemplateItem.display_order),
                    CompetencyTemplateItem,
                    user,
                )
            )
        ).all():
            items.setdefault(item.template_id, []).append((item, competency))
    return [
        {
            "id": t.id,
            "name": t.name,
            "description": t.description,
            "applicable_roles": t.applicable_roles or [],
            "applicable_grades": t.applicable_grades or [],
            "active": t.active,
            "items": [
                {
                    "competency_id": competency.id,
                    "competency_code": competency.code,
                    "competency_name": competency.name,
                    "weight": item.weight,
                    "is_mandatory": item.is_mandatory,
                    "display_order": item.display_order,
                }
                for item, competency in items.get(t.id, [])
            ],
            "total_weight": sum(
                (Decimal(str(item.weight)) for item, _ in items.get(t.id, [])), Decimal(0)
            ),
        }
        for t in templates
    ]


# ── Appraisal templates ─────────────────────────────────────────────────────


async def create_appraisal_template(
    session: AsyncSession,
    user: User,
    payload: AppraisalTemplateCreate,
    *,
    request: Request | None = None,
) -> dict[str, Any]:
    """The weighting and the score bands, validated now, on a draft cycle."""
    permissions.require_hr(user, detail="Appraisal templates are configured by HR.")
    split = scoring.WeightSplit(
        goal_weight=payload.goal_weight,
        competency_weight=payload.competency_weight,
        values_weight=payload.values_weight,
    )
    bands = [
        scoring.Band(label=b.label, lower=b.lower, upper=b.upper, description=b.description)
        for b in payload.bands
    ]
    try:
        scoring.validate_weights(split)
        scoring.validate_bands(bands)
    except scoring.ScoringError as exc:
        raise _unprocessable(str(exc)) from exc

    cycle = await session.scalar(
        tenant_scoped(
            select(AppraisalCycle).where(AppraisalCycle.id == payload.cycle_id),
            AppraisalCycle,
            user,
        )
    )
    if cycle is None:
        raise _not_found("Cycle not found")
    if cycle.status is not CycleStatus.DRAFT:
        raise _conflict(
            "This cycle has launched. Changing how scores are composed now would change the "
            "meaning of ratings already given."
        )
    scale = await session.scalar(
        tenant_scoped(
            select(RatingScale.id).where(RatingScale.id == payload.rating_scale_id),
            RatingScale,
            user,
        )
    )
    if scale is None:
        raise _not_found("Rating scale not found")
    if payload.competency_weight > 0 and payload.competency_template_id is None:
        raise _unprocessable(
            "Competencies carry weight on this template, so it needs a competency template."
        )
    if payload.competency_template_id is not None:
        found = await session.scalar(
            tenant_scoped(
                select(CompetencyTemplate.id).where(
                    CompetencyTemplate.id == payload.competency_template_id
                ),
                CompetencyTemplate,
                user,
            )
        )
        if found is None:
            raise _not_found("Competency template not found")

    existing = await session.scalar(
        tenant_scoped(
            select(AppraisalTemplate).where(AppraisalTemplate.cycle_id == payload.cycle_id),
            AppraisalTemplate,
            user,
        )
    )
    template = existing or AppraisalTemplate(tenant_id=user.tenant_id, cycle_id=payload.cycle_id)
    template.rating_scale_id = payload.rating_scale_id
    template.competency_template_id = payload.competency_template_id
    template.goal_weight = payload.goal_weight
    template.competency_weight = payload.competency_weight
    template.values_weight = payload.values_weight
    template.score_precision = payload.score_precision
    template.score_bands = [band.model_dump(mode="json") for band in payload.bands]
    if existing is None:
        session.add(template)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE if existing else AuditAction.CREATE,
        entity_type="appraisal_template",
        entity_id=template.id,
        summary=(
            f"Set the scoring for '{cycle.name}': goals {payload.goal_weight}, competencies "
            f"{payload.competency_weight}, values {payload.values_weight}"
        ),
        changes=payload.model_dump(mode="json"),
        request=request,
    )
    return await get_appraisal_template(session, user, payload.cycle_id) or {}


async def get_appraisal_template(
    session: AsyncSession, user: User, cycle_id: uuid.UUID
) -> dict[str, Any] | None:
    template = await session.scalar(
        tenant_scoped(
            select(AppraisalTemplate).where(AppraisalTemplate.cycle_id == cycle_id),
            AppraisalTemplate,
            user,
        )
    )
    if template is None:
        return None
    scale_name = await session.scalar(
        tenant_scoped(
            select(RatingScale.name).where(RatingScale.id == template.rating_scale_id),
            RatingScale,
            user,
        )
    )
    competency_template_name = None
    if template.competency_template_id:
        competency_template_name = await session.scalar(
            tenant_scoped(
                select(CompetencyTemplate.name).where(
                    CompetencyTemplate.id == template.competency_template_id
                ),
                CompetencyTemplate,
                user,
            )
        )
    return {
        "id": template.id,
        "cycle_id": template.cycle_id,
        "rating_scale_id": template.rating_scale_id,
        "rating_scale_name": scale_name,
        "competency_template_id": template.competency_template_id,
        "competency_template_name": competency_template_name,
        "goal_weight": template.goal_weight,
        "competency_weight": template.competency_weight,
        "values_weight": template.values_weight,
        "score_precision": template.score_precision,
        "bands": template.score_bands or [],
    }


# ── Evidence ────────────────────────────────────────────────────────────────


async def employee_evidence(
    session: AsyncSession, user: User, *, employee_id: uuid.UUID, cycle_id: uuid.UUID
) -> list[dict[str, Any]]:
    """Recognition this employee received, against each active competency's values.

    Evidence a manager reads, not a score. For the person themselves, their
    reporting line and HR.
    """
    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
    )
    if employee is None:
        raise _not_found("Employee not found")
    await permissions.require_manager_or_hr(
        session,
        user,
        employee_id,
        detail="Recognition evidence is visible to the person, their line and HR.",
        allow_self=True,
    )
    cycle = await session.scalar(
        tenant_scoped(
            select(AppraisalCycle).where(AppraisalCycle.id == cycle_id), AppraisalCycle, user
        )
    )
    if cycle is None:
        raise _not_found("Cycle not found")
    competencies = list(
        (
            await session.scalars(
                tenant_scoped(
                    select(Competency).where(Competency.active.is_(True)), Competency, user
                )
            )
        ).all()
    )

    class _Subject:
        employee_id = employee.id

    evidence = await review.recognition_evidence(
        session,
        user,
        appraisal=_Subject,  # type: ignore[arg-type]
        cycle=cycle,
        competencies=competencies,
        viewer_is_subject=employee.id == user.employee_id,
    )
    return [evidence["by_competency"][str(c.id)] for c in competencies]
