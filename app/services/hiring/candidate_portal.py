"""The public careers pages and the candidate portal, as the web pages consume them.

Everything here is reached without a login. The tenant comes from the careers
slug or the portal token — never from the request body — and every query below
filters on that tenant explicitly.

What a candidate gets through their portal token: their own applications (in
coarse statuses), their upcoming interviews with the join link and preparation
notes, their uploaded documents, and a way to ask for a different time. Nothing
about other candidates, the internal pipeline stage, scores or interviewers.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_filter
from app.models.audit import AuditAction
from app.models.hiring import Candidate, Requisition
from app.models.interview import Interview, InterviewStage, InterviewStatus
from app.models.jobboard import CandidateDocument, CareersPageConfig
from app.models.org import Department, Location
from app.models.tenant import Tenant
from app.models.user import User
from app.services import audit
from app.services.hiring import careers, ingestion, plain_text, resume_files
from app.services.interviews import availability
from app.services.notifications import dispatch
from app.services.storage.sniff import ALLOWED_TYPES

__all__ = [
    "DOCUMENT_CATEGORIES",
    "offer_letter",
    "portal_offers",
    "portal_overview",
    "public_job",
    "public_jobs",
    "request_reschedule",
    "respond_to_offer",
    "upload_document",
]

#: What a candidate may upload before joining. A fixed list rather than free
#: text, so HR can find "PAN card" without reading thirty spellings of it.
DOCUMENT_CATEGORIES: tuple[str, ...] = (
    "identity_proof",
    "address_proof",
    "pan_card",
    "education_certificate",
    "previous_employment",
    "relieving_letter",
    "payslip",
    "photograph",
    "other",
)

#: A reschedule request this close to the start is a phone call, not a form.
_RESCHEDULE_CUTOFF = timedelta(hours=2)


async def public_jobs(session: AsyncSession, slug: str, **filters: Any) -> dict[str, Any]:
    """The careers page with names for departments and locations, and text-only copy."""
    payload = await careers.public_job_list(session, slug, **filters)
    config = await session.scalar(select(CareersPageConfig).where(CareersPageConfig.slug == slug))
    tenant_id = config.tenant_id if config else None

    department_ids = {j["department_id"] for j in payload["jobs"] if j["department_id"]}
    location_ids = {j["location_id"] for j in payload["jobs"] if j["location_id"]}
    departments = {
        str(d.id): d.name
        for d in (
            await session.scalars(
                select(Department).where(
                    tenant_filter(Department, tenant_id),
                    Department.id.in_([uuid.UUID(i) for i in department_ids]),
                )
            )
        ).all()
    } if department_ids else {}
    locations = {
        str(loc.id): loc.name
        for loc in (
            await session.scalars(
                select(Location).where(
                    tenant_filter(Location, tenant_id),
                    Location.id.in_([uuid.UUID(i) for i in location_ids]),
                )
            )
        ).all()
    } if location_ids else {}

    for job in payload["jobs"]:
        job["department"] = departments.get(job["department_id"] or "")
        job["location"] = locations.get(job["location_id"] or "")
    branding = payload["branding"]
    branding["about_paragraphs"] = plain_text.html_to_paragraphs(branding.pop("about_html", None))
    payload["privacy_notice_paragraphs"] = plain_text.html_to_paragraphs(
        payload.pop("privacy_notice_html", None)
    )
    payload["filters"] = {
        "departments": [
            {"id": k, "name": v} for k, v in sorted(departments.items(), key=lambda i: i[1])
        ],
        "locations": [
            {"id": k, "name": v} for k, v in sorted(locations.items(), key=lambda i: i[1])
        ],
    }
    return payload


async def public_job(session: AsyncSession, slug: str, code: str) -> dict[str, Any]:
    """One job with text-only description and notice, plus the JSON-LD as data."""
    payload = await careers.public_job_detail(session, slug, code)
    payload["job"]["description_paragraphs"] = plain_text.html_to_paragraphs(
        payload["job"].pop("description_html", None)
    )
    payload["privacy_notice_paragraphs"] = plain_text.html_to_paragraphs(
        payload.pop("privacy_notice_html", None)
    )
    config = await session.scalar(select(CareersPageConfig).where(CareersPageConfig.slug == slug))
    tenant = await session.get(Tenant, config.tenant_id) if config else None
    payload["organisation"] = tenant.name if tenant else ""
    payload["document_rules"] = {
        "resume_types": ["PDF"],
        "resume_max_mb": resume_files.RESUME_MAX_BYTES // (1024 * 1024),
    }
    return payload


async def portal_overview(session: AsyncSession, token: str) -> dict[str, Any]:
    """The candidate's own status page: applications, interviews, documents."""
    base = await careers.portal_view(session, token)
    row = await careers.resolve_portal_token(session, token)
    now = datetime.now(UTC)
    tenant = await session.get(Tenant, row.tenant_id)

    rows = (
        await session.execute(
            select(Interview, InterviewStage)
            .join(InterviewStage, InterviewStage.id == Interview.stage_id)
            .where(
                tenant_filter(Interview, row.tenant_id),
                tenant_filter(InterviewStage, row.tenant_id),
                Interview.candidate_id == row.candidate_id,
                Interview.status.in_((InterviewStatus.SCHEDULED, InterviewStatus.RESCHEDULED)),
                Interview.scheduled_end >= now,
            )
            .order_by(Interview.scheduled_start)
        )
    ).all()

    candidate = await session.scalar(
        select(Candidate).where(
            Candidate.id == row.candidate_id, tenant_filter(Candidate, row.tenant_id)
        )
    )
    from app.services.hiring import candidate_comms

    return base | {
        "organisation": tenant.name if tenant else "",
        "whatsapp": {
            "opted_in": bool(candidate and candidate_comms.whatsapp_on(candidate)),
            "phone_on_file": bool(
                candidate and candidate_comms.whatsapp_number(candidate.phone)
            ),
        },
        "interviews": [
            {
                "id": str(interview.id),
                "stage_name": stage.name,
                "duration_minutes": stage.duration_minutes,
                "start_utc": interview.scheduled_start.isoformat(),
                "start_local": availability.render_for(
                    interview.scheduled_start, interview.candidate_timezone
                ).isoformat(),
                "timezone": interview.candidate_timezone,
                "mode": interview.mode.value,
                "location_or_link": interview.location_or_link,
                "preparation_notes": stage.preparation_notes,
                "can_request_reschedule": interview.scheduled_start - now > _RESCHEDULE_CUTOFF,
            }
            for interview, stage in rows
        ],
        "document_rules": {
            "categories": list(DOCUMENT_CATEGORIES),
            "accepted_types": list(ALLOWED_TYPES),
            "max_mb": resume_files.DOCUMENT_MAX_BYTES // (1024 * 1024),
        },
    }


