"""360 feedback requests, the reviewer's form, reminders, and private feedback.

Who may do what:

- **Ask.** The person about themselves; a manager about somebody in their
  reporting line; HR about anybody. Up to eight colleagues, never the person
  themselves, and only colleagues with a login — somebody who cannot sign in
  cannot answer.
- **Answer.** The colleague asked, and only they (`_my_reviewer_row`). HR does
  not answer on anybody's behalf.
- **Read.** The manager line above the person and HR see every answer with its
  writer's name. The person sees what the request's visibility allows — by
  default an anonymised summary, and only once at least three colleagues have
  answered. A colleague who was asked sees their own answer and nothing else.

Private feedback is a note from one colleague to another, visible to the
recipient, the recipient's manager line and the writer. It is not on any feed
and HR does not read it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.appraisal import Appraisal, AppraisalCycle, CycleStatus
from app.models.audit import AuditAction
from app.models.competency import (
    AppraisalTemplate,
    Competency,
    CompetencyTemplateItem,
    RatingScale,
    RatingScaleLevel,
)
from app.models.employee import Employee, EmploymentStatus
from app.models.feedback import (
    FeedbackRequest,
    FeedbackRequestStatus,
    FeedbackReviewer,
    FeedbackReviewerStatus,
    FeedbackVisibility,
    PrivateFeedback,
)
from app.models.notifications import Channel, DigestMode
from app.models.user import User
from app.schemas.feedback import (
    CompetencyAggregateRead,
    CompetencyAnswerIn,
    CompetencyAnswerRead,
    FeedbackAggregateRead,
    FeedbackDecline,
    FeedbackForm,
    FeedbackInboxItem,
    FeedbackQuestion,
    FeedbackRemindResult,
    FeedbackRequestCreate,
    FeedbackRequestDetail,
    FeedbackRequestSummary,
    FeedbackResponseSubmit,
    FeedbackScale,
    PrivateFeedbackCreate,
    PrivateFeedbackRead,
    ResponseRead,
    ReviewerStatusRead,
)
from app.services import audit
from app.services.notifications.dispatch import notify, register_event
from app.services.performance import feedback_engine as engine
from app.services.performance import notices

__all__ = [
    "close_request",
    "create_request",
    "decline",
    "get_form",
    "get_request",
    "give_private",
    "inbox",
    "inbox_items",
    "list_private",
    "list_requests",
    "remind",
    "send_due_reminders",
    "submit",
]

_PROMPT = {Channel.IN_APP: DigestMode.IMMEDIATE, Channel.EMAIL: DigestMode.IMMEDIATE}

register_event(
    "performance.feedback.requested",
    "Your feedback on {subject_name} is requested",
    "{requester_name} asked for your feedback on {subject_name}, by {due_on}. "
    "Open Performance → Feedback to answer.",
    preferences=_PROMPT,
)
register_event(
    "performance.feedback.reminder",
    "Reminder: feedback on {subject_name} is due {due_on}",
    "Your feedback on {subject_name} is still waiting, and is due {due_on}.",
    preferences=_PROMPT,
)
register_event(
    "performance.feedback.submitted",
    "{reviewer_name} answered the feedback request on {subject_name}",
    "{reviewer_name} submitted feedback on {subject_name}. {responses} of {total} have answered.",
)
register_event(
    "performance.feedback.summary_ready",
    "Your feedback summary is ready",
    "{responses} colleagues have answered, so your anonymised feedback summary is now open.",
)
register_event(
    "performance.feedback.private_received",
    "{from_name} sent you feedback",
    "{from_name} sent you private feedback. Only you and your manager can read it.",
    preferences=_PROMPT,
)

#: A pending reviewer is reminded when the due date is this close, and no more
#: often than every `REMINDER_GAP`.
REMIND_WITHIN_DAYS = 2
REMINDER_GAP = timedelta(days=2)
#: Past this many days overdue the scheduler stops reminding; the request is stale.
STOP_REMINDING_AFTER_DAYS = 14


def _not_found(detail: str = "Feedback request not found") -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


def _d(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


async def _today(session: AsyncSession, user: User) -> date:
    from app.services.attendance.service import local_today

    return await local_today(session, user)


# ── Loading ─────────────────────────────────────────────────────────────────


async def _employees(
    session: AsyncSession, user: User, ids: set[uuid.UUID]
) -> dict[uuid.UUID, Employee]:
    if not ids:
        return {}
    rows = (
        await session.scalars(
            tenant_scoped(select(Employee).where(Employee.id.in_(ids)), Employee, user)
        )
    ).all()
    return {row.id: row for row in rows}


async def _user_names(
    session: AsyncSession, user: User, ids: set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = (
        await session.execute(
            tenant_scoped(select(User.id, User.full_name).where(User.id.in_(ids)), User, user)
        )
    ).all()
    return {uid: name for uid, name in rows}


async def _cycle_names(
    session: AsyncSession, user: User, ids: set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = (
        await session.execute(
            tenant_scoped(
                select(AppraisalCycle.id, AppraisalCycle.name).where(AppraisalCycle.id.in_(ids)),
                AppraisalCycle,
                user,
            )
        )
    ).all()
    return {cid: name for cid, name in rows}


async def _request(session: AsyncSession, user: User, request_id: uuid.UUID) -> FeedbackRequest:
    row = await session.scalar(
        tenant_scoped(
            select(FeedbackRequest).where(FeedbackRequest.id == request_id), FeedbackRequest, user
        )
    )
    if row is None:
        raise _not_found()
    return row


async def _reviewers(
    session: AsyncSession, user: User, request_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[FeedbackReviewer]]:
    grouped: dict[uuid.UUID, list[FeedbackReviewer]] = {rid: [] for rid in request_ids}
    if not request_ids:
        return grouped
    rows = (
        await session.scalars(
            tenant_scoped(
                select(FeedbackReviewer)
                .where(FeedbackReviewer.request_id.in_(request_ids))
                .order_by(FeedbackReviewer.created_at),
                FeedbackReviewer,
                user,
            )
        )
    ).all()
    for row in rows:
        grouped.setdefault(row.request_id, []).append(row)
    return grouped


async def _viewer_role(
    session: AsyncSession, user: User, req: FeedbackRequest, line: set[uuid.UUID] | None = None
) -> str | None:
    """How the caller relates to a request: subject, manager, hr, requester — or None.

    The person the feedback is about is always the subject, whatever else they
    are: HR does not get to read the names on their own 360.
    """
    if user.employee_id is not None and req.subject_employee_id == user.employee_id:
        return "subject"
    if line is None:
        line = await permissions.reporting_line(session, user)
    if req.subject_employee_id in line:
        return "manager"
    if permissions.is_hr(user):
        return "hr"
    if req.requested_by_user_id == user.id:
        return "requester"
    return None


# ── Asking ──────────────────────────────────────────────────────────────────


async def _form_snapshot(
    session: AsyncSession, user: User, cycle: AppraisalCycle, subject_id: uuid.UUID
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The cycle's competencies and rating scale, copied onto the request."""
    template = await session.scalar(
        tenant_scoped(
            select(AppraisalTemplate).where(AppraisalTemplate.cycle_id == cycle.id),
            AppraisalTemplate,
            user,
        )
    )
    if template is None:
        return [], {}
    appraisal = await session.scalar(
        tenant_scoped(
            select(Appraisal).where(
                Appraisal.cycle_id == cycle.id, Appraisal.employee_id == subject_id
            ),
            Appraisal,
            user,
        )
    )
    competency_template_id = (
        appraisal.competency_template_id if appraisal is not None else None
    ) or template.competency_template_id
    questions: list[dict[str, Any]] = []
    if competency_template_id is not None:
        rows = (
            await session.execute(
                tenant_scoped(
                    select(Competency)
                    .join(
                        CompetencyTemplateItem,
                        CompetencyTemplateItem.competency_id == Competency.id,
                    )
                    .where(CompetencyTemplateItem.template_id == competency_template_id)
                    .order_by(CompetencyTemplateItem.display_order),
                    Competency,
                    user,
                )
            )
        ).scalars()
        questions = [
            {
                "competency_id": str(c.id),
                "code": c.code,
                "name": c.name,
                "description": c.description,
            }
            for c in rows
        ]
    scale: dict[str, Any] = {}
    rating_scale = await session.scalar(
        tenant_scoped(
            select(RatingScale).where(RatingScale.id == template.rating_scale_id),
            RatingScale,
            user,
        )
    )
    if rating_scale is not None and questions:
        levels = (
            await session.scalars(
                tenant_scoped(
                    select(RatingScaleLevel)
                    .where(RatingScaleLevel.scale_id == rating_scale.id)
                    .order_by(RatingScaleLevel.value),
                    RatingScaleLevel,
                    user,
                )
            )
        ).all()
        scale = {
            "min": str(rating_scale.min_value),
            "max": str(rating_scale.max_value),
            "levels": [{"value": str(lv.value), "label": lv.label} for lv in levels],
        }
    return questions, scale


