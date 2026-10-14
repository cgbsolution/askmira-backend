"""Emails to candidates: queued on hiring events, delivered by a job.

A candidate is not a user, so the notification pipeline — which addresses
users — cannot reach them. Hiring events queue a `CandidateMessage` instead,
worded from the tenant's own template for the event (editable like any other
notification template) or the built-in wording, and the
`hiring.candidate_messages` job delivers it through the tenant's mail relay
with the same retry rules as every other email.

Three rules shape this module:

- **A link that acts as the candidate is never readable at rest.** Portal and
  self-schedule links are encrypted until sent and erased after; the stored
  body shows a placeholder.
- **A rejection waits.** It is held for `REJECTION_HOLD` and dropped if the
  candidate has been moved back by then, because a mis-click must not reach
  somebody's inbox as a no.
- **Recruiters see exactly what went.** Every message, automatic or written,
  is listed on the candidate with its status.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.candidate_comms import CandidateMessage, CandidateMessageStatus
from app.models.hiring import Candidate, CandidateStage, Requisition
from app.models.interview import Interview, InterviewMode
from app.models.tenant import Tenant
from app.models.user import User
from app.services import audit
from app.services.notifications.adapters import (
    OutboundMessage,
    PermanentFailure,
    TransientFailure,
)
from app.services.notifications.dispatch import (
    email_adapter_for,
    email_wording,
    register_event,
    render,
)

__all__ = [
    "APPLICATION_RECEIVED",
    "INTERVIEW_CANCELLED",
    "INTERVIEW_INVITE",
    "INTERVIEW_RESCHEDULED",
    "INTERVIEW_SCHEDULED",
    "LINK_SHOWN",
    "MESSAGE",
    "OFFER_RELEASED",
    "PORTAL_LINK",
    "REJECTED",
    "cancel_message",
    "cancel_pending_rejections",
    "deliver_due",
    "job_title",
    "list_messages",
    "on_interview",
    "on_portal_link",
    "on_rejected",
    "on_self_schedule_link",
    "portal_url",
    "queue",
    "self_schedule_url",
    "send_message",
]

APPLICATION_RECEIVED = "hiring.candidate.application_received"
INTERVIEW_INVITE = "hiring.candidate.interview_invite"
INTERVIEW_SCHEDULED = "hiring.candidate.interview_scheduled"
INTERVIEW_RESCHEDULED = "hiring.candidate.interview_rescheduled"
INTERVIEW_CANCELLED = "hiring.candidate.interview_cancelled"
REJECTED = "hiring.candidate.rejected"
OFFER_RELEASED = "hiring.candidate.offer_released"
PORTAL_LINK = "hiring.candidate.portal_link"
MESSAGE = "hiring.candidate.message"

#: What the stored body says where the private link goes.
LINK_SHOWN = "[private link, sent to the candidate only]"
REJECTION_HOLD = timedelta(hours=24)
MAX_ATTEMPTS = 5
BATCH = 200

_PRIVATE = (
    "Keep this link to yourself: it opens your application without a password."
)

register_event(
    APPLICATION_RECEIVED,
    "We have your application for {job_title}",
    "Hello {first_name},\n\nThank you for applying for {job_title} at {organisation}. "
    "We will be in touch about the next step.\n\nYou can follow your application, upload "
    "documents, and see any interview or offer here, with no account needed:\n{link}\n\n"
    + _PRIVATE,
)
register_event(
    INTERVIEW_INVITE,
    "Choose a time for your {stage} interview",
    "Hello {first_name},\n\n{organisation} would like to interview you for {job_title}. "
    "Pick the time that suits you from the slots offered:\n{link}\n\nThe link works until "
    "{expires}.",
)
register_event(
    INTERVIEW_SCHEDULED,
    "Your {stage} interview with {organisation}",
    "Hello {first_name},\n\nYour {stage} interview for {job_title} is booked for {when}.\n"
    "{where}\n\nIf you need a different time, ask through your application link or reply "
    "to this email.",
)
register_event(
    INTERVIEW_RESCHEDULED,
    "Your {stage} interview has moved",
    "Hello {first_name},\n\nYour {stage} interview for {job_title} is now on {when}.\n"
    "{where}\n\nSorry for the change.",
)
register_event(
    INTERVIEW_CANCELLED,
    "Your {stage} interview has been cancelled",
    "Hello {first_name},\n\nYour {stage} interview for {job_title}, booked for {when}, has "
    "been cancelled. We will be in touch about what happens next.",
)
register_event(
    REJECTED,
    "Your application for {job_title}",
    "Hello {first_name},\n\nThank you for your time and your interest in {job_title} at "
    "{organisation}. After careful consideration, we will not be taking your application "
    "further.\n\nWe appreciate the effort you put in, and wish you well.",
)
register_event(
    OFFER_RELEASED,
    "Your offer from {organisation}",
    "Hello {first_name},\n\nWe are pleased to offer you the role of {job_title}. Your offer "
    "letter is ready to read, and to accept or decline, here:\n{link}\n\nPlease respond by "
    "{valid_until}. " + _PRIVATE,
)
register_event(
    PORTAL_LINK,
    "Your application link from {organisation}",
    "Hello {first_name},\n\nHere is your link to follow your application for {job_title}:\n"
    "{link}\n\n" + _PRIVATE,
)
register_event(MESSAGE, "{subject}", "{message}")


def _web_base() -> str:
    base = settings.WEB_APP_URL or (settings.CORS_ORIGINS[0] if settings.CORS_ORIGINS else "")
    return base.rstrip("/")


def portal_url(token: str) -> str:
    return f"{_web_base()}/candidate/{token}"


def self_schedule_url(token: str) -> str:
    return f"{_web_base()}/interview/schedule/{token}"


async def queue(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    candidate: Candidate,
    event_key: str,
    payload: dict[str, Any] | None = None,
    secret_link: str | None = None,
    send_after: datetime | None = None,
    sent_by_user_id: uuid.UUID | None = None,
) -> CandidateMessage | None:
    """Word and queue one email. Returns None when the event has no wording at all."""
    wording = await email_wording(session, tenant_id, event_key)
    if wording is None:
        return None
    tenant = await session.get(Tenant, tenant_id)
    values: dict[str, Any] = {
        "candidate_name": candidate.full_name,
        "first_name": candidate.full_name.split()[0] if candidate.full_name.strip() else "",
        "organisation": tenant.name if tenant else "",
        "where": "",
        **(payload or {}),
    }
    if secret_link is not None:
        values["link"] = LINK_SHOWN
    subject = render(wording[0], values).strip()[:500]
    body = render(wording[1], values)
    if secret_link is not None and LINK_SHOWN not in body:
        # A tenant's own template may have dropped {link}; the link still has to arrive.
        body = f"{body}\n\n{LINK_SHOWN}"
    message = CandidateMessage(
        tenant_id=tenant_id,
        candidate_id=candidate.id,
        event_key=event_key,
        to_address=candidate.email,
        subject=subject or "(no subject)",
        body=body,
        secret_link_encrypted=(
            encrypt_secret(secret_link, tenant_id) if secret_link is not None else None
        ),
        status=CandidateMessageStatus.QUEUED,
        next_attempt_at=send_after or datetime.now(UTC),
        sent_by_user_id=sent_by_user_id,
    )
    session.add(message)
    return message


# ── What each hiring event says ─────────────────────────────────────────────


async def job_title(session: AsyncSession, user: Any, candidate: Candidate) -> str:
    title = await session.scalar(
        tenant_scoped(
            select(Requisition.title).where(Requisition.id == candidate.requisition_id),
            Requisition,
            user,
        )
    )
    return title or "the role"


def _when(moment: datetime, timezone_name: str) -> str:
    from app.services.interviews import availability

    local = availability.render_for(moment, timezone_name)
    return f"{local:%A %d %B %Y at %I:%M %p} ({timezone_name})"


def _where(interview: Interview) -> str:
    place = (interview.location_or_link or "").strip()
    if interview.mode is InterviewMode.VIDEO:
        return f"Join here: {place}" if place else "It is a video call; the joining link follows."
    if interview.mode is InterviewMode.PHONE:
        return f"We will call you{f' ({place})' if place else ''} on the number you gave us."
    if interview.mode is InterviewMode.IN_PERSON:
        return f"Where: {place}" if place else "In person; the address follows."
    return f"Where: {place}" if place else ""


async def on_interview(
    session: AsyncSession,
    user: Any,
    *,
    event_key: str,
    interview: Interview,
    stage_name: str,
    candidate: Candidate,
    when: datetime | None = None,
) -> CandidateMessage | None:
    """Tell the candidate an interview was booked, moved or cancelled."""
    return await queue(
        session,
        tenant_id=user.tenant_id,
        candidate=candidate,
        event_key=event_key,
        payload={
            "job_title": await job_title(session, user, candidate),
            "stage": stage_name,
            "when": _when(when or interview.scheduled_start, interview.candidate_timezone),
            "where": _where(interview),
        },
    )


async def on_self_schedule_link(
    session: AsyncSession,
    user: Any,
    *,
    candidate: Candidate,
    stage_name: str,
    token: str,
    expires_at: datetime,
    timezone_name: str,
) -> CandidateMessage | None:
    return await queue(
        session,
        tenant_id=user.tenant_id,
        candidate=candidate,
        event_key=INTERVIEW_INVITE,
        payload={
            "job_title": await job_title(session, user, candidate),
            "stage": stage_name,
            "expires": _when(expires_at, timezone_name),
        },
        secret_link=self_schedule_url(token),
    )


async def on_rejected(
    session: AsyncSession, user: Any, candidate: Candidate
) -> CandidateMessage | None:
    """Held for `REJECTION_HOLD`, and dropped if the candidate is moved back meanwhile."""
    return await queue(
        session,
        tenant_id=user.tenant_id,
        candidate=candidate,
        event_key=REJECTED,
        payload={"job_title": await job_title(session, user, candidate)},
        send_after=datetime.now(UTC) + REJECTION_HOLD,
    )


async def on_portal_link(
    session: AsyncSession, user: Any, *, candidate: Candidate, token: str, event_key: str,
    payload: dict[str, Any] | None = None,
) -> CandidateMessage | None:
    """Any email whose point is the candidate's portal link."""
    return await queue(
        session,
        tenant_id=user.tenant_id,
        candidate=candidate,
        event_key=event_key,
        payload={"job_title": await job_title(session, user, candidate), **(payload or {})},
        secret_link=portal_url(token),
    )


