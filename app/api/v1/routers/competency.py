"""Rating scales, competencies and weighted scoring (slice 16)."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from app.core.deps import CurrentUser, DbSession, require_roles, tenant_scoped
from app.models.appraisal import AppraisalCycle
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
    ScaleType,
)
from app.models.culture import Shoutout
from app.models.employee import Employee
from app.models.user import UserRole
from app.services.appraisal import evidence as evidence_service
from app.services.appraisal import scoring

router = APIRouter(tags=["competency"])

HR_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN, UserRole.HR_OPS)
ADMIN_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN)
RATER_ROLES = (*HR_ROLES, UserRole.MANAGER)


# ── Contracts ───────────────────────────────────────────────────────────────


class ScaleLevelCreate(BaseModel):
    value: Decimal
    label: str = Field(min_length=1, max_length=128)
    # Required, not optional. It is what makes one manager's 4 the same as
    # another's, and what a calibration conversation argues from.
    description: str = Field(min_length=1)
    colour_hint: str | None = Field(default=None, max_length=16)
    is_default: bool = False


class ScaleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    scale_type: ScaleType
    min_value: Decimal
    max_value: Decimal
    allow_decimals: bool = False
    levels: list[ScaleLevelCreate] = Field(min_length=2)

    @model_validator(mode="after")
    def levels_sit_inside_the_scale(self) -> ScaleCreate:
        if self.max_value <= self.min_value:
            raise ValueError("The scale's maximum is not above its minimum.")
        outside = [
            str(level.value)
            for level in self.levels
            if not (self.min_value <= level.value <= self.max_value)
        ]
        if outside:
            raise ValueError(
                f"These levels fall outside the scale's range: {', '.join(outside)}."
            )
        values = [level.value for level in self.levels]
        if len(set(values)) != len(values):
            raise ValueError("Two levels share a value.")
        return self


class ScaleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    scale_type: ScaleType
    min_value: Decimal
    max_value: Decimal
    allow_decimals: bool
    active: bool


class IndicatorCreate(BaseModel):
    scale_value: Decimal
    behaviour_description: str = Field(min_length=1)


class CompetencyCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    category: CompetencyCategory
    scale_id: uuid.UUID
    description: str | None = None
    # Required. Without indicators "Collaboration: 3" is noise — one manager's
    # 3 is another's 4, and calibration has nothing to argue from.
    indicators: list[IndicatorCreate] = Field(min_length=2)
    linked_value_codes: list[str] = Field(default_factory=list)


class CompetencyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    category: CompetencyCategory
    scale_id: uuid.UUID
    description: str | None
    active: bool


class TemplateItemCreate(BaseModel):
    competency_id: uuid.UUID
    weight: Decimal = Field(gt=0)
    is_mandatory: bool = True
    display_order: int = 0


class CompetencyTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    applicable_roles: list[str] = Field(default_factory=list)
    applicable_grades: list[str] = Field(default_factory=list)
    items: list[TemplateItemCreate] = Field(min_length=1)


class BandSpec(BaseModel):
    label: str = Field(min_length=1)
    lower: Decimal
    upper: Decimal
    description: str | None = None


class AppraisalTemplateCreate(BaseModel):
    cycle_id: uuid.UUID
    rating_scale_id: uuid.UUID
    competency_template_id: uuid.UUID | None = None
    goal_weight: Decimal = Field(ge=0, le=100)
    competency_weight: Decimal = Field(ge=0, le=100)
    values_weight: Decimal = Field(default=Decimal(0), ge=0, le=100)
    score_precision: int = Field(default=2, ge=0, le=4)
    bands: list[BandSpec] = Field(min_length=1)


class CompetencyRatingInput(BaseModel):
    code: str
    name: str
    weight: Decimal = Field(ge=0)
    rating: Decimal | None = None
    # False removes it from the calculation entirely. Scoring it zero instead
    # would penalise the employee for a thing they were never expected to do.
    is_applicable: bool = True
    not_applicable_reason: str | None = None


class ScoreRequest(BaseModel):
    goal_score: Decimal = Field(ge=0, le=100)
    competency_ratings: list[CompetencyRatingInput] = Field(default_factory=list)
    scale_max: Decimal = Field(gt=0)
    values_score: Decimal | None = Field(default=None, ge=0, le=100)


# ── Rating scales ───────────────────────────────────────────────────────────


@router.post(
    "/competency/scales",
    response_model=ScaleRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def create_scale(
    payload: ScaleCreate, user: CurrentUser, session: DbSession
) -> ScaleRead:
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
            RatingScaleLevel(
                tenant_id=user.tenant_id, scale_id=scale.id, **level.model_dump()
            )
        )
    await session.commit()
    await session.refresh(scale)
    return ScaleRead.model_validate(scale)


@router.get(
    "/competency/scales",
    response_model=list[ScaleRead],
    dependencies=[Depends(require_roles(*RATER_ROLES))],
)
async def list_scales(user: CurrentUser, session: DbSession) -> list[ScaleRead]:
    rows = (
        await session.scalars(
            tenant_scoped(select(RatingScale).order_by(RatingScale.name), RatingScale, user)
        )
    ).all()
    return [ScaleRead.model_validate(row) for row in rows]


@router.get(
    "/competency/scales/{scale_id}/levels",
    dependencies=[Depends(require_roles(*RATER_ROLES))],
)
async def list_levels(
    scale_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[dict[str, Any]]:
    """Each level with its description.

    The description is what a rater reads to decide between a 3 and a 4, so it
    travels with the value rather than living in a policy document.
    """
    rows = (
        await session.scalars(
            tenant_scoped(
                select(RatingScaleLevel)
                .where(RatingScaleLevel.scale_id == scale_id)
                .order_by(RatingScaleLevel.value),
                RatingScaleLevel,
                user,
            )
        )
    ).all()
    return [
        {
            "value": str(row.value),
            "label": row.label,
            "description": row.description,
            "colour_hint": row.colour_hint,
        }
        for row in rows
    ]


# ── Competencies ────────────────────────────────────────────────────────────


@router.post(
    "/competency/competencies",
    response_model=CompetencyRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def create_competency(
    payload: CompetencyCreate, user: CurrentUser, session: DbSession
) -> CompetencyRead:
    """A competency and what each of its levels looks like.

    Indicators are required rather than optional: without them a rating is
    noise, and calibration in slice 17 has nothing to argue from.
    """
    scale = await session.scalar(
        tenant_scoped(
            select(RatingScale).where(RatingScale.id == payload.scale_id), RatingScale, user
        )
    )
    if scale is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rating scale not found")

    outside = [
        str(indicator.scale_value)
        for indicator in payload.indicators
        if not (
            Decimal(str(scale.min_value))
            <= indicator.scale_value
            <= Decimal(str(scale.max_value))
        )
    ]
    if outside:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"These indicator values fall outside the '{scale.name}' scale: "
                f"{', '.join(outside)}."
            ),
        )

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
                tenant_id=user.tenant_id,
                competency_id=competency.id,
                **indicator.model_dump(),
            )
        )
    for value_code in payload.linked_value_codes:
        session.add(
            CompetencyValueLink(
                tenant_id=user.tenant_id,
                competency_id=competency.id,
                value_code=value_code,
            )
        )

    await session.commit()
    await session.refresh(competency)
    return CompetencyRead.model_validate(competency)


@router.get(
    "/competency/competencies",
    response_model=list[CompetencyRead],
    dependencies=[Depends(require_roles(*RATER_ROLES))],
)
async def list_competencies(
    user: CurrentUser, session: DbSession, category: CompetencyCategory | None = None
) -> list[CompetencyRead]:
    stmt = select(Competency).order_by(Competency.code)
    if category is not None:
        stmt = stmt.where(Competency.category == category)
    rows = (await session.scalars(tenant_scoped(stmt, Competency, user))).all()
    return [CompetencyRead.model_validate(row) for row in rows]


@router.get(
    "/competency/competencies/{competency_id}/indicators",
    dependencies=[Depends(require_roles(*RATER_ROLES))],
)
async def list_indicators(
    competency_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[dict[str, str]]:
    """What each rating looks like for this competency."""
    rows = (
        await session.scalars(
            tenant_scoped(
                select(CompetencyLevelIndicator)
                .where(CompetencyLevelIndicator.competency_id == competency_id)
                .order_by(CompetencyLevelIndicator.scale_value),
                CompetencyLevelIndicator,
                user,
            )
        )
    ).all()
    return [
        {"scale_value": str(row.scale_value), "behaviour": row.behaviour_description}
        for row in rows
    ]


@router.post(
    "/competency/templates",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def create_competency_template(
    payload: CompetencyTemplateCreate, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
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
    await session.commit()
    return {"id": str(template.id), "items": len(payload.items)}


# ── Appraisal templates and scoring ─────────────────────────────────────────


@router.post(
    "/competency/appraisal-templates",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def create_appraisal_template(
    payload: AppraisalTemplateCreate, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    """The weighting and the score bands, validated now.

    At computation time it is too late — the cycle has launched and managers
    have started rating against a template that cannot produce a score.
    """
    split = scoring.WeightSplit(
        goal_weight=payload.goal_weight,
        competency_weight=payload.competency_weight,
        values_weight=payload.values_weight,
    )
    bands = [
        scoring.Band(
            label=band.label, lower=band.lower, upper=band.upper,
            description=band.description,
        )
        for band in payload.bands
    ]
    try:
        scoring.validate_weights(split)
        scoring.validate_bands(bands)
    except scoring.ScoringError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    cycle = await session.scalar(
        tenant_scoped(
            select(AppraisalCycle).where(AppraisalCycle.id == payload.cycle_id),
            AppraisalCycle,
            user,
        )
    )
    if cycle is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cycle not found")

    template = AppraisalTemplate(
        tenant_id=user.tenant_id,
        cycle_id=payload.cycle_id,
        rating_scale_id=payload.rating_scale_id,
        competency_template_id=payload.competency_template_id,
        goal_weight=payload.goal_weight,
        competency_weight=payload.competency_weight,
        values_weight=payload.values_weight,
        score_precision=payload.score_precision,
        score_bands=[band.model_dump(mode="json") for band in payload.bands],
    )
    session.add(template)
    await session.commit()
    return {"id": str(template.id)}


@router.post(
    "/competency/appraisal-templates/{template_id}/score",
    dependencies=[Depends(require_roles(*RATER_ROLES))],
)
async def compute(
    template_id: uuid.UUID,
    payload: ScoreRequest,
    user: CurrentUser,
    session: DbSession,
) -> dict[str, Any]:
    """The score these ratings produce, and the rating band it lands in.

    Computed on demand so a manager sees the score move as they rate, rather
    than discovering it after submitting.
    """
    template = await session.scalar(
        tenant_scoped(
            select(AppraisalTemplate).where(AppraisalTemplate.id == template_id),
            AppraisalTemplate,
            user,
        )
    )
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Appraisal template not found"
        )

    try:
        breakdown = scoring.compute_score(
            split=scoring.WeightSplit(
                goal_weight=Decimal(str(template.goal_weight)),
                competency_weight=Decimal(str(template.competency_weight)),
                values_weight=Decimal(str(template.values_weight)),
            ),
            goal_score=payload.goal_score,
            competency_ratings=[
                scoring.CompetencyRating(**item.model_dump())
                for item in payload.competency_ratings
            ],
            scale_max=payload.scale_max,
            values_score=payload.values_score,
            precision=template.score_precision,
        )
        band = scoring.map_to_band(
            breakdown.weighted_total,
            [
                scoring.Band(
                    label=b["label"], lower=Decimal(str(b["lower"])),
                    upper=Decimal(str(b["upper"])), description=b.get("description"),
                )
                for b in template.score_bands
            ],
        )
    except scoring.ScoringError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    return {
        **breakdown.as_dict(),
        "rating": {"label": band.label, "description": band.description},
    }


# ── Recognition evidence — the differentiator ───────────────────────────────


@router.get(
    "/competency/evidence",
    dependencies=[Depends(require_roles(*RATER_ROLES))],
)
async def recognition_evidence(
    user: CurrentUser,
    session: DbSession,
    employee_id: uuid.UUID,
    cycle_id: uuid.UUID,
) -> list[dict[str, Any]]:
    """Recognition this employee received, against each competency's values.

    Evidence a manager reads, not a score. See
    `app/services/appraisal/evidence.py` for why that line is where it is.
    """
    cycle = await session.scalar(
        tenant_scoped(
            select(AppraisalCycle).where(AppraisalCycle.id == cycle_id),
            AppraisalCycle,
            user,
        )
    )
    if cycle is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cycle not found")

    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
    )
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    shoutouts = (
        await session.execute(
            tenant_scoped(
                select(Shoutout, Employee)
                .join(Employee, Shoutout.from_employee_id == Employee.id)
                .where(Shoutout.to_employee_id == employee_id),
                Shoutout,
                user,
            )
        )
    ).all()
    recognitions = [
        evidence_service.RecognitionItem(
            recognition_id=str(shoutout.id),
            # The Culture module calls it `value_tag`; the competency link
            # calls it a value code. Same thing, named from each side.
            value_code=shoutout.value_tag or "",
            from_name=giver.full_name,
            message=shoutout.message,
            given_on=shoutout.created_at.date(),
        )
        for shoutout, giver in shoutouts
    ]

    competencies = (
        await session.scalars(
            tenant_scoped(
                select(Competency).where(Competency.active.is_(True)), Competency, user
            )
        )
    ).all()

    out: list[dict[str, Any]] = []
    for competency in competencies:
        links = (
            await session.scalars(
                tenant_scoped(
                    select(CompetencyValueLink.value_code).where(
                        CompetencyValueLink.competency_id == competency.id
                    ),
                    CompetencyValueLink,
                    user,
                )
            )
        ).all()
        out.append(
            evidence_service.gather_evidence(
                competency_code=competency.code,
                competency_name=competency.name,
                linked_value_codes=list(links),
                recognitions=recognitions,
                period_start=cycle.period_start,
                period_end=cycle.period_end,
            ).as_dict()
        )
    return out