async def create_request(
    session: AsyncSession,
    user: User,
    payload: FeedbackRequestCreate,
    *,
    request: Request | None = None,
) -> FeedbackRequestDetail:
    """Ask up to eight colleagues for feedback about one person."""
    subject_id = payload.subject_employee_id or permissions.own_employee_id(user)
    if subject_id != user.employee_id:
        await permissions.require_manager_or_hr(
            session,
            user,
            subject_id,
            detail="You can ask for feedback about yourself or somebody in your reporting line.",
            allow_self=True,
        )
    reviewer_ids = list(dict.fromkeys(payload.reviewer_employee_ids))
    if subject_id in reviewer_ids:
        raise _unprocessable("The person the feedback is about cannot be one of its reviewers.")
    if len(reviewer_ids) > engine.MAX_REVIEWERS:
        raise _unprocessable(f"Ask at most {engine.MAX_REVIEWERS} colleagues in one request.")
    if payload.due_on < await _today(session, user):
        raise _unprocessable("The due date has already passed.")

    people = await _employees(session, user, {subject_id, *reviewer_ids})
    subject = people.get(subject_id)
    if subject is None or subject.deleted_at is not None:
        raise _not_found("Employee not found")
    missing = [rid for rid in reviewer_ids if rid not in people]
    if missing:
        raise _not_found("A colleague you chose was not found.")
    gone = [
        people[rid].full_name
        for rid in reviewer_ids
        if people[rid].deleted_at is not None or people[rid].status is EmploymentStatus.EXITED
    ]
    if gone:
        raise _unprocessable(f"No longer with the company: {', '.join(gone)}.")
    logins = await notices.user_ids_for_employees(session, user, reviewer_ids)
    no_login = [people[rid].full_name for rid in reviewer_ids if rid not in logins]
    if no_login:
        raise _unprocessable(
            "These colleagues cannot sign in, so they could not answer: "
            + ", ".join(no_login)
            + "."
        )

    questions: list[dict[str, Any]] = []
    scale: dict[str, Any] = {}
    cycle: AppraisalCycle | None = None
    if payload.appraisal_cycle_id is not None:
        cycle = await session.scalar(
            tenant_scoped(
                select(AppraisalCycle).where(AppraisalCycle.id == payload.appraisal_cycle_id),
                AppraisalCycle,
                user,
            )
        )
        if cycle is None:
            raise _not_found("Appraisal cycle not found")
        if cycle.status is CycleStatus.CLOSED:
            raise _conflict("That review cycle is closed.")
        questions, scale = await _form_snapshot(session, user, cycle, subject_id)

    row = FeedbackRequest(
        tenant_id=user.tenant_id,
        subject_employee_id=subject_id,
        requested_by_user_id=user.id,
        appraisal_cycle_id=cycle.id if cycle else None,
        message=(payload.message or "").strip() or None,
        due_on=payload.due_on,
        status=FeedbackRequestStatus.OPEN,
        employee_visibility=payload.employee_visibility,
        anonymity_threshold=engine.ANONYMITY_FLOOR,
        questions=questions,
        scale=scale,
    )
    session.add(row)
    await session.flush()
    skip_level = None
    if subject.manager_id is not None:
        skip_level = await session.scalar(
            tenant_scoped(
                select(Employee.manager_id).where(Employee.id == subject.manager_id),
                Employee,
                user,
            )
        )
    for rid in reviewer_ids:
        session.add(
            FeedbackReviewer(
                tenant_id=user.tenant_id,
                request_id=row.id,
                reviewer_employee_id=rid,
                status=FeedbackReviewerStatus.PENDING,
                competency_ratings=[],
                relation=engine.relationship_of(
                    subject_id=subject.id,
                    subject_manager_id=subject.manager_id,
                    skip_level_id=skip_level,
                    reviewer_id=rid,
                    reviewer_manager_id=people[rid].manager_id,
                ),
            )
        )
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="feedback_request",
        entity_id=row.id,
        actor=user,
        changes={
            "subject_employee_id": str(subject_id),
            "reviewers": len(reviewer_ids),
            "appraisal_cycle_id": str(cycle.id) if cycle else None,
            "employee_visibility": payload.employee_visibility.value,
            "questions": len(questions),
        },
        summary=f"Asked {len(reviewer_ids)} colleague(s) for feedback on {subject.full_name}",
        request=request,
    )
    await notify(
        session,
        tenant_id=user.tenant_id,
        event_key="performance.feedback.requested",
        recipient_user_ids=list(logins.values()),
        payload={
            "requester_name": user.full_name,
            "subject_name": subject.full_name,
            "due_on": f"{payload.due_on:%d %b %Y}",
        },
        commit=False,
    )
    await session.commit()
    return await get_request(session, user, row.id)