async def cancel_pending_rejections(
    session: AsyncSession, user: User, candidate_id: uuid.UUID
) -> int:
    """Withdraw a rejection not yet sent — the candidate has been moved back."""
    rows = (
        await session.scalars(
            tenant_scoped(
                select(CandidateMessage).where(
                    CandidateMessage.candidate_id == candidate_id,
                    CandidateMessage.event_key == REJECTED,
                    CandidateMessage.status == CandidateMessageStatus.QUEUED,
                ),
                CandidateMessage,
                user,
            )
        )
    ).all()
    for row in rows:
        _withdraw(row, "The candidate was moved out of rejected before this was sent.")
    return len(rows)


def _withdraw(row: CandidateMessage, reason: str) -> None:
    row.status = CandidateMessageStatus.CANCELLED
    row.failure_reason = reason
    row.next_attempt_at = None
    row.secret_link_encrypted = None


def _backoff(attempt: int) -> timedelta:
    return timedelta(minutes=min(2 ** (attempt - 1), 60))


async def deliver_due(
    session: AsyncSession, actor: User, *, now: datetime | None = None
) -> dict[str, int]:
    """Send every queued message that is due. Scheduler-callable; never raises."""
    now = now or datetime.now(UTC)
    due = (
        await session.scalars(
            tenant_scoped(
                select(CandidateMessage)
                .where(
                    CandidateMessage.status == CandidateMessageStatus.QUEUED,
                    CandidateMessage.next_attempt_at <= now,
                )
                .order_by(CandidateMessage.next_attempt_at)
                .limit(BATCH)
                .with_for_update(skip_locked=True),
                CandidateMessage,
                actor,
            )
        )
    ).all()
    outcome = {"sent": 0, "retrying": 0, "failed": 0, "withdrawn": 0}
    if not due:
        return outcome

    candidates = {
        row.id: row
        for row in (
            await session.scalars(
                tenant_scoped(
                    select(Candidate).where(Candidate.id.in_({m.candidate_id for m in due})),
                    Candidate,
                    actor,
                )
            )
        ).all()
    }
    try:
        adapter, _relay = await email_adapter_for(session, actor.tenant_id)
    except Exception as exc:
        for message in due:
            message.status = CandidateMessageStatus.FAILED
            message.failure_reason = f"The configured mail relay could not be used: {exc}"
            outcome["failed"] += 1
        await session.commit()
        return outcome

    for message in due:
        candidate = candidates.get(message.candidate_id)
        if candidate is None:
            _withdraw(message, "The candidate no longer exists.")
            outcome["withdrawn"] += 1
            continue
        if message.event_key == REJECTED and candidate.stage is not CandidateStage.REJECTED:
            _withdraw(message, "The candidate is no longer rejected.")
            outcome["withdrawn"] += 1
            continue

        body = message.body
        if message.secret_link_encrypted:
            link = decrypt_secret(message.secret_link_encrypted, actor.tenant_id) or ""
            body = body.replace(LINK_SHOWN, link)
        message.attempt_count += 1
        try:
            result = await adapter.send(
                OutboundMessage(
                    recipient_name=candidate.full_name,
                    recipient_email=message.to_address,
                    recipient_phone=None,
                    subject=message.subject,
                    body=body,
                    event_key=message.event_key,
                )
            )
        except PermanentFailure as exc:
            message.status = CandidateMessageStatus.FAILED
            message.failure_reason = f"Permanent: {exc}"
            message.next_attempt_at = None
            message.secret_link_encrypted = None
            outcome["failed"] += 1
            continue
        except TransientFailure as exc:
            if message.attempt_count >= MAX_ATTEMPTS:
                message.status = CandidateMessageStatus.DEAD_LETTER
                message.failure_reason = f"Gave up after {message.attempt_count} attempts: {exc}"
                message.next_attempt_at = None
                message.secret_link_encrypted = None
                outcome["failed"] += 1
            else:
                message.failure_reason = f"Transient: {exc}"
                message.next_attempt_at = now + _backoff(message.attempt_count)
                outcome["retrying"] += 1
            continue
        except Exception as exc:
            message.status = CandidateMessageStatus.FAILED
            message.failure_reason = f"Adapter error: {exc}"
            message.next_attempt_at = None
            outcome["failed"] += 1
            continue

        message.status = CandidateMessageStatus.SENT
        message.sent_at = now
        message.provider_message_id = result.provider_message_id
        message.failure_reason = None
        message.next_attempt_at = None
        message.secret_link_encrypted = None
        outcome["sent"] += 1

    await session.commit()
    return outcome


