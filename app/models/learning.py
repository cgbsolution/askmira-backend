"""Courses, who must take them, and proof they did — POSH, compliance, skills.

A **course** is written by HR: modules to read and, optionally, a short quiz
with a pass mark. A course can lapse: with `validity_months` set, a completion
expires and the course is due again, as annual POSH awareness training is.

An **assignment** is one person and one course, with a due date. Passing it
records the score and the moment, issues a numbered certificate, and sets when
it expires. The quiz's correct answers are never sent to a learner.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
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


class CourseKind(str, enum.Enum):
    POSH = "posh"
    COMPLIANCE = "compliance"
    SAFETY = "safety"
    SKILL = "skill"
    OTHER = "other"


class AssignmentStatus(str, enum.Enum):
    ASSIGNED = "assigned"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class Course(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "courses"
    __table_args__ = (CheckConstraint("pass_mark BETWEEN 0 AND 100", name="ck_course_pass_mark"),)

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    kind: Mapped[CourseKind] = mapped_column(Enum(CourseKind, name="course_kind"), nullable=False)
    #: [{"title", "body", "url"}], in order.
    modules: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    #: [{"question", "options": [...], "correct": index}]. Never sent to learners.
    quiz: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    pass_mark: Mapped[int] = mapped_column(Integer, default=70, nullable=False)
    #: Months a completion stays valid; null never lapses.
    validity_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class CourseAssignment(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "course_assignments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "certificate_number", name="uq_course_certificate"),
    )

    course_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("courses.id", ondelete="CASCADE", name="fk_course_assignments_course"),
        nullable=False,
        index=True,
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE", name="fk_course_assignments_employee"),
        nullable=False,
        index=True,
    )
    assigned_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL", name="fk_course_assignments_assigned_by"),
        nullable=True,
    )
    due_on: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[AssignmentStatus] = mapped_column(
        Enum(AssignmentStatus, name="course_assignment_status"),
        default=AssignmentStatus.ASSIGNED,
        nullable=False,
        index=True,
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    certificate_number: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_reminded_on: Mapped[date | None] = mapped_column(Date, nullable=True)
