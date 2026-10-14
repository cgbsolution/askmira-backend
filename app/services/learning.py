"""Courses, assignments, quizzes, certificates and overdue reminders.

- HR writes a course and assigns it to named people or to an audience (the
  announcement rule: departments, locations, grades). Somebody who already
  holds a valid completion is not assigned again.
- A learner reads the modules and submits the quiz; the score is worked out
  here, from the stored answers, which the learner never receives. A pass
  completes the assignment, numbers a certificate and, for a course that
  lapses, sets when it expires. A fail can be retried.
- A course with no quiz is completed by the learner confirming they read it.
- A daily job reminds people whose assignment is overdue, at most once a week.
"""

from __future__ import annotations

import calendar
import secrets
import uuid
from datetime import UTC, date, datetime, timedelta
from html import escape
from typing import Any

from fastapi import HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee, EmploymentStatus
from app.models.learning import AssignmentStatus, Course, CourseAssignment, CourseKind
from app.models.tenant import Tenant
from app.models.user import User
from app.services import audit
from app.services.culture import comms as comms_engine
from app.services.notifications.dispatch import notify, register_event

__all__ = [
    "AssignIn",
    "CourseIn",
    "assign",
    "certificate_pdf",
    "complete",
    "course_report",
    "create_course",
    "list_courses",
    "my_assignments",
    "open_course",
    "send_overdue_reminders",
]

register_event(
    "learning.assigned",
    "New course: {course}",
    "You have been asked to complete {course} by {due_on}.",
)
register_event(
    "learning.overdue",
    "{course} is overdue",
    "{course} was due on {due_on}. Please complete it.",
)

REMIND_EVERY = timedelta(days=7)


class ModuleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    body: str | None = Field(default=None, max_length=20_000)
    url: str | None = Field(default=None, max_length=1000, pattern=r"^https://")


class QuestionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=3, max_length=500)
    options: list[str] = Field(min_length=2, max_length=6)
    correct: int = Field(ge=0)

    @model_validator(mode="after")
    def _in_range(self) -> QuestionIn:
        if self.correct >= len(self.options):
            raise ValueError("The correct answer must be one of the options.")
        return self


class CourseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=3, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    kind: CourseKind = CourseKind.COMPLIANCE
    modules: list[ModuleIn] = Field(min_length=1, max_length=50)
    quiz: list[QuestionIn] = Field(default_factory=list, max_length=50)
    pass_mark: int = Field(default=70, ge=0, le=100)
    validity_months: int | None = Field(default=None, ge=1, le=120)


class AssignIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    employee_ids: list[uuid.UUID] = Field(default_factory=list, max_length=5000)
    everyone: bool = False
    department_ids: list[uuid.UUID] = Field(default_factory=list)
    location_ids: list[uuid.UUID] = Field(default_factory=list)
    due_on: date


def _add_months(day: date, months: int) -> date:
    month = day.month - 1 + months
    year = day.year + month // 12
    month = month % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _course_read(course: Course, *, for_learner: bool) -> dict[str, Any]:
    quiz = [
        {"question": q["question"], "options": q["options"]} if for_learner else dict(q)
        for q in course.quiz or []
    ]
    return {
        "id": str(course.id),
        "title": course.title,
        "description": course.description,
        "kind": course.kind.value,
        "modules": list(course.modules or []),
        "quiz": quiz,
        "pass_mark": course.pass_mark,
        "validity_months": course.validity_months,
        "active": course.active,
    }


async def _course(session: AsyncSession, user: User, course_id: uuid.UUID) -> Course:
    row = await session.scalar(
        tenant_scoped(select(Course).where(Course.id == course_id), Course, user)
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course not found")
    return row


async def create_course(
    session: AsyncSession, user: User, payload: CourseIn, *, request: Request | None
) -> dict[str, Any]:
    permissions.require_hr(user, detail="Courses are written by HR.")
    row = Course(tenant_id=user.tenant_id, **payload.model_dump())
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="course",
        entity_id=row.id,
        actor=user,
        changes={"title": row.title, "kind": row.kind.value, "questions": len(row.quiz)},
        summary=f"Created the course {row.title}",
        request=request,
    )
    await session.commit()
    return _course_read(row, for_learner=False)


