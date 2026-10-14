"""Onboarding: pre-boarding and the joining checklist.

The shape mirrors exit clearance, turned around: a tenant keeps checklists
(`OnboardingTemplate` with ordered `OnboardingTemplateTask` rows), and starting
onboarding for one person copies a checklist into an `OnboardingPlan` with its
own `OnboardingTask` rows.

**Tasks are copied, not referenced.** Editing a checklist after somebody has
started must not move a task they are halfway through, change who owns it or
shift its due date. The plan keeps what it was started with; the template is
only where the next plan comes from.

**Due dates are relative to the joining date.** A task due seven days before
joining is pre-boarding (documents, offer acceptance); a task due thirty days
after is the day-30 check-in. The offset is stored on the template, the
resolved calendar date on the task.

**Assignees are resolved when the plan starts.** The new joiner's login, their
manager's login, the buddy's login, or — for HR, IT and finance work — the
person the checklist names or else the HR user who started the plan. A person
with no login yet (a pre-joiner HR has not invited) leaves the task unassigned;
the new joiner's own tasks are still theirs, because who may complete them is
decided by whose plan it is, not by the assignee column.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class OnboardingOwnerType(str, enum.Enum):
    """Who a checklist task belongs to."""

    NEW_JOINER = "new_joiner"
    MANAGER = "manager"
    HR = "hr"
    IT = "it"
    BUDDY = "buddy"
    FINANCE = "finance"


class OnboardingTaskType(str, enum.Enum):
    GENERAL = "general"
    # Completed by uploading a file into the joiner's document vault.
    DOCUMENT_UPLOAD = "document_upload"
    # A declaration the joiner fills in (PF nomination, Form 11).
    FORM = "form"
    # Only the joiner can acknowledge a policy; nobody does it for them.
    ACKNOWLEDGE_POLICY = "acknowledge_policy"
    MEETING = "meeting"


class OnboardingPlanStatus(str, enum.Enum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class OnboardingTaskStatus(str, enum.Enum):
    PENDING = "pending"
    DONE = "done"
    # Deliberately not done, by HR, with a reason. Not the same as done.
    SKIPPED = "skipped"


class OnboardingTemplate(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A tenant's onboarding checklist."""

    __tablename__ = "onboarding_templates"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_onboarding_template_name"),)

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Applicability. Empty means every department / every location.
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("departments.id", ondelete="SET NULL"), nullable=True
    )
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("locations.id", ondelete="SET NULL"), nullable=True
    )

    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # At most one per tenant. The default is what a converted offer starts on.
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    tasks: Mapped[list[OnboardingTemplateTask]] = relationship(
        back_populates="template",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="OnboardingTemplateTask.sort_order",
    )


class OnboardingTemplateTask(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One line of a checklist."""

    __tablename__ = "onboarding_template_tasks"

    template_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("onboarding_templates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    owner_type: Mapped[OnboardingOwnerType] = mapped_column(
        Enum(OnboardingOwnerType, name="onboarding_owner_type"), nullable=False
    )
    # For HR, IT and finance work: the person who does it. Empty means the HR
    # user who starts the plan.
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # Days relative to the joining date. Negative is pre-boarding.
    due_offset_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    task_type: Mapped[OnboardingTaskType] = mapped_column(
        Enum(OnboardingTaskType, name="onboarding_task_type"),
        default=OnboardingTaskType.GENERAL,
        nullable=False,
    )
    # The vault category an upload lands in (`DocumentCategory` value).
    document_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: A FORM task's statutory form: the letter template code it generates
    #: (FORM11_STD, FORM2_STD, FORMF_STD, ESI_DECL_STD).
    form_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_mandatory: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    template: Mapped[OnboardingTemplate] = relationship(back_populates="tasks")


class OnboardingPlan(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One person's onboarding, instantiated from a checklist."""

    __tablename__ = "onboarding_plans"
    __table_args__ = (
        # One live plan per person. A cancelled or completed plan does not
        # block a fresh start.
        Index(
            "uq_onboarding_plan_in_progress",
            "tenant_id",
            "employee_id",
            unique=True,
            postgresql_where=text("status = 'IN_PROGRESS'"),
        ),
    )

    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # SET NULL: a checklist may be deleted later; the plan keeps its name.
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("onboarding_templates.id", ondelete="SET NULL"),
        nullable=True,
    )
    template_name: Mapped[str] = mapped_column(String(160), nullable=False)

    joining_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    buddy_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )

    status: Mapped[OnboardingPlanStatus] = mapped_column(
        Enum(OnboardingPlanStatus, name="onboarding_plan_status"),
        default=OnboardingPlanStatus.IN_PROGRESS,
        nullable=False,
        index=True,
    )
    started_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    tasks: Mapped[list[OnboardingTask]] = relationship(
        back_populates="plan",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="OnboardingTask.sort_order",
    )


class OnboardingTask(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One task on one person's plan, with its resolved owner and date."""

    __tablename__ = "onboarding_tasks"
    __table_args__ = (
        Index("ix_onboarding_tasks_assignee_status", "tenant_id", "assignee_user_id", "status"),
    )

    plan_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("onboarding_plans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    template_task_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("onboarding_template_tasks.id", ondelete="SET NULL"),
        nullable=True,
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_type: Mapped[OnboardingOwnerType] = mapped_column(
        Enum(OnboardingOwnerType, name="onboarding_owner_type"), nullable=False
    )
    assignee_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    due_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    task_type: Mapped[OnboardingTaskType] = mapped_column(
        Enum(OnboardingTaskType, name="onboarding_task_type"), nullable=False
    )
    document_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_mandatory: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    status: Mapped[OnboardingTaskStatus] = mapped_column(
        Enum(OnboardingTaskStatus, name="onboarding_task_status"),
        default=OnboardingTaskStatus.PENDING,
        nullable=False,
        index=True,
    )
    completed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employee_documents.id", ondelete="SET NULL"),
        nullable=True,
    )
    #: A FORM task: which form it generates, and the issued form the joiner
    #: signs by acknowledging. Acknowledging that letter completes the task.
    form_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    form_letter_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("issued_letters.id", ondelete="SET NULL"),
        nullable=True,
    )
    # When the assignee was told it is overdue — once, not every morning.
    overdue_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    plan: Mapped[OnboardingPlan] = relationship(back_populates="tasks")