# ── Reading requests ────────────────────────────────────────────────────────


def _summary(
    req: FeedbackRequest,
    role: str,
    reviewers: list[FeedbackReviewer],
    people: dict[uuid.UUID, Employee],
    user_names: dict[uuid.UUID, str],
    cycles: dict[uuid.UUID, str],
) -> dict[str, Any]:
    subject = people.get(req.subject_employee_id)
    return {
        "id": req.id,
        "subject_employee_id": req.subject_employee_id,
        "subject_name": subject.full_name if subject else "Unknown",
        "requested_by_name": (
            user_names.get(req.requested_by_user_id) if req.requested_by_user_id else None
        ),
        "appraisal_cycle_id": req.appraisal_cycle_id,
        "cycle_name": cycles.get(req.appraisal_cycle_id) if req.appraisal_cycle_id else None,
        "due_on": req.due_on,
        "status": req.status,
        "employee_visibility": req.employee_visibility,
        "reviewers_total": len(reviewers),
        "responses": sum(r.status is FeedbackReviewerStatus.SUBMITTED for r in reviewers),
        "declined": sum(r.status is FeedbackReviewerStatus.DECLINED for r in reviewers),
        "created_at": req.created_at,
        "my_role": role,
    }


async def list_requests(
    session: AsyncSession, user: User, *, scope: str = "about_me"
) -> list[FeedbackRequestSummary]:
    """Requests about the caller, about their reporting line, or (HR) everybody."""
    line = await permissions.reporting_line(session, user)
    stmt = tenant_scoped(select(FeedbackRequest), FeedbackRequest, user).order_by(
        FeedbackRequest.created_at.desc()
    )
    if scope == "about_me":
        if user.employee_id is None:
            return []
        stmt = stmt.where(FeedbackRequest.subject_employee_id == user.employee_id)
    elif scope == "team":
        conditions = [FeedbackRequest.requested_by_user_id == user.id]
        if line:
            conditions.append(FeedbackRequest.subject_employee_id.in_(line))
        stmt = stmt.where(or_(*conditions))
        if user.employee_id is not None:
            stmt = stmt.where(FeedbackRequest.subject_employee_id != user.employee_id)
    elif scope == "all":
        permissions.require_hr(user, detail="Every feedback request is visible to HR only.")
    rows = list((await session.scalars(stmt.limit(500))).all())
    roles = {r.id: await _viewer_role(session, user, r, line) for r in rows}
    rows = [r for r in rows if roles[r.id] is not None]
    reviewers = await _reviewers(session, user, [r.id for r in rows])
    people = await _employees(session, user, {r.subject_employee_id for r in rows})
    user_names = await _user_names(
        session, user, {r.requested_by_user_id for r in rows if r.requested_by_user_id}
    )
    cycles = await _cycle_names(
        session, user, {r.appraisal_cycle_id for r in rows if r.appraisal_cycle_id}
    )
    return [
        FeedbackRequestSummary(
            **_summary(r, roles[r.id] or "", reviewers.get(r.id, []), people, user_names, cycles)
        )
        for r in rows
    ]