async def list_courses(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    permissions.require_hr(user, detail="The course list is for HR.")
    rows = (
        await session.scalars(tenant_scoped(select(Course).order_by(Course.title), Course, user))
    ).all()
    return [_course_read(r, for_learner=False) for r in rows]


async def assign(
    session: AsyncSession,
    user: User,
    course_id: uuid.UUID,
    payload: AssignIn,
    *,
    request: Request | None,
) -> dict[str, Any]:
    permissions.require_hr(user, detail="Courses are assigned by HR.")
    course = await _course(session, user, course_id)
    if not course.active:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This course is off.")
    people = (
        await session.scalars(
            tenant_scoped(
                select(Employee).where(
                    Employee.deleted_at.is_(None), Employee.status != EmploymentStatus.EXITED
                ),
                Employee,
                user,
            )
        )
    ).all()
    audience = comms_engine.Audience(
        department_ids=tuple(str(d) for d in payload.department_ids),
        location_ids=tuple(str(lo) for lo in payload.location_ids),
    )
    named = set(payload.employee_ids)
    chosen = [
        p
        for p in people
        if p.id in named
        or (
            (payload.everyone or not audience.is_everyone)
            and comms_engine.is_visible_to(
                audience,
                comms_engine.Recipient(
                    employee_id=str(p.id),
                    department_id=str(p.department_id) if p.department_id else None,
                    location_id=str(p.location_id) if p.location_id else None,
                    grade=p.grade,
                ),
            )
        )
    ]
    if not chosen:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Nobody matches."
        )
    today = date.today()
    current = {
        a.employee_id
        for a in (
            await session.scalars(
                tenant_scoped(
                    select(CourseAssignment).where(
                        CourseAssignment.course_id == course.id,
                        or_(
                            CourseAssignment.status == AssignmentStatus.ASSIGNED,
                            (CourseAssignment.status == AssignmentStatus.COMPLETED)
                            & or_(
                                CourseAssignment.expires_on.is_(None),
                                CourseAssignment.expires_on > today,
                            ),
                        ),
                    ),
                    CourseAssignment,
                    user,
                )
            )
        ).all()
    }
    created = [p for p in chosen if p.id not in current]
    for person in created:
        session.add(
            CourseAssignment(
                tenant_id=user.tenant_id,
                course_id=course.id,
                employee_id=person.id,
                assigned_by_user_id=user.id,
                due_on=payload.due_on,
            )
        )
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="course_assignment",
        entity_id=course.id,
        actor=user,
        changes={
            "assigned": len(created),
            "already_current": len(chosen) - len(created),
            "due_on": payload.due_on.isoformat(),
        },
        summary=f"Assigned {course.title} to {len(created)} people",
        request=request,
    )
    if created:
        recipients = list(
            (
                await session.scalars(
                    tenant_scoped(
                        select(User.id).where(
                            User.employee_id.in_([p.id for p in created]),
                            User.is_active.is_(True),
                        ),
                        User,
                        user,
                    )
                )
            ).all()
        )
        if recipients:
            await notify(
                session,
                tenant_id=user.tenant_id,
                event_key="learning.assigned",
                recipient_user_ids=recipients,
                payload={"course": course.title, "due_on": payload.due_on.strftime("%d %b %Y")},
                commit=False,
            )
    await session.commit()
    return {"assigned": len(created), "already_current": len(chosen) - len(created)}


def _assignment_read(a: CourseAssignment, course: Course) -> dict[str, Any]:
    today = date.today()
    return {
        "id": str(a.id),
        "course_id": str(course.id),
        "course": course.title,
        "kind": course.kind.value,
        "due_on": a.due_on.isoformat(),
        "status": a.status.value,
        "overdue": a.status is AssignmentStatus.ASSIGNED and a.due_on < today,
        "attempts": a.attempts,
        "score": a.score,
        "completed_at": a.completed_at.isoformat() if a.completed_at else None,
        "expires_on": a.expires_on.isoformat() if a.expires_on else None,
        "expired": a.expires_on is not None and a.expires_on <= today,
        "certificate_number": a.certificate_number,
    }