def _title(file_name: str | None) -> str:
    """A display title only. The stored key never derives from it."""
    name = re.split(r"[\\/]", file_name or "")[-1]
    name = re.sub(r"[\x00-\x1f<>\"]+", "", name).strip()
    return name[:255] or "document"


async def upload_document(
    session: AsyncSession,
    token: str,
    *,
    category: str,
    file_name: str | None,
    data: bytes,
) -> dict[str, Any]:
    """A pre-joining document, checked from its bytes and stored under a generated key."""
    if category not in DOCUMENT_CATEGORIES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Choose one of the listed document types.",
        )
    row = await careers.resolve_portal_token(session, token)
    key, kind = await resume_files.accept_document(data, row.tenant_id)

    config = await session.scalar(
        select(CareersPageConfig).where(tenant_filter(CareersPageConfig, row.tenant_id))
    )
    retention_days = (
        config.resume_retention_days if config else ingestion.DEFAULT_RETENTION_DAYS
    )
    now = datetime.now(UTC)
    document = CandidateDocument(
        tenant_id=row.tenant_id,
        candidate_id=row.candidate_id,
        category=category,
        file_name=_title(file_name),
        storage_key=key,
        content_type=kind.mime_type,
        size_bytes=len(data),
        uploaded_at=now,
        retention_until=now.date() + timedelta(days=retention_days),
    )
    session.add(document)
    await session.flush()
    await audit.record(
        session,
        tenant_id=row.tenant_id,
        actor_type="candidate",
        action=AuditAction.CREATE,
        entity_type="candidate_document",
        entity_id=document.id,
        summary=f"Candidate uploaded a {category.replace('_', ' ')} ({kind.label})",
    )
    return {
        "document_id": str(document.id),
        "category": category,
        "file_name": document.file_name,
        "retention_until": document.retention_until.isoformat(),
    }


