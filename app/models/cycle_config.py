"""Cycle configuration: assignment rules and probation reviews.

Two additions to the slice 15 chain.

**`TemplateAssignmentRule`** decides which template each employee is reviewed
against. Rules overlap constantly in practice — "all engineers" and "all L5s"
both match an L5 engineer — so `priority` is unique per cycle: two rules at the
same priority means the winner depends on the order rows come back in, which is
not a decision anyone made.

**`ProbationReview`** runs on its own clock. A probation review is triggered by
one employee's confirmation date, not by a calendar cycle, and its outcome is
close to binary. `extends_review_id` chains an extension to the review that
caused it, so "how many times has this been extended" is answerable — which is
the question that matters when it has been extended three times.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class AutoAdvanceBehaviour(str, enum.Enum):
    """What happens to an appraisal still sitting on a stage that has closed.

    All three are real choices and none is a safe default, which is why the
    column has no default that quietly picks one.
    """

    # Keep it on the stage, raise it to the next level up. Nothing is skipped.
    ESCALATE = "escalate"
    # Move on, recording the stage as never filled in. The cycle finishes.
    ADVANCE = "advance"
    # Do nothing and wait for a human. Right for a tenant that wants to decide
    # case by case; wrong for one that will not look.
    HOLD = "hold"


class ProbationOutcome(str, enum.Enum):
    CONFIRM = "confirm"
    EXTEND = "extend"
    TERMINATE = "terminate"


class TemplateAssignmentRule(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """Which template an employee is reviewed against.

    A rule with empty `conditions` is the default and matches everyone. One is
    required before a cycle can activate — without it, an employee whose grade
    was typed with a trailing space gets no appraisal, and nobody notices until
    they ask.
    """

    __tablename__ = "template_assignment_rules"
    __table_args__ = (
        # Priority is unique per cycle. Two rules at the same priority means
        # the winner depends on row order, which is not a decision.
        UniqueConstraint(
            "tenant_id", "cycle_id", "priority", name="uq_assignment_rule_priority"
        ),
    )

    cycle_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("appraisal_cycles.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Lower runs first.
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    template_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("competency_templates.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # Only the keys the assignment engine knows: grades, department_ids,
    # location_ids, employment_types, min_tenure_months, max_tenure_months.
    # An unknown key is refused at validation — a rule that can never match
    # looks exactly like a rule that is not needed.
    conditions: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    label: Mapped[str | None] = mapped_column(String(128), nullable=True)


class ProbationReview(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A probation review, on one employee's own clock.

    Not part of a calendar cycle: it is triggered by the confirmation date on
    the employee record, which is why it has its own table rather than being an
    `Appraisal` with a different type.
    """

    __tablename__ = "probation_reviews"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    probation_end_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    # Deliberately allowed to be before the end date: the conversation should
    # happen before the deadline, not after it.
    review_due_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    outcome: Mapped[ProbationOutcome | None] = mapped_column(
        Enum(ProbationOutcome, name="probation_outcome"), nullable=True
    )
    outcome_recorded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    outcome_recorded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    extended_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    # The review this one extends. Chained rather than overwritten, so "how
    # many times has this been extended" is answerable — which is exactly the
    # question when it has happened three times.
    extends_review_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("probation_reviews.id", ondelete="SET NULL"),
        nullable=True,
    )
    # The confirmation letter, where the outcome was CONFIRM.
    confirmation_letter_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("issued_letters.id", ondelete="SET NULL"),
        nullable=True,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