# ── Recruiter-facing: the log, a written message, a withdrawal ──────────────


def _read(row: CandidateMessage, names: dict[uuid.UUID, str]) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "event_key": row.event_key,
        "to_address": row.to_address,
        "subject": row.subject,
        "body": row.body,
        "status": row.status.value,
        "attempt_count": row.attempt_count,
        "scheduled_for": row.next_attempt_at.isoformat() if row.next_attempt_at else None,
        "sent_at": row.sent_at.isoformat() if row.sent_at else None,
        "failure_reason": row.failure_reason,
        "sent_by": names.get(row.sent_by_user_id) if row.sent_by_user_id else None,
        "created_at": row.created_at.isoformat(),
    }


async def _candidate(session: AsyncSession, user: User, candidate_id: uuid.UUID) -> Candidate:
    from app.services.hiring import visibility

    candidate, _requisition = await visibility.load_candidate(session, user, candidate_id)
    return candidate


async def list_messages(
    session: AsyncSession, user: User, candidate_id: uuid.UUID
) -> list[dict[str, Any]]:
    await _candidate(session, user, candidate_id)
    rows = (
        await session.scalars(
            tenant_scoped(
                select(CandidateMessage)
                .where(CandidateMessage.candidate_id == candidate_id)
                .order_by(CandidateMessage.created_at.desc()),
                CandidateMessage,
                user,
            )
        )
    ).all()
    author_ids = {r.sent_by_user_id for r in rows if r.sent_by_user_id}
    names = {
        u.id: u.full_name
        for u in (
            await session.scalars(
                tenant_scoped(select(User).where(User.id.in_(author_ids)), User, user)
            )
        ).all()
    } if author_ids else {}
    return [_read(row, names) for row in rows]