def _answers(
    reviewers: list[FeedbackReviewer],
) -> list[engine.Answer]:
    out = []
    for r in reviewers:
        if r.status is not FeedbackReviewerStatus.SUBMITTED:
            continue
        ratings: dict[str, Decimal | None] = {}
        comments: dict[str, str] = {}
        for item in r.competency_ratings or []:
            key = str(item.get("competency_id"))
            ratings[key] = _d(item.get("rating"))
            if item.get("comment"):
                comments[key] = str(item["comment"])
        out.append(
            engine.Answer(
                ratings=ratings,
                comments=comments,
                strengths=r.strengths,
                improvements=r.improvements,
                overall=_d(r.overall_rating),
            )
        )
    return out


def _aggregate_read(
    reviewers: list[FeedbackReviewer], questions: list[dict]
) -> FeedbackAggregateRead:
    result = engine.aggregate(_answers(reviewers), questions)
    return FeedbackAggregateRead(
        responses=result.responses,
        overall_average=result.overall_average,
        overall_count=result.overall_count,
        competencies=[CompetencyAggregateRead(**c) for c in result.competencies],
        strengths=result.strengths,
        improvements=result.improvements,
    )


def _named_responses(
    reviewers: list[FeedbackReviewer],
    people: dict[uuid.UUID, Employee],
    questions: list[dict],
) -> list[ResponseRead]:
    names = {str(q["competency_id"]): q.get("name", "") for q in questions}
    out = []
    for r in reviewers:
        if r.status is not FeedbackReviewerStatus.SUBMITTED or r.submitted_at is None:
            continue
        person = people.get(r.reviewer_employee_id)
        out.append(
            ResponseRead(
                reviewer_name=person.full_name if person else "Unknown",
                relationship=r.relation,
                competency_ratings=[
                    CompetencyAnswerRead(
                        competency_id=uuid.UUID(str(item["competency_id"])),
                        name=names.get(str(item["competency_id"]), ""),
                        rating=_d(item.get("rating")),
                        comment=item.get("comment"),
                    )
                    for item in (r.competency_ratings or [])
                ],
                strengths=r.strengths,
                improvements=r.improvements,
                overall_rating=_d(r.overall_rating),
                submitted_at=r.submitted_at,
            )
        )
    return out


async def get_request(
    session: AsyncSession, user: User, request_id: uuid.UUID
) -> FeedbackRequestDetail:
    """One request, as much of it as this viewer may see."""
    req = await _request(session, user, request_id)
    role = await _viewer_role(session, user, req)
    if role is None:
        # A colleague who was only asked reads their own answer through the form.
        raise _not_found()
    reviewers = (await _reviewers(session, user, [req.id]))[req.id]
    people = await _employees(
        session, user, {req.subject_employee_id, *(r.reviewer_employee_id for r in reviewers)}
    )
    user_names = await _user_names(
        session, user, {req.requested_by_user_id} if req.requested_by_user_id else set()
    )
    cycles = await _cycle_names(
        session, user, {req.appraisal_cycle_id} if req.appraisal_cycle_id else set()
    )
    summary = _summary(req, role, reviewers, people, user_names, cycles)
    submitted = summary["responses"]
    questions = list(req.questions or [])

    reviewer_rows = [
        ReviewerStatusRead(
            id=r.id,
            reviewer_employee_id=r.reviewer_employee_id,
            name=people[r.reviewer_employee_id].full_name
            if r.reviewer_employee_id in people
            else "Unknown",
            status=r.status,
            submitted_at=r.submitted_at,
            decline_reason=r.decline_reason,
            last_reminded_at=r.last_reminded_at,
            reminder_count=r.reminder_count,
        )
        for r in reviewers
    ]
    reviewers_out: list[ReviewerStatusRead] | None = None
    named: list[ResponseRead] | None = None
    aggregate: FeedbackAggregateRead | None = None
    if role == "subject":
        view = engine.subject_view(
            req.employee_visibility.value, submitted, req.anonymity_threshold
        )
        if view == "named":
            reviewers_out = reviewer_rows
            named = _named_responses(reviewers, people, questions)
            aggregate = _aggregate_read(reviewers, questions) if submitted else None
            note = "This request shows each answer with the colleague's name."
        elif view == "summary":
            aggregate = _aggregate_read(reviewers, questions)
            note = (
                f"An anonymised summary of {submitted} answers. Names are not shown, and "
                "written answers are listed in no particular order."
            )
        elif view == "waiting":
            note = (
                f"{submitted} of {len(reviewers)} colleagues have answered. The anonymised "
                f"summary opens once {req.anonymity_threshold} have, so no answer can be "
                "traced back to one person."
            )
        else:
            note = (
                "The answers to this request are for your manager line. You can see how many "
                "colleagues have answered."
            )
    else:
        reviewers_out = reviewer_rows
        named = _named_responses(reviewers, people, questions)
        aggregate = _aggregate_read(reviewers, questions) if submitted else None
        note = {
            FeedbackVisibility.ANONYMISED: (
                "You see names. The person sees an anonymised summary once "
                f"{req.anonymity_threshold} colleagues have answered."
            ),
            FeedbackVisibility.NAMED: "You and the person both see each answer with its name.",
            FeedbackVisibility.MANAGER_ONLY: (
                "Only the manager line and HR read these answers; the person sees counts."
            ),
        }[req.employee_visibility]

    return FeedbackRequestDetail(
        **summary,
        message=req.message,
        questions=[FeedbackQuestion(**q) for q in questions],
        scale=FeedbackScale(**req.scale) if req.scale else None,
        anonymity_threshold=req.anonymity_threshold,
        reviewers=reviewers_out,
        responses_named=named,
        aggregate=aggregate,
        visibility_note=note,
        can_manage=_can_manage(req, role, user),
    )


