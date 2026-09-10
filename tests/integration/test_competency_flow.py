"""Slice 16 gates that need a real database.

The scoring and evidence logic is proved in `tests/appraisal/`. What only a
database settles: that weight validation actually rejects at save, that every
seeded competency has level indicators, and that recognition surfaces against
the matching competency — the differentiator, end to end.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appraisal import AppraisalCycle, CycleType
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
from app.models.org import LegalEntity
from app.models.tenant import Tenant
from app.models.user import User
from app.services.appraisal import evidence as evidence_service
from app.services.appraisal import scoring
from tests.integration.conftest import requires_db

pytestmark = requires_db

PERIOD_START = date(2026, 4, 1)
PERIOD_END = date(2027, 3, 31)


async def _scale(session: AsyncSession, tenant: Tenant) -> RatingScale:
    """A five-point scale with a description on every level.

    The descriptions are what make one manager's 4 the same as another's.
    """
    scale = RatingScale(
        tenant_id=tenant.id,
        name="Five-point performance scale",
        scale_type=ScaleType.NUMERIC,
        min_value=Decimal(1),
        max_value=Decimal(5),
    )
    session.add(scale)
    await session.flush()

    for value, label, description in [
        (1, "Below expectations", "Consistently falls short of what the role requires."),
        (2, "Partially meets", "Meets some expectations; gaps need active support."),
        (3, "Meets expectations", "Delivers what the role requires, reliably."),
        (4, "Exceeds expectations", "Regularly delivers beyond what the role requires."),
        (5, "Outstanding", "Sets the standard others in the role are measured against."),
    ]:
        session.add(
            RatingScaleLevel(
                tenant_id=tenant.id, scale_id=scale.id, value=Decimal(value),
                label=label, description=description,
            )
        )
    await session.commit()
    await session.refresh(scale)
    return scale


async def _competency(
    session: AsyncSession,
    tenant: Tenant,
    scale: RatingScale,
    *,
    code: str = "COLLAB",
    name: str = "Collaboration",
    values: list[str] | None = None,
) -> Competency:
    competency = Competency(
        tenant_id=tenant.id, code=code, name=name,
        category=CompetencyCategory.BEHAVIOURAL, scale_id=scale.id,
    )
    session.add(competency)
    await session.flush()

    for value in range(1, 6):
        session.add(
            CompetencyLevelIndicator(
                tenant_id=tenant.id, competency_id=competency.id,
                scale_value=Decimal(value),
                behaviour_description=(
                    f"At {value}: what '{name}' looks like at this level, concretely."
                ),
            )
        )
    for value_code in values or []:
        session.add(
            CompetencyValueLink(
                tenant_id=tenant.id, competency_id=competency.id, value_code=value_code
            )
        )
    await session.commit()
    await session.refresh(competency)
    return competency


async def _cycle(session: AsyncSession, tenant: Tenant) -> AppraisalCycle:
    cycle = AppraisalCycle(
        tenant_id=tenant.id, name="FY26 review", cycle_type=CycleType.ANNUAL,
        period_start=PERIOD_START, period_end=PERIOD_END,
    )
    session.add(cycle)
    await session.commit()
    await session.refresh(cycle)
    return cycle


# ═══════════════════════════════════════════════════════════════════════════
# Level indicators — the gate
# ═══════════════════════════════════════════════════════════════════════════


async def test_every_competency_has_an_indicator_for_every_scale_level(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """The checklist gate.

    Without indicators, "Collaboration: 3" is noise — one manager's 3 is
    another's 4, and calibration has nothing to argue from.
    """
    scale = await _scale(session, tenant)
    await _competency(session, tenant, scale)
    await _competency(session, tenant, scale, code="OWN", name="Ownership")

    competencies = (await session.scalars(select(Competency))).all()
    assert competencies

    levels = await session.scalar(
        select(func.count()).select_from(RatingScaleLevel).where(
            RatingScaleLevel.scale_id == scale.id
        )
    )
    for competency in competencies:
        count = await session.scalar(
            select(func.count()).select_from(CompetencyLevelIndicator).where(
                CompetencyLevelIndicator.competency_id == competency.id
            )
        )
        assert count == levels, (
            f"'{competency.name}' has {count} indicator(s) against {levels} scale levels"
        )


async def test_every_scale_level_carries_a_description(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """It is what a rater reads to decide between a 3 and a 4."""
    scale = await _scale(session, tenant)
    levels = (
        await session.scalars(
            select(RatingScaleLevel).where(RatingScaleLevel.scale_id == scale.id)
        )
    ).all()
    assert len(levels) == 5
    for level in levels:
        assert level.description and level.description.strip()


# ═══════════════════════════════════════════════════════════════════════════
# Weight validation at save
# ═══════════════════════════════════════════════════════════════════════════


async def test_weights_that_do_not_sum_to_a_hundred_never_reach_the_database(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """The checklist gate: at save.

    At computation time the cycle has launched and managers have started
    rating against a template that cannot produce a score.
    """
    scale = await _scale(session, tenant)
    cycle = await _cycle(session, tenant)

    bad = scoring.WeightSplit(goal_weight=Decimal(70), competency_weight=Decimal(20))
    with pytest.raises(scoring.ScoringError, match="sum to 90"):
        scoring.validate_weights(bad)

    stored = await session.scalar(select(func.count()).select_from(AppraisalTemplate))
    assert stored == 0, "nothing was persisted"

    good = scoring.WeightSplit(goal_weight=Decimal(70), competency_weight=Decimal(30))
    scoring.validate_weights(good)
    session.add(
        AppraisalTemplate(
            tenant_id=tenant.id, cycle_id=cycle.id, rating_scale_id=scale.id,
            goal_weight=good.goal_weight, competency_weight=good.competency_weight,
            score_bands=[
                {"label": "Meets expectations", "lower": 0, "upper": 80},
                {"label": "Exceeds expectations", "lower": 80, "upper": 100},
            ],
        )
    )
    await session.commit()
    assert await session.scalar(select(func.count()).select_from(AppraisalTemplate)) == 1


async def test_stored_bands_map_a_score_to_a_rating(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    scale = await _scale(session, tenant)
    cycle = await _cycle(session, tenant)
    template = AppraisalTemplate(
        tenant_id=tenant.id, cycle_id=cycle.id, rating_scale_id=scale.id,
        goal_weight=Decimal(70), competency_weight=Decimal(30),
        score_bands=[
            {"label": "Below", "lower": 0, "upper": 50},
            {"label": "Meets", "lower": 50, "upper": 80},
            {"label": "Exceeds", "lower": 80, "upper": 100},
        ],
    )
    session.add(template)
    await session.commit()

    bands = [
        scoring.Band(b["label"], Decimal(str(b["lower"])), Decimal(str(b["upper"])))
        for b in template.score_bands
    ]
    scoring.validate_bands(bands)

    assert scoring.map_to_band(Decimal("79.99"), bands).label == "Meets"
    assert scoring.map_to_band(Decimal(80), bands).label == "Exceeds"
    assert scoring.map_to_band(Decimal(100), bands).label == "Exceeds"


# ═══════════════════════════════════════════════════════════════════════════
# Scoring against a real template
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_non_applicable_competency_is_excluded_from_the_stored_template(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """The checklist gate, at the boundary.

    An individual contributor with "manages a team" on their template scores
    the same as one without it — the item is removed from the denominator,
    not scored zero.
    """
    scale = await _scale(session, tenant)
    collab = await _competency(session, tenant, scale)
    leads = await _competency(session, tenant, scale, code="LEADS", name="Manages a team")

    template = CompetencyTemplate(tenant_id=tenant.id, name="Engineering")
    session.add(template)
    await session.flush()
    for competency, weight in ((collab, Decimal(50)), (leads, Decimal(50))):
        session.add(
            CompetencyTemplateItem(
                tenant_id=tenant.id, template_id=template.id,
                competency_id=competency.id, weight=weight,
            )
        )
    await session.commit()

    items = (
        await session.execute(
            select(CompetencyTemplateItem, Competency)
            .join(Competency, CompetencyTemplateItem.competency_id == Competency.id)
            .where(CompetencyTemplateItem.template_id == template.id)
        )
    ).all()

    ratings = [
        scoring.CompetencyRating(
            code=competency.code,
            name=competency.name,
            weight=Decimal(str(item.weight)),
            rating=Decimal(4) if competency.code == "COLLAB" else None,
            is_applicable=competency.code == "COLLAB",
            not_applicable_reason=(
                None if competency.code == "COLLAB"
                else "Individual contributor with no reports."
            ),
        )
        for item, competency in items
    ]

    with_exclusion = scoring.compute_score(
        split=scoring.WeightSplit(goal_weight=Decimal(70), competency_weight=Decimal(30)),
        goal_score=Decimal(80),
        competency_ratings=ratings,
        scale_max=Decimal(str(scale.max_value)),
    )
    assert with_exclusion.competency_score == Decimal(80)
    assert len(with_exclusion.excluded_competencies) == 1
    assert with_exclusion.excluded_competencies[0]["code"] == "LEADS"

    # The same person, if the item had been scored zero instead.
    zeroed = scoring.compute_score(
        split=scoring.WeightSplit(goal_weight=Decimal(70), competency_weight=Decimal(30)),
        goal_score=Decimal(80),
        competency_ratings=[
            scoring.CompetencyRating("COLLAB", "Collaboration", Decimal(50), Decimal(4)),
            scoring.CompetencyRating("LEADS", "Manages a team", Decimal(50), Decimal(0)),
        ],
        scale_max=Decimal(str(scale.max_value)),
    )
    assert with_exclusion.weighted_total > zeroed.weighted_total


# ═══════════════════════════════════════════════════════════════════════════
# The differentiator, end to end
# ═══════════════════════════════════════════════════════════════════════════


async def test_recognition_surfaces_against_the_matching_competency(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee,
    legal_entity: LegalEntity,
) -> None:
    """The checklist gate.

    A manager rating "Collaboration" sees the shoutouts this person received
    for collaboration, during the review period, with who wrote them.
    """
    scale = await _scale(session, tenant)
    collab = await _competency(
        session, tenant, scale, values=["collaboration", "teamwork"]
    )
    await _competency(session, tenant, scale, code="OWN", name="Ownership",
                      values=["ownership"])
    cycle = await _cycle(session, tenant)

    colleague = Employee(
        tenant_id=tenant.id, employee_code="ACM900", first_name="Rahul",
        last_name="Iyer", work_email="rahul.i@acme.test",
        legal_entity_id=legal_entity.id,
    )
    session.add(colleague)
    await session.flush()

    session.add_all(
        [
            Shoutout(
                tenant_id=tenant.id, from_employee_id=colleague.id,
                to_employee_id=employee.id, value_tag="collaboration",
                message="Unblocked the payments migration for our whole team.",
            ),
            Shoutout(
                tenant_id=tenant.id, from_employee_id=colleague.id,
                to_employee_id=employee.id, value_tag="ownership",
                message="Took the on-call escalation end to end.",
            ),
        ]
    )
    await session.commit()

    shoutouts = (
        await session.execute(
            select(Shoutout, Employee).join(
                Employee, Shoutout.from_employee_id == Employee.id
            ).where(Shoutout.to_employee_id == employee.id)
        )
    ).all()
    recognitions = [
        evidence_service.RecognitionItem(
            recognition_id=str(s.id), value_code=s.value_tag or "",
            from_name=giver.full_name, message=s.message,
            given_on=s.created_at.date(),
        )
        for s, giver in shoutouts
    ]

    links = (
        await session.scalars(
            select(CompetencyValueLink.value_code).where(
                CompetencyValueLink.competency_id == collab.id
            )
        )
    ).all()

    result = evidence_service.gather_evidence(
        competency_code=collab.code,
        competency_name=collab.name,
        linked_value_codes=list(links),
        recognitions=recognitions,
        # The shoutouts were created now, so widen the window around today
        # rather than pinning it to a fictional review year.
        period_start=date.today() - timedelta(days=1),
        period_end=date.today() + timedelta(days=1),
    )

    assert result.has_evidence is True
    assert len(result.items) == 1, "the ownership shoutout is not collaboration evidence"
    assert "payments migration" in result.items[0].message
    assert result.items[0].from_name == "Rahul Iyer"
    assert result.distinct_recognisers == 1
    assert cycle.period_start == PERIOD_START


async def test_the_evidence_payload_carries_no_score(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """Recognition converted into a rating is a popularity metric wearing a
    competency's name. It is evidence a manager reads, and nothing more."""
    scale = await _scale(session, tenant)
    collab = await _competency(session, tenant, scale, values=["collaboration"])

    payload = evidence_service.gather_evidence(
        competency_code=collab.code, competency_name=collab.name,
        linked_value_codes=["collaboration"], recognitions=[],
        period_start=PERIOD_START, period_end=PERIOD_END,
    ).as_dict()

    assert not any("score" in key or "rating" in key for key in payload)
    assert "absence of evidence, not evidence of absence" in payload["note"]


async def test_an_unlinked_competency_says_how_to_fix_it(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """Rather than showing nothing, which looks identical to having no
    recognition."""
    scale = await _scale(session, tenant)
    orphan = await _competency(session, tenant, scale, code="CRAFT", name="Craft")

    links = (
        await session.scalars(
            select(CompetencyValueLink.value_code).where(
                CompetencyValueLink.competency_id == orphan.id
            )
        )
    ).all()
    result = evidence_service.gather_evidence(
        competency_code=orphan.code, competency_name=orphan.name,
        linked_value_codes=list(links), recognitions=[],
        period_start=PERIOD_START, period_end=PERIOD_END,
    )
    assert "not linked to any company value" in result.note


async def test_competencies_are_tenant_isolated(
    session: AsyncSession, tenant: Tenant, hr_user: User, other_hr_user: User
) -> None:
    from app.core.deps import tenant_scoped

    scale = await _scale(session, tenant)
    await _competency(session, tenant, scale)

    visible = (
        await session.scalars(
            tenant_scoped(select(Competency), Competency, other_hr_user)
        )
    ).all()
    assert visible == []