async def send_message(
    session: AsyncSession,
    user: User,
    *,
    candidate_id: uuid.UUID,
    subject: str,
    message: str,
    include_portal_link: bool,
    request: Request | None = None,
) -> dict[str, Any]:
    """A recruiter's own email to a candidate, logged with everything else sent."""
    from app.services.hiring import careers

    subject, message = subject.strip(), message.strip()
    if not subject or not message:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A message needs a subject and some text.",
        )
    candidate = await _candidate(session, user, candidate_id)
    link = None
    if include_portal_link:
        _row, token = await careers.issue_portal_token(session, user, candidate_id=candidate.id)
        link = portal_url(token)
    row = await queue(
        session,
        tenant_id=user.tenant_id,
        candidate=candidate,
        event_key=MESSAGE,
        payload={"subject": subject, "message": message},
        secret_link=link,
        sent_by_user_id=user.id,
    )
    assert row is not None  # MESSAGE always has built-in wording
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="candidate_message",
        entity_id=row.id,
        summary=f"Email to candidate queued: {subject}",
        request=request,
    )
    return _read(row, {user.id: user.full_name})


async def cancel_message(
    session: AsyncSession, user: User, message_id: uuid.UUID, *, request: Request | None = None
) -> dict[str, Any]:
    row = await session.scalar(
        tenant_scoped(
            select(CandidateMessage).where(CandidateMessage.id == message_id),
            CandidateMessage,
            user,
        )
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    await _candidate(session, user, row.candidate_id)
    if row.status is not CandidateMessageStatus.QUEUED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only a message that has not gone yet can be withdrawn.",
        )
    _withdraw(row, f"Withdrawn by {user.full_name}.")
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="candidate_message",
        entity_id=row.id,
        summary=f"Queued email to candidate withdrawn: {row.subject}",
        request=request,
    )
    return _read(row, {})