def _by_relationship(reviewers: list[FeedbackReviewer], threshold: int) -> list[dict[str, Any]]:
    """Overall average per relationship, only for a group large enough to stay anonymous."""
    groups: dict[str, list[Decimal]] = {}
    for r in reviewers:
        if r.status is FeedbackReviewerStatus.SUBMITTED and r.overall_rating is not None:
            groups.setdefault(r.relation or "other", []).append(Decimal(str(r.overall_rating)))
    out = []
    for name in engine.RELATIONSHIPS:
        given = groups.get(name, [])
        if not given:
            continue
        shown = len(given) >= max(threshold, engine.ANONYMITY_FLOOR)
        out.append(
            {
                "relationship": name,
                "responses": len(given),
                "overall_average": (
                    str((sum(given) / len(given)).quantize(Decimal("0.01"))) if shown else None
                ),
                "withheld": not shown,
            }
        )
    return out


async def feedback_for_review(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    cycle_id: uuid.UUID,
) -> list[dict[str, Any]]:
    """The cycle's 360 requests about this person, as the appraisal screen shows them.

    The same visibility as the feedback page: the manager line and HR read
    names and answers; the person reads what their request's visibility allows;
    anybody else nothing. Group averages by relationship appear only where a
    group is large enough that no answer can be traced to one colleague.
    """
    requests = (
        await session.scalars(
            tenant_scoped(
                select(FeedbackRequest)
                .where(
                    FeedbackRequest.subject_employee_id == employee_id,
                    FeedbackRequest.appraisal_cycle_id == cycle_id,
                )
                .order_by(FeedbackRequest.created_at),
                FeedbackRequest,
                user,
            )
        )
    ).all()
    if not requests:
        return []
    line = await permissions.reporting_line(session, user)
    reviewers = await _reviewers(session, user, [r.id for r in requests])
    out: list[dict[str, Any]] = []
    for req in requests:
        role = await _viewer_role(session, user, req, line)
        if role is None:
            continue
        rows = reviewers[req.id]
        submitted = sum(1 for r in rows if r.status is FeedbackReviewerStatus.SUBMITTED)
        questions = list(req.questions or [])
        entry: dict[str, Any] = {
            "request_id": str(req.id),
            "asked": len(rows),
            "answered": submitted,
            "status": req.status.value,
            "aggregate": None,
            "by_relationship": [],
            "responses_named": None,
        }
        if role == "subject":
            view = engine.subject_view(
                req.employee_visibility.value, submitted, req.anonymity_threshold
            )
            if view in ("named", "summary") and submitted:
                entry["aggregate"] = _aggregate_read(rows, questions).model_dump(mode="json")
                entry["by_relationship"] = _by_relationship(rows, req.anonymity_threshold)
            if view == "named":
                people = await _employees(session, user, {r.reviewer_employee_id for r in rows})
                entry["responses_named"] = [
                    n.model_dump(mode="json") for n in _named_responses(rows, people, questions)
                ]
        elif submitted:
            people = await _employees(session, user, {r.reviewer_employee_id for r in rows})
            entry["aggregate"] = _aggregate_read(rows, questions).model_dump(mode="json")
            entry["by_relationship"] = _by_relationship(rows, req.anonymity_threshold)
            entry["responses_named"] = [
                n.model_dump(mode="json") for n in _named_responses(rows, people, questions)
            ]
        out.append(entry)
    return out


def _can_manage(req: FeedbackRequest, role: str | None, user: User) -> bool:
    """Remind and close: whoever asked, the manager line and HR."""
    if req.status is not FeedbackRequestStatus.OPEN:
        return False
    return role in {"manager", "hr", "requester"} or req.requested_by_user_id == user.id


async def remind(
    session: AsyncSession, user: User, request_id: uuid.UUID, *, request: Request | None = None
) -> FeedbackRemindResult:
    """Nudge every colleague who has not answered yet."""
    req = await _request(session, user, request_id)
    role = await _viewer_role(session, user, req)
    if role is None:
        raise _not_found()
    if not _can_manage(req, role, user):
        raise permissions.forbidden(
            "Only whoever asked, the manager line or HR can send reminders."
        )
    reviewers = (await _reviewers(session, user, [req.id]))[req.id]
    reminded = await _send_reminders(session, user, req, reviewers)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="feedback_request",
        entity_id=req.id,
        actor=user,
        changes={"reminded": reminded},
        summary=f"Reminded {reminded} colleague(s) about a feedback request",
        request=request,
    )
    await session.commit()
    return FeedbackRemindResult(reminded=reminded)


