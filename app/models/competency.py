"""Rating scales, competencies, and the templates that combine them.

**Level indicators are the point of this schema.** A competency called
"Collaboration" rated 3 out of 5 means nothing on its own: one manager's 3 is
another's 4, and the ratings are not comparable across a department — which
makes calibration (slice 17) impossible and makes the whole framework noise.

`CompetencyLevelIndicator` says what a 3 actually looks like for *this*
competency. That is what makes two managers' ratings mean the same thing, and
it is why the model requires them rather than treating them as documentation.

The values dimension is deliberate too. A manager rating someone on
"collaboration" should see the shoutouts that person received for
collaboration — as **evidence they read**, not as a number that scores itself.
Recognition converted automatically into a rating would be a popularity metric
with a competency's name on it; recognition shown next to the rating is context
the manager weighs. `CompetencyValueLink` connects the two without collapsing
them.
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import (
    Boolean,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class ScaleType(str, enum.Enum):
    NUMERIC = "numeric"
    LABEL = "label"
    PERCENTAGE = "percentage"


class CompetencyCategory(str, enum.Enum):
    BEHAVIOURAL = "behavioural"
    FUNCTIONAL = "functional"
    LEADERSHIP = "leadership"


class RatingScale(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A scale to rate against.

    The common Indian shapes are all representable: a 5-point scale, a 4-point
    one (which forces a choice by removing the middle), and a percentage.
    Which a tenant uses is a real decision about how they want managers to
    behave, so none of them is built in.
    """

    __tablename__ = "rating_scales"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_rating_scale_name"),)

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    scale_type: Mapped[ScaleType] = mapped_column(
        Enum(ScaleType, name="rating_scale_type"), nullable=False
    )
    allow_decimals: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    min_value: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)
    max_value: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    levels: Mapped[list[RatingScaleLevel]] = relationship(
        back_populates="scale", cascade="all, delete-orphan"
    )


class RatingScaleLevel(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One point on the scale, and what it means.

    `description` is not decoration. It is what makes one manager's 4 the same
    as another's, and it is what a calibration conversation argues from.
    """

    __tablename__ = "rating_scale_levels"
    __table_args__ = (
        UniqueConstraint("tenant_id", "scale_id", "value", name="uq_scale_level_value"),
    )

    scale_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("rating_scales.id", ondelete="CASCADE"),
        nullable=False,
    )
    value: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    colour_hint: Mapped[str | None] = mapped_column(String(16), nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    scale: Mapped[RatingScale] = relationship(back_populates="levels")


class Competency(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "competencies"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_competency_code"),)

    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[CompetencyCategory] = mapped_column(
        Enum(CompetencyCategory, name="competency_category"), nullable=False
    )
    scale_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("rating_scales.id", ondelete="RESTRICT"),
        nullable=False,
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    indicators: Mapped[list[CompetencyLevelIndicator]] = relationship(
        back_populates="competency", cascade="all, delete-orphan"
    )


class CompetencyLevelIndicator(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """What a given rating looks like for this competency.

    Without these, "Collaboration: 3" is noise — one manager's 3 is another's
    4, the ratings are not comparable across a department, and calibration has
    nothing to argue from.
    """

    __tablename__ = "competency_level_indicators"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "competency_id", "scale_value", name="uq_competency_indicator"
        ),
    )

    competency_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("competencies.id", ondelete="CASCADE"),
        nullable=False,
    )
    scale_value: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)
    behaviour_description: Mapped[str] = mapped_column(Text, nullable=False)

    competency: Mapped[Competency] = relationship(back_populates="indicators")


class CompetencyTemplate(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A set of competencies for a kind of role."""

    __tablename__ = "competency_templates"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_competency_template_name"),)

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    applicable_roles: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    applicable_grades: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    items: Mapped[list[CompetencyTemplateItem]] = relationship(
        back_populates="template", cascade="all, delete-orphan"
    )


class CompetencyTemplateItem(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "competency_template_items"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "template_id", "competency_id", name="uq_template_competency"
        ),
    )

    template_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("competency_templates.id", ondelete="CASCADE"),
        nullable=False,
    )
    competency_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("competencies.id", ondelete="RESTRICT"),
        nullable=False,
    )
    weight: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)
    is_mandatory: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    template: Mapped[CompetencyTemplate] = relationship(back_populates="items")


class AppraisalTemplate(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """How a cycle's final score is composed.

    The weights are validated at save. Discovering at submission time that a
    template cannot be scored means the cycle has launched and managers have
    already started rating against it.
    """

    __tablename__ = "appraisal_templates"
    __table_args__ = (
        UniqueConstraint("tenant_id", "cycle_id", name="uq_appraisal_template_cycle"),
    )

    cycle_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("appraisal_cycles.id", ondelete="CASCADE"),
        nullable=False,
    )
    rating_scale_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("rating_scales.id", ondelete="RESTRICT"),
        nullable=False,
    )
    competency_template_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("competency_templates.id", ondelete="SET NULL"),
        nullable=True,
    )

    goal_weight: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)
    competency_weight: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)
    # Zero for a tenant not using the values dimension. Where it is non-zero,
    # recognition data appears alongside the rating as evidence.
    values_weight: Mapped[float] = mapped_column(Numeric(6, 2), default=0, nullable=False)

    score_precision: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    # [{"label": "Meets expectations", "lower": 65, "upper": 80}, ...] —
    # contiguous and non-overlapping, validated at save.
    score_bands: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)


class CompetencyValueLink(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """Which company value a competency maps to.

    ┌───────────────────────────────────────────────────────────────────────┐
    │ THE DIFFERENTIATOR, AND WHY IT IS A LINK RATHER THAN A SCORE          │
    │                                                                       │
    │ A manager rating someone on "collaboration" sees the shoutouts that   │
    │ person received for collaboration. That is the concrete mechanism     │
    │ behind culture data informing performance.                            │
    │                                                                       │
    │ It is deliberately **evidence, not an automatic score**. Recognition  │
    │ converted into a rating is a popularity metric wearing a competency's │
    │ name: it rewards visibility over contribution, and it is trivially    │
    │ gamed the moment anyone works out it counts. Shown *next to* the      │
    │ rating, it is context a manager weighs — which is what it actually    │
    │ is.                                                                   │
    └───────────────────────────────────────────────────────────────────────┘
    """

    __tablename__ = "competency_value_links"
    __table_args__ = (
        UniqueConstraint("tenant_id", "competency_id", "value_code", name="uq_competency_value"),
    )

    competency_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("competencies.id", ondelete="CASCADE"),
        nullable=False,
    )
    # The company value's code, as the Culture module knows it.
    value_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
