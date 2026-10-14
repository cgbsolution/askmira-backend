"""Group benefits: plans, who is enrolled with which family members, and changes.

A **plan** is a policy the company buys — group health, life, accident — with
its insurer, policy number, sum insured, which relations it covers, what the
company pays a year per covered member, and optional top-ups an employee may
buy on top. People enrol in the plan's window, choosing which dependants to
cover; an enrolment **snapshots** those members (name, relation, birth date,
gender), because the insurer's census is a list at a moment, not a live view
of somebody's profile.

Outside the window, a **life event** — marriage, a birth or adoption, a
divorce, a death — lets a person change who is covered, within the plan's
window of days after the event, once HR approves. Every change to an
enrolment's members is written to `benefit_member_changes` (insert-only),
which is what the insurer's endorsement file is built from.

A top-up's premium is a voluntary deduction: it reaches payroll as a monthly
payroll adjustment, which an admin approves like any other.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
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
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class BenefitKind(str, enum.Enum):
    HEALTH = "health"
    LIFE = "life"
    ACCIDENT = "accident"
    OTHER = "other"


class EnrolmentStatus(str, enum.Enum):
    ENROLLED = "enrolled"
    WAIVED = "waived"


class LifeEventKind(str, enum.Enum):
    MARRIAGE = "marriage"
    BIRTH = "birth"
    ADOPTION = "adoption"
    DIVORCE = "divorce"
    DEATH = "death"


class LifeEventStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class BenefitPlan(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "benefit_plans"
    __table_args__ = (
        CheckConstraint("policy_end >= policy_start", name="ck_benefit_plan_policy_dates"),
        CheckConstraint("life_event_window_days > 0", name="ck_benefit_plan_window"),
    )

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    kind: Mapped[BenefitKind] = mapped_column(
        Enum(BenefitKind, name="benefit_kind"), nullable=False
    )
    insurer: Mapped[str] = mapped_column(String(160), nullable=False)
    tpa: Mapped[str | None] = mapped_column(String(160), nullable=True)
    policy_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    policy_start: Mapped[date] = mapped_column(Date, nullable=False)
    policy_end: Mapped[date] = mapped_column(Date, nullable=False)
    sum_insured: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    #: Relations the plan covers besides the employee: spouse, child, parent,
    #: parent_in_law.
    covers: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    max_children: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: What the company pays a year for each covered person, the employee included.
    employer_premium_per_member: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), default=0, nullable=False
    )
    #: [{"code", "name", "sum_insured", "employee_premium_annual"}]
    top_ups: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    enrolment_opens: Mapped[date | None] = mapped_column(Date, nullable=True)
    enrolment_closes: Mapped[date | None] = mapped_column(Date, nullable=True)
    life_event_window_days: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class BenefitEnrolment(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "benefit_enrolments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "plan_id", "employee_id", name="uq_benefit_enrolment"),
    )

    plan_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("benefit_plans.id", ondelete="CASCADE", name="fk_benefit_enrolments_plan_id"),
        nullable=False,
        index=True,
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE", name="fk_benefit_enrolments_employee_id"),
        nullable=False,
        index=True,
    )
    status: Mapped[EnrolmentStatus] = mapped_column(
        Enum(EnrolmentStatus, name="benefit_enrolment_status"), nullable=False
    )
    #: Dependants covered, as they were given: [{name, relationship, date_of_birth, gender}].
    members: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    top_up_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    employee_premium_annual: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), default=0, nullable=False
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    payroll_adjustment_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "payroll_adjustments.id",
            ondelete="SET NULL",
            name="fk_benefit_enrolments_payroll_adjustment_id",
        ),
        nullable=True,
    )


class BenefitLifeEvent(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "benefit_life_events"

    enrolment_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "benefit_enrolments.id", ondelete="CASCADE", name="fk_benefit_life_events_enrolment"
        ),
        nullable=False,
        index=True,
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE", name="fk_benefit_life_events_employee"),
        nullable=False,
    )
    kind: Mapped[LifeEventKind] = mapped_column(
        Enum(LifeEventKind, name="benefit_life_event_kind"), nullable=False
    )
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    add_members: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    #: Names of covered members to remove, as they appear on the enrolment.
    remove_members: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[LifeEventStatus] = mapped_column(
        Enum(LifeEventStatus, name="benefit_life_event_status"),
        default=LifeEventStatus.PENDING,
        nullable=False,
        index=True,
    )
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL", name="fk_benefit_life_events_decided_by"),
        nullable=True,
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class BenefitMemberChange(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One person added to or removed from cover. Insert-only: the endorsement trail."""

    __tablename__ = "benefit_member_changes"

    enrolment_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "benefit_enrolments.id", ondelete="CASCADE", name="fk_benefit_member_changes_enrolment"
        ),
        nullable=False,
        index=True,
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("benefit_plans.id", ondelete="CASCADE", name="fk_benefit_member_changes_plan"),
        nullable=False,
        index=True,
    )
    #: "add" or "remove".
    change: Mapped[str] = mapped_column(String(8), nullable=False)
    member: Mapped[dict] = mapped_column(JSONB, nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    #: "enrolment", "life_event" or "waiver".
    source: Mapped[str] = mapped_column(String(16), nullable=False)