async def _send_reminders(
    session: AsyncSession,
    user: User,
    req: FeedbackRequest,
    reviewers: list[FeedbackReviewer],
) -> int:
    pending = [r for r in reviewers if r.status is FeedbackReviewerStatus.PENDING]
    if not pending:
        return 0
    logins = await notices.user_ids_for_employees(
        session, user, [r.reviewer_employee_id for r in pending]
    )
    subject = (await _employees(session, user, {req.subject_employee_id})).get(
        req.subject_employee_id
    )
    now = datetime.now(UTC)
    sent = 0
    for r in pending:
        uid = logins.get(r.reviewer_employee_id)
        if uid is None:
            continue
        r.last_reminded_at = now
        r.reminder_count += 1
        sent += 1
        await notify(
            session,
            tenant_id=user.tenant_id,
            event_key="performance.feedback.reminder",
            recipient_user_ids=[uid],
            payload={
                "subject_name": subject.full_name if subject else "a colleague",
                "due_on": f"{req.due_on:%d %b %Y}",
            },
            commit=False,
        )
    await session.flush()
    return sent


async def close_request(
    session: AsyncSession, user: User, request_id: uuid.UUID, *, request: Request | None = None
) -> FeedbackRequestDetail:
    """Stop collecting. Answers already given stay; pending colleagues drop off."""
    req = await _request(session, user, request_id)
    role = await _viewer_role(session, user, req)
    if role is None:
        raise _not_found()
    if not _can_manage(req, role, user):
        raise permissions.forbidden("Only whoever asked, the manager line or HR can close this.")
    req.status = FeedbackRequestStatus.CLOSED
    req.closed_at = datetime.now(UTC)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="feedback_request",
        entity_id=req.id,
        actor=user,
        changes={"status": {"from": "open", "to": "closed"}},
        summary="Closed a feedback request",
        request=request,
    )
    await session.commit()
    return await get_request(session, user, req.id)


# ── The reviewer's side ─────────────────────────────────────────────────────


async def _my_reviewer_row(
    session: AsyncSession, user: User, reviewer_id: uuid.UUID
) -> tuple[FeedbackReviewer, FeedbackRequest]:
    """The caller's own reviewer row, or 404. Nobody answers for somebody else."""
    row = await session.scalar(
        tenant_scoped(
            select(FeedbackReviewer).where(FeedbackReviewer.id == reviewer_id),
            FeedbackReviewer,
            user,
        )
    )
    if row is None or user.employee_id is None or row.reviewer_employee_id != user.employee_id:
        raise _not_found("Feedback request not found")
    return row, await _request(session, user, row.request_id)


def _visibility_statement(req: FeedbackRequest, subject_name: str) -> str:
    if req.employee_visibility is FeedbackVisibility.NAMED:
        return f"{subject_name}, their manager line and HR will see your answer with your name."
    if req.employee_visibility is FeedbackVisibility.MANAGER_ONLY:
        return (
            f"{subject_name}'s manager line and HR will see your answer with your name. "
            f"{subject_name} will not see it."
        )
    return (
        f"{subject_name}'s manager line and HR will see your answer with your name. "
        f"{subject_name} sees only an anonymised summary, and only once at least "
        f"{req.anonymity_threshold} colleagues have answered."
    )


async def inbox(session: AsyncSession, user: User) -> list[FeedbackInboxItem]:
    """Feedback the caller has been asked to give — pending first, then answered."""
    if user.employee_id is None:
        return []
    rows = (
        await session.execute(
            tenant_scoped(
                select(FeedbackReviewer, FeedbackRequest)
                .join(FeedbackRequest, FeedbackRequest.id == FeedbackReviewer.request_id)
                .where(FeedbackReviewer.reviewer_employee_id == user.employee_id)
                .order_by(FeedbackRequest.due_on),
                FeedbackReviewer,
                user,
            )
        )
    ).all()
    people = await _employees(session, user, {req.subject_employee_id for _, req in rows})
    user_names = await _user_names(
        session, user, {req.requested_by_user_id for _, req in rows if req.requested_by_user_id}
    )
    cycles = await _cycle_names(
        session, user, {req.appraisal_cycle_id for _, req in rows if req.appraisal_cycle_id}
    )
    items = [
        FeedbackInboxItem(
            reviewer_id=r.id,
            request_id=req.id,
            subject_name=people[req.subject_employee_id].full_name
            if req.subject_employee_id in people
            else "Unknown",
            requested_by_name=user_names.get(req.requested_by_user_id)
            if req.requested_by_user_id
            else None,
            cycle_name=cycles.get(req.appraisal_cycle_id) if req.appraisal_cycle_id else None,
            due_on=req.due_on,
            status=r.status,
            request_status=req.status,
            submitted_at=r.submitted_at,
            created_at=r.created_at,
        )
        for r, req in rows
    ]
    waiting = [
        i
        for i in items
        if i.status is FeedbackReviewerStatus.PENDING
        and i.request_status is FeedbackRequestStatus.OPEN
    ]
    return waiting + [i for i in items if i not in waiting]