async def request_reschedule(
    session: AsyncSession, token: str, *, interview_id: uuid.UUID, reason: str
) -> dict[str, Any]:
    """The candidate asks for a different time. The recruiter is told; nothing moves.

    Moving the interview is the recruiter's call, because it is their panel's
    calendars that have to agree. The request reaches whoever scheduled it and the
    requisition's hiring team immediately.
    """
    cleaned = (reason or "").strip()
    if len(cleaned) < 5:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Tell the recruiter briefly why, and which times would suit you.",
        )
    row = await careers.resolve_portal_token(session, token)
    now = datetime.now(UTC)
    interview = await session.scalar(
        select(Interview).where(
            Interview.id == interview_id,
            tenant_filter(Interview, row.tenant_id),
            Interview.candidate_id == row.candidate_id,
        )
    )
    if interview is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Interview not found.")
    if interview.status not in (InterviewStatus.SCHEDULED, InterviewStatus.RESCHEDULED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This interview is no longer scheduled.",
        )
    if interview.scheduled_start - now <= _RESCHEDULE_CUTOFF:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This interview starts too soon to request a change here. Please contact "
                "the recruiter directly."
            ),
        )

    candidate = await session.scalar(
        select(Candidate).where(
            Candidate.id == row.candidate_id, tenant_filter(Candidate, row.tenant_id)
        )
    )
    requisition = (
        await session.scalar(
            select(Requisition).where(
                Requisition.id == candidate.requisition_id,
                tenant_filter(Requisition, row.tenant_id),
            )
        )
        if candidate
        else None
    )
    recipients: list[uuid.UUID] = []
    if interview.scheduled_by_user_id:
        recipients.append(interview.scheduled_by_user_id)
    team = [
        e for e in (
            requisition.hiring_manager_id if requisition else None,
            requisition.recruiter_id if requisition else None,
        )
        if e is not None
    ]
    if team:
        recipients.extend(
            (
                await session.scalars(
                    select(User.id).where(
                        tenant_filter(User, row.tenant_id), User.employee_id.in_(team)
                    )
                )
            ).all()
        )
    recipients = list(dict.fromkeys(recipients))
    if recipients:
        await dispatch.notify(
            session,
            tenant_id=row.tenant_id,
            event_key="interview.reschedule_requested",
            recipient_user_ids=recipients,
            payload={
                "candidate": candidate.full_name if candidate else "",
                "when": interview.scheduled_start.isoformat(),
                "reason": cleaned[:1000],
            },
            commit=False,
        )
    await audit.record(
        session,
        tenant_id=row.tenant_id,
        actor_type="candidate",
        action=AuditAction.UPDATE,
        entity_type="interview",
        entity_id=interview.id,
        summary=f"Candidate asked to reschedule this interview: {cleaned[:500]}",
    )
    return {
        "requested": True,
        "recruiter_told": bool(recipients),
        "message": (
            "Your request has been sent to the recruiter. Your interview stays at the "
            "current time until they confirm a new one."
            if recipients
            else "Your request is recorded, but no recruiter is assigned to receive it. "
            "Please also contact the company directly."
        ),
    }


async def set_whatsapp(session: AsyncSession, token: str, *, opt_in: bool) -> dict[str, Any]:
    """The candidate's own WhatsApp choice. Only they can make it."""
    from app.services.hiring import candidate_comms

    row = await careers.resolve_portal_token(session, token)
    candidate = await session.scalar(
        select(Candidate).where(
            Candidate.id == row.candidate_id, tenant_filter(Candidate, row.tenant_id)
        )
    )
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found.")
    now = datetime.now(UTC)
    if opt_in:
        if candidate_comms.whatsapp_number(candidate.phone) is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="There is no usable phone number on your application to message.",
            )
        candidate.whatsapp_opt_in_at = now
        candidate.whatsapp_opted_out_at = None
    elif candidate.whatsapp_opt_in_at is not None:
        candidate.whatsapp_opted_out_at = now
    await audit.record(
        session,
        tenant_id=row.tenant_id,
        actor_type="candidate",
        action=AuditAction.UPDATE,
        entity_type="candidate",
        entity_id=candidate.id,
        changes={"whatsapp": opt_in},
        summary=f"Candidate turned WhatsApp updates {'on' if opt_in else 'off'}",
    )
    return {"opted_in": candidate_comms.whatsapp_on(candidate)}


# ── Offers in the portal ────────────────────────────────────────────────────


def _portal_actor(tenant_id: uuid.UUID) -> User:
    """The identity a candidate's own action runs as. Never persisted.

    A candidate has no login, but the offer service scopes by a caller. This
    actor carries the tenant only; the audit row it causes is written with
    `actor_type="candidate"` beside it, so the log says who really acted.
    """
    from app.models.user import UserRole

    return User(
        tenant_id=tenant_id,
        email="candidate-portal@system.askmira.invalid",
        full_name="Candidate portal",
        role=UserRole.ADMIN,
        is_active=True,
        password_hash="!",
        employee_id=None,
    )