async def my_assignments(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    permissions.own_employee_id(user)
    rows = (
        await session.execute(
            tenant_scoped(
                select(CourseAssignment, Course)
                .join(Course, Course.id == CourseAssignment.course_id)
                .where(
                    CourseAssignment.employee_id == user.employee_id,
                    CourseAssignment.status != AssignmentStatus.CANCELLED,
                )
                .order_by(CourseAssignment.due_on),
                CourseAssignment,
                user,
            )
        )
    ).all()
    return [_assignment_read(a, c) for a, c in rows]


async def _own_assignment(
    session: AsyncSession, user: User, assignment_id: uuid.UUID
) -> tuple[CourseAssignment, Course]:
    permissions.own_employee_id(user)
    row = await session.execute(
        tenant_scoped(
            select(CourseAssignment, Course)
            .join(Course, Course.id == CourseAssignment.course_id)
            .where(
                CourseAssignment.id == assignment_id,
                CourseAssignment.employee_id == user.employee_id,
            ),
            CourseAssignment,
            user,
        )
    )
    found = row.first()
    if found is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course not found")
    return found[0], found[1]


async def open_course(
    session: AsyncSession, user: User, assignment_id: uuid.UUID
) -> dict[str, Any]:
    """The course as a learner sees it: modules and questions, never the answers."""
    permissions.own_employee_id(user)
    assignment, course = await _own_assignment(session, user, assignment_id)
    return {
        **_assignment_read(assignment, course),
        "content": _course_read(course, for_learner=True),
    }


async def complete(
    session: AsyncSession,
    user: User,
    assignment_id: uuid.UUID,
    *,
    answers: list[int],
    request: Request | None = None,
) -> dict[str, Any]:
    permissions.own_employee_id(user)
    assignment, course = await _own_assignment(session, user, assignment_id)
    if assignment.status is not AssignmentStatus.ASSIGNED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already completed.")
    quiz = list(course.quiz or [])
    assignment.attempts += 1
    if quiz:
        if len(answers) != len(quiz):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Answer every question.",
            )
        right = sum(1 for q, a in zip(quiz, answers, strict=True) if q["correct"] == a)
        score = round(right * 100 / len(quiz))
    else:
        score = 100
    assignment.score = score
    passed = score >= course.pass_mark
    if passed:
        now = datetime.now(UTC)
        assignment.status = AssignmentStatus.COMPLETED
        assignment.completed_at = now
        assignment.certificate_number = f"C-{now:%Y%m%d}-{secrets.token_hex(4).upper()}"
        if course.validity_months:
            assignment.expires_on = _add_months(now.date(), course.validity_months)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="course_assignment",
        entity_id=assignment.id,
        actor=user,
        changes={"score": score, "passed": passed, "attempt": assignment.attempts},
        summary=f"{'Passed' if passed else 'Did not pass'} {course.title} with {score}%",
        request=request,
    )
    await session.commit()
    return {**_assignment_read(assignment, course), "passed": passed, "pass_mark": course.pass_mark}