async def inbox_items(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """Feedback waiting on the caller, for the unified approvals inbox.

    Only rows the caller can act on: their own, pending, on an open request.
    The person the feedback is about is never a reviewer on it, so nobody is
    shown their own request.
    """
    return [
        {
            "id": item.reviewer_id,
            "title": f"Feedback on {item.subject_name}",
            "subject_name": item.subject_name,
            "detail": (f"Asked by {item.requested_by_name}" if item.requested_by_name else None),
            "submitted_at": item.created_at,
            "due_on": item.due_on,
        }
        for item in await inbox(session, user)
        if item.status is FeedbackReviewerStatus.PENDING
        and item.request_status is FeedbackRequestStatus.OPEN
    ]


async def get_form(session: AsyncSession, user: User, reviewer_id: uuid.UUID) -> FeedbackForm:
    row, req = await _my_reviewer_row(session, user, reviewer_id)
    people = await _employees(session, user, {req.subject_employee_id})
    subject_name = (
        people[req.subject_employee_id].full_name if req.subject_employee_id in people else ""
    )
    user_names = await _user_names(
        session, user, {req.requested_by_user_id} if req.requested_by_user_id else set()
    )
    cycles = await _cycle_names(
        session, user, {req.appraisal_cycle_id} if req.appraisal_cycle_id else set()
    )
    return FeedbackForm(
        reviewer_id=row.id,
        request_id=req.id,
        subject_name=subject_name,
        requested_by_name=user_names.get(req.requested_by_user_id)
        if req.requested_by_user_id
        else None,
        cycle_name=cycles.get(req.appraisal_cycle_id) if req.appraisal_cycle_id else None,
        message=req.message,
        due_on=req.due_on,
        questions=[FeedbackQuestion(**q) for q in (req.questions or [])],
        scale=FeedbackScale(**req.scale) if req.scale else None,
        employee_visibility=req.employee_visibility,
        visibility_statement=_visibility_statement(req, subject_name),
        status=row.status,
        request_status=req.status,
        competency_ratings=[
            CompetencyAnswerIn(
                competency_id=uuid.UUID(str(item["competency_id"])),
                rating=_d(item.get("rating")),
                comment=item.get("comment"),
            )
            for item in (row.competency_ratings or [])
        ],
        strengths=row.strengths,
        improvements=row.improvements,
        overall_rating=_d(row.overall_rating),
        submitted_at=row.submitted_at,
    )


def _check_ratings(req: FeedbackRequest, answers: list[CompetencyAnswerIn]) -> list[dict]:
    known = {str(q["competency_id"]) for q in (req.questions or [])}
    low = _d((req.scale or {}).get("min"))
    high = _d((req.scale or {}).get("max"))
    out: list[dict] = []
    seen: set[str] = set()
    for answer in answers:
        key = str(answer.competency_id)
        if key not in known:
            raise _unprocessable("That competency is not part of this request.")
        if key in seen:
            raise _unprocessable("Each competency is answered once.")
        seen.add(key)
        if (
            answer.rating is not None
            and low is not None
            and high is not None
            and not low <= answer.rating <= high
        ):
            raise _unprocessable(f"Ratings on this scale run from {low} to {high}.")
        comment = (answer.comment or "").strip() or None
        if answer.rating is None and comment is None:
            continue
        out.append(
            {
                "competency_id": key,
                "rating": str(answer.rating) if answer.rating is not None else None,
                "comment": comment,
            }
        )
    return out


async def submit(
    session: AsyncSession,
    user: User,
    reviewer_id: uuid.UUID,
    payload: FeedbackResponseSubmit,
    *,
    request: Request | None = None,
) -> FeedbackForm:
    """Answer a request. Once: an answer given is the answer."""
    row, req = await _my_reviewer_row(session, user, reviewer_id)
    if req.status is not FeedbackRequestStatus.OPEN:
        raise _conflict("This request has been closed.")
    if row.status is not FeedbackReviewerStatus.PENDING:
        raise _conflict(f"You have already {row.status.value} this request.")
    row.competency_ratings = _check_ratings(req, payload.competency_ratings)
    row.strengths = (payload.strengths or "").strip() or None
    row.improvements = (payload.improvements or "").strip() or None
    row.overall_rating = payload.overall_rating
    row.status = FeedbackReviewerStatus.SUBMITTED
    row.submitted_at = datetime.now(UTC)
    await session.flush()
    # What they wrote is not copied into the audit trail — only that they did.
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="feedback_reviewer",
        entity_id=row.id,
        actor=user,
        changes={"request_id": str(req.id), "status": "submitted"},
        summary="Submitted 360 feedback",
        request=request,
    )

    reviewers = (await _reviewers(session, user, [req.id]))[req.id]
    submitted = sum(r.status is FeedbackReviewerStatus.SUBMITTED for r in reviewers)
    subject = (await _employees(session, user, {req.subject_employee_id})).get(
        req.subject_employee_id
    )
    subject_name = subject.full_name if subject else "a colleague"
    subject_login = (
        await notices.user_ids_for_employees(session, user, [req.subject_employee_id])
    ).get(req.subject_employee_id)
    # Whoever asked hears who answered — unless whoever asked is the person the
    # feedback is about, in which case a name per answer would undo anonymity.
    if (
        req.requested_by_user_id is not None
        and req.requested_by_user_id != subject_login
        and req.requested_by_user_id != user.id
    ):
        await notify(
            session,
            tenant_id=user.tenant_id,
            event_key="performance.feedback.submitted",
            recipient_user_ids=[req.requested_by_user_id],
            payload={
                "reviewer_name": user.full_name,
                "subject_name": subject_name,
                "responses": submitted,
                "total": len(reviewers),
            },
            commit=False,
        )
    if (
        subject_login is not None
        and req.employee_visibility is FeedbackVisibility.ANONYMISED
        and submitted == req.anonymity_threshold
    ):
        await notify(
            session,
            tenant_id=user.tenant_id,
            event_key="performance.feedback.summary_ready",
            recipient_user_ids=[subject_login],
            payload={"responses": submitted},
            commit=False,
        )
    await session.commit()
    return await get_form(session, user, row.id)