async def _portal_offer(session: AsyncSession, token: str, offer_id: uuid.UUID):
    from app.models.offer import Offer, OfferStatus

    row = await careers.resolve_portal_token(session, token)
    offer = await session.scalar(
        select(Offer).where(
            Offer.id == offer_id,
            tenant_filter(Offer, row.tenant_id),
            Offer.candidate_id == row.candidate_id,
            Offer.status.in_(
                [OfferStatus.RELEASED, OfferStatus.ACCEPTED, OfferStatus.DECLINED]
            ),
        )
    )
    if offer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Offer not found")
    return row, offer


async def portal_offers(session: AsyncSession, token: str) -> list[dict[str, Any]]:
    """The candidate's released offers, and what they answered."""
    from datetime import date as date_type

    from app.models.offer import Offer, OfferStatus

    row = await careers.resolve_portal_token(session, token)
    offers = (
        await session.scalars(
            select(Offer)
            .where(
                tenant_filter(Offer, row.tenant_id),
                Offer.candidate_id == row.candidate_id,
                Offer.status.in_(
                    [OfferStatus.RELEASED, OfferStatus.ACCEPTED, OfferStatus.DECLINED]
                ),
            )
            .order_by(Offer.released_at.desc())
        )
    ).all()
    today = date_type.today()
    return [
        {
            "id": str(offer.id),
            "code": offer.code,
            "designation": offer.designation,
            "ctc_annual": str(offer.ctc_annual),
            "currency": offer.currency,
            "joining_date": offer.joining_date.isoformat(),
            "valid_until": offer.valid_until.isoformat() if offer.valid_until else None,
            "status": offer.status.value,
            "has_letter": offer.letter_id is not None,
            "can_respond": offer.status is OfferStatus.RELEASED
            and (offer.valid_until is None or offer.valid_until >= today),
            "responded_at": offer.responded_at.isoformat() if offer.responded_at else None,
        }
        for offer in offers
    ]


async def offer_letter(session: AsyncSession, token: str, offer_id: uuid.UUID) -> dict[str, Any]:
    """The letter as the candidate reads it before answering."""
    from app.models.documents import IssuedLetter

    row, offer = await _portal_offer(session, token, offer_id)
    if offer.letter_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This offer has no letter yet. The recruiter will send it.",
        )
    letter = await session.scalar(
        select(IssuedLetter).where(
            IssuedLetter.id == offer.letter_id, tenant_filter(IssuedLetter, row.tenant_id)
        )
    )
    if letter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Letter not found")
    return {
        "reference_number": letter.reference_number,
        "html": letter.merged_html,
        "content_hash": letter.content_hash,
    }


async def respond_to_offer(
    session: AsyncSession,
    token: str,
    offer_id: uuid.UUID,
    *,
    accept: bool,
    typed_name: str | None,
    reason: str | None,
    ip_address: str | None,
    user_agent: str | None,
) -> dict[str, Any]:
    """The candidate accepts by typing their own name, or declines with a reason."""
    from app.models.documents import IssuedLetter
    from app.services.hiring import offers as offer_service

    row, offer = await _portal_offer(session, token, offer_id)
    candidate = await session.scalar(
        select(Candidate).where(
            Candidate.id == row.candidate_id, tenant_filter(Candidate, row.tenant_id)
        )
    )
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Offer not found")
    actor = _portal_actor(row.tenant_id)

    if accept:
        typed = " ".join((typed_name or "").split())
        if typed.lower() != " ".join(candidate.full_name.split()).lower():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Type your full name exactly as it appears on the offer "
                f"({candidate.full_name}) to accept.",
            )
        letter_hash = None
        if offer.letter_id is not None:
            letter_hash = await session.scalar(
                select(IssuedLetter.content_hash).where(
                    IssuedLetter.id == offer.letter_id,
                    tenant_filter(IssuedLetter, row.tenant_id),
                )
            )
        result = await offer_service.accept_offer(
            session,
            actor,
            offer_id=offer.id,
            signature_reference=f"Accepted in the candidate portal by typing '{typed}'",
        )
        offer.acceptance_record = {
            "method": "portal_typed_name",
            "typed_name": typed,
            "accepted_at": datetime.now(UTC).isoformat(),
            "ip_address": ip_address,
            "user_agent": (user_agent or "")[:512],
            "letter_content_hash": letter_hash,
        }
    else:
        clean = (reason or "").strip()
        result = await offer_service.decline_offer(
            session, actor, offer_id=offer.id, reason=clean
        )
    verb = "accepted" if accept else "declined"
    await audit.record(
        session,
        tenant_id=row.tenant_id,
        actor_type="candidate",
        action=AuditAction.UPDATE,
        entity_type="offer",
        entity_id=offer.id,
        changes={"response": verb, "via": "candidate_portal"},
        summary=f"Candidate {verb} offer {offer.code} in the portal",
    )
    return result