async def certificate_pdf(
    session: AsyncSession, user: User, assignment_id: uuid.UUID
) -> tuple[bytes, str]:
    """The certificate for a completed course, for the person or HR."""
    row = (
        await session.execute(
            tenant_scoped(
                select(CourseAssignment, Course, Employee)
                .join(Course, Course.id == CourseAssignment.course_id)
                .join(Employee, Employee.id == CourseAssignment.employee_id)
                .where(CourseAssignment.id == assignment_id),
                CourseAssignment,
                user,
            )
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    assignment, course, employee = row
    if not (permissions.is_hr(user) or user.employee_id == employee.id):
        raise permissions.forbidden("A certificate is for the person and HR.")
    if assignment.status is not AssignmentStatus.COMPLETED or not assignment.completed_at:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Not completed yet.")
    from app.services.documents.pdf import Letterhead, render_pdf

    tenant = await session.get(Tenant, user.tenant_id)
    expires = (
        f"<p>Valid until {assignment.expires_on:%d %B %Y}.</p>" if assignment.expires_on else ""
    )
    html = (
        "<h1>Certificate of completion</h1>"
        f"<p>This is to certify that <strong>{escape(employee.full_name)}</strong> "
        f"({escape(employee.employee_code)}) completed <strong>{escape(course.title)}</strong> "
        f"on {assignment.completed_at:%d %B %Y} with a score of {assignment.score}%.</p>"
        f"{expires}"
        f"<p>Certificate number {escape(assignment.certificate_number or '')}.</p>"
    )
    data = render_pdf(
        merged_html=html,
        reference_number=escape(assignment.certificate_number or ""),
        letterhead=Letterhead(company_name=escape(tenant.name if tenant else "Employer")),
    )
    return data, f"certificate-{assignment.certificate_number}.pdf"


async def course_report(session: AsyncSession, user: User, course_id: uuid.UUID) -> dict[str, Any]:
    """Who has completed, who is due and who is overdue, for one course."""
    permissions.require_hr(user, detail="Course reports are for HR.")
    course = await _course(session, user, course_id)
    rows = (
        await session.execute(
            tenant_scoped(
                select(CourseAssignment, Employee)
                .join(Employee, Employee.id == CourseAssignment.employee_id)
                .where(
                    CourseAssignment.course_id == course.id,
                    CourseAssignment.status != AssignmentStatus.CANCELLED,
                )
                .order_by(Employee.employee_code),
                CourseAssignment,
                user,
            )
        )
    ).all()
    people = [
        {
            **_assignment_read(a, course),
            "employee_name": e.full_name,
            "employee_code": e.employee_code,
        }
        for a, e in rows
    ]
    return {
        "course": _course_read(course, for_learner=False),
        "assigned": len(people),
        "completed": sum(1 for p in people if p["status"] == "completed" and not p["expired"]),
        "overdue": sum(1 for p in people if p["overdue"]),
        "expired": sum(1 for p in people if p["expired"]),
        "people": people,
    }


async def send_overdue_reminders(
    session: AsyncSession, actor: User, *, today: date
) -> dict[str, Any]:
    """Scheduler: remind each person with an overdue course, at most weekly."""
    rows = (
        await session.execute(
            tenant_scoped(
                select(CourseAssignment, Course)
                .join(Course, Course.id == CourseAssignment.course_id)
                .where(
                    CourseAssignment.status == AssignmentStatus.ASSIGNED,
                    CourseAssignment.due_on < today,
                    or_(
                        CourseAssignment.last_reminded_on.is_(None),
                        CourseAssignment.last_reminded_on <= today - REMIND_EVERY,
                    ),
                ),
                CourseAssignment,
                actor,
            )
        )
    ).all()
    sent = 0
    for assignment, course in rows:
        recipients = list(
            (
                await session.scalars(
                    tenant_scoped(
                        select(User.id).where(
                            User.employee_id == assignment.employee_id, User.is_active.is_(True)
                        ),
                        User,
                        actor,
                    )
                )
            ).all()
        )
        assignment.last_reminded_on = today
        if recipients:
            await notify(
                session,
                tenant_id=actor.tenant_id,
                event_key="learning.overdue",
                recipient_user_ids=recipients,
                payload={"course": course.title, "due_on": assignment.due_on.strftime("%d %b %Y")},
                commit=False,
            )
            sent += 1
    await session.commit()
    pending = await session.scalar(
        tenant_scoped(
            select(func.count())
            .select_from(CourseAssignment)
            .where(
                CourseAssignment.status == AssignmentStatus.ASSIGNED,
                CourseAssignment.due_on < today,
            ),
            CourseAssignment,
            actor,
        )
    )
    return {"reminded": sent, "overdue": int(pending or 0)}