async def decline(
    session: AsyncSession,
    user: User,
    reviewer_id: uuid.UUID,
    payload: FeedbackDecline,
    *,
    request: Request | None = None,
) -> FeedbackForm:
    """Say no — "I haven't worked with them" is a real answer, better than silence."""
    row, req = await _my_reviewer_row(session, user, reviewer_id)
    if req.status is not FeedbackRequestStatus.OPEN:
        raise _conflict("This request has been closed.")
    if row.status is not FeedbackReviewerStatus.PENDING:
        raise _conflict(f"You have already {row.status.value} this request.")
    row.status = FeedbackReviewerStatus.DECLINED
    row.declined_at = datetime.now(UTC)
    row.decline_reason = (payload.reason or "").strip() or None
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="feedback_reviewer",
        entity_id=row.id,
        actor=user,
        changes={"request_id": str(req.id), "status": "declined"},
        summary="Declined a 360 feedback request",
        request=request,
    )
    await session.commit()
    return await get_form(session, user, row.id)


# ── The scheduled reminder ──────────────────────────────────────────────────


async def send_due_reminders(session: AsyncSession, actor: User, *, on: date) -> dict[str, int]:
    """Remind colleagues whose feedback is due within two days, or overdue.

    At most once every two days per colleague, and not past two weeks overdue —
    a request that stale needs its owner to close it, not a fifth email.
    """
    rows = (
        await session.scalars(
            tenant_scoped(
                select(FeedbackRequest).where(
                    FeedbackRequest.status == FeedbackRequestStatus.OPEN,
                    FeedbackRequest.due_on <= on + timedelta(days=REMIND_WITHIN_DAYS),
                    FeedbackRequest.due_on >= on - timedelta(days=STOP_REMINDING_AFTER_DAYS),
                ),
                FeedbackRequest,
                actor,
            )
        )
    ).all()
    cutoff = datetime.now(UTC) - REMINDER_GAP
    reviewers = await _reviewers(session, actor, [r.id for r in rows])
    reminded = 0
    for req in rows:
        due = [
            r
            for r in reviewers.get(req.id, [])
            if r.last_reminded_at is None or r.last_reminded_at < cutoff
        ]
        reminded += await _send_reminders(session, actor, req, due)
    await session.commit()
    return {"requests": len(rows), "reminded": reminded}


# ── Private feedback ────────────────────────────────────────────────────────


async def give_private(
    session: AsyncSession,
    user: User,
    payload: PrivateFeedbackCreate,
    *,
    request: Request | None = None,
) -> PrivateFeedbackRead:
    """A private note to a colleague. The writer is always the caller."""
    me = permissions.own_employee_id(user)
    if payload.to_employee_id == me:
        raise _unprocessable("Feedback goes to somebody else.")
    people = await _employees(session, user, {me, payload.to_employee_id})
    to = people.get(payload.to_employee_id)
    if to is None or to.deleted_at is not None or to.status is EmploymentStatus.EXITED:
        raise _not_found("Employee not found")
    row = PrivateFeedback(
        tenant_id=user.tenant_id,
        from_employee_id=me,
        to_employee_id=to.id,
        kind=payload.kind,
        body=payload.body.strip(),
    )
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="private_feedback",
        entity_id=row.id,
        actor=user,
        changes={"to_employee_id": str(to.id), "kind": payload.kind.value},
        summary=f"Sent private feedback to {to.full_name}",
        request=request,
    )
    logins = await notices.user_ids_for_employees(session, user, [to.id])
    await notify(
        session,
        tenant_id=user.tenant_id,
        event_key="performance.feedback.private_received",
        recipient_user_ids=list(logins.values()),
        payload={"from_name": user.full_name},
        commit=False,
    )
    await session.commit()
    await session.refresh(row)
    return PrivateFeedbackRead(
        id=row.id,
        from_employee_id=me,
        from_name=people[me].full_name if me in people else user.full_name,
        to_employee_id=to.id,
        to_name=to.full_name,
        kind=row.kind,
        body=row.body,
        created_at=row.created_at,
    )


async def list_private(
    session: AsyncSession, user: User, *, scope: str = "received"
) -> list[PrivateFeedbackRead]:
    """Received by the caller, given by the caller, or received by their reporting line."""
    if user.employee_id is None:
        return []
    stmt = tenant_scoped(select(PrivateFeedback), PrivateFeedback, user).order_by(
        PrivateFeedback.created_at.desc()
    )
    if scope == "received":
        stmt = stmt.where(PrivateFeedback.to_employee_id == user.employee_id)
    elif scope == "given":
        stmt = stmt.where(PrivateFeedback.from_employee_id == user.employee_id)
    else:
        line = await permissions.reporting_line(session, user)
        if not line:
            return []
        stmt = stmt.where(PrivateFeedback.to_employee_id.in_(line))
    rows = list((await session.scalars(stmt.limit(300))).all())
    people = await _employees(
        session, user, {r.from_employee_id for r in rows} | {r.to_employee_id for r in rows}
    )
    return [
        PrivateFeedbackRead(
            id=r.id,
            from_employee_id=r.from_employee_id,
            from_name=people[r.from_employee_id].full_name
            if r.from_employee_id in people
            else "Unknown",
            to_employee_id=r.to_employee_id,
            to_name=people[r.to_employee_id].full_name if r.to_employee_id in people else "Unknown",
            kind=r.kind,
            body=r.body,
            created_at=r.created_at,
        )
        for r in rows
    ]
