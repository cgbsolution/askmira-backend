"""Candidates: the pipeline list, the profile, manual entry and stage moves.

═══════════════════════════════════════════════════════════════════════════════
VISIBILITY IS THE ORIGINAL RULE, APPLIED EVERYWHERE
═══════════════════════════════════════════════════════════════════════════════

HR sees every candidate. Outside HR, only the requisition's hiring manager or
recruiter — or whoever raised it — does, and anybody else gets a 404 rather than
a 403 that confirms the person exists. Salary facts (current and expected CTC)
are HR's alone.

═══════════════════════════════════════════════════════════════════════════════
A STAGE MOVE IS A RECORD, NOT AN EDIT
═══════════════════════════════════════════════════════════════════════════════

Every move writes a `CandidateStageEvent` with who and why, in the same
transaction as the audit row. Forward moves are refused while a past interview
is unmarked or its required feedback is missing — the same gate as the
interview screen, enforced here because the API is reachable without that
screen. Interviews still in the future do not block: scheduling the next round
before moving the card is normal.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.hiring import (
    Candidate,
    CandidateStage,
    CandidateStageEvent,
    InterviewScore,
    Requisition,
    RequisitionStatus,
)
from app.models.interview import (
    Interview,
    InterviewFeedback,
    InterviewPanelist,
    InterviewStage,
    InterviewStatus,
)
from app.models.jobboard import (
    Application,
    ApplicationStatus,
    CandidateConsent,
    CandidateDocument,
    ConsentPurpose,
    JobBoard,
)
from app.models.offer import Offer
from app.models.recruiting import Referral
from app.models.user import User
from app.schemas.hiring import CandidateCreate
from app.services import audit
from app.services.hiring import ingestion, pipeline, resume_files, signals, visibility
from app.services.interviews import service as interview_service
from app.services.jobboards.base import RawApplication, normalise_email, normalise_phone

__all__ = [
    "candidate_profile",
    "create_candidate",
    "document_file",
    "list_candidates",
    "move_stage",
    "resume_file",
]


# ═══════════════════════════════════════════════════════════════════════════
# Listing
# ═══════════════════════════════════════════════════════════════════════════


async def list_candidates(
    session: AsyncSession,
    user: User,
    *,
    requisition_id: uuid.UUID | None = None,
    stage: CandidateStage | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[dict[str, Any]], int]:
    """Candidates this caller may see, with the moves open to each and the real total."""
    permissions.require_role(
        user,
        permissions.MANAGER_ROLES,
        detail="Candidates are visible to HR and to the hiring manager on the requisition.",
    )
    stmt = tenant_scoped(
        select(Candidate, Requisition.code)
        .join(Requisition, Requisition.id == Candidate.requisition_id),
        Candidate,
        user,
    )
    if not permissions.is_hr(user):
        stmt = stmt.where(Candidate.requisition_id.in_(visibility.team_requisition_ids(user)))
    if requisition_id is not None:
        stmt = stmt.where(Candidate.requisition_id == requisition_id)
    if stage is not None:
        stmt = stmt.where(Candidate.stage == stage)
    if search and search.strip():
        term = f"%{search.strip()[:80]}%"
        stmt = stmt.where(or_(Candidate.full_name.ilike(term), Candidate.email.ilike(term)))

    total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (
        await session.execute(
            stmt.order_by(Candidate.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()

    ids = [candidate.id for candidate, _ in rows]
    ambiguous: set[uuid.UUID] = set()
    if ids:
        ambiguous = set(
            (
                await session.scalars(
                    tenant_scoped(
                        select(Application.candidate_id).where(
                            Application.candidate_id.in_(ids),
                            Application.matched_on == "ambiguous",
                        ),
                        Application,
                        user,
                    )
                )
            ).all()
        )

    items = [
        {
            "id": candidate.id,
            "requisition_id": candidate.requisition_id,
            "requisition_code": code,
            "full_name": candidate.full_name,
            "email": candidate.email,
            "phone": candidate.phone,
            "stage": candidate.stage,
            "source": candidate.source,
            "total_experience_years": candidate.total_experience_years,
            "notice_period_days": candidate.notice_period_days,
            "converted_employee_id": candidate.converted_employee_id,
            "created_at": candidate.created_at,
            "allowed_moves": pipeline.allowed_targets(candidate.stage.value),
            "possible_duplicate": candidate.id in ambiguous,
        }
        for candidate, code in rows
    ]
    return items, int(total or 0)


# ═══════════════════════════════════════════════════════════════════════════
# Manual entry
# ═══════════════════════════════════════════════════════════════════════════


async def _record_event(
    session: AsyncSession,
    user: User,
    candidate: Candidate,
    *,
    from_stage: CandidateStage | None,
    to_stage: CandidateStage,
    reason: str | None,
    requisition_id: uuid.UUID | None = None,
) -> None:
    session.add(
        CandidateStageEvent(
            tenant_id=user.tenant_id,
            candidate_id=candidate.id,
            requisition_id=requisition_id or candidate.requisition_id,
            from_stage=from_stage,
            to_stage=to_stage,
            reason=reason,
            actor_user_id=user.id,
            occurred_at=datetime.now(UTC),
        )
    )


async def _possible_duplicates(
    session: AsyncSession, user: User, candidate: Candidate
) -> list[Candidate]:
    """Other candidate records sharing this person's email or phone."""
    conditions = []
    email = normalise_email(candidate.email)
    if email:
        conditions.append(Candidate.email.ilike(email))
    phone = normalise_phone(candidate.phone)
    if phone:
        conditions.append(
            func.right(func.regexp_replace(Candidate.phone, r"\D", "", "g"), 10) == phone
        )
    if not conditions:
        return []
    rows = await session.scalars(
        tenant_scoped(
            select(Candidate).where(or_(*conditions), Candidate.id != candidate.id),
            Candidate,
            user,
        )
    )
    return list(rows.all())


async def create_candidate(
    session: AsyncSession,
    user: User,
    payload: CandidateCreate,
    *,
    request: Request | None = None,
) -> dict[str, Any]:
    """Add a candidate by hand, through the same ingestion and duplicate check as a board.

    No consent is recorded: HR typed this in, and the candidate agreed to
    nothing here. The profile says "not recorded" rather than implying otherwise.
    """
    requisition = await visibility.load_requisition(session, user, payload.requisition_id)
    if requisition.status not in (RequisitionStatus.OPEN, RequisitionStatus.ON_HOLD):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Requisition {requisition.code} is "
                f"{requisition.status.value.replace('_', ' ')}. Candidates are added to "
                "an approved requisition."
            ),
        )

    outcome = await ingestion.ingest_application(
        session,
        user,
        RawApplication(
            external_application_id="",
            board=JobBoard.MANUAL.value,
            external_job_id="",
            full_name=payload.full_name.strip(),
            email=str(payload.email),
            phone=payload.phone,
            current_company=payload.current_company,
            current_ctc=payload.current_ctc,
            expected_ctc=payload.expected_ctc,
            total_experience_years=payload.total_experience_years,
            notice_period_days=payload.notice_period_days,
            applied_at=datetime.now(UTC),
            raw={"entered_by": "hr", "source_note": payload.source_note or ""},
        ),
        requisition_id=requisition.id,
        consent_purposes=[],
    )
    if outcome.application is None or outcome.candidate is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=outcome.note)

    candidate = outcome.candidate
    if outcome.created_candidate:
        await _record_event(
            session, user, candidate, from_stage=None, to_stage=CandidateStage.APPLIED,
            reason=f"Added by hand{f': {payload.source_note}' if payload.source_note else ''}",
        )
    await session.flush()

    duplicates = await _possible_duplicates(session, user, candidate)
    elsewhere = candidate.requisition_id != requisition.id
    return {
        "candidate_id": str(candidate.id),
        "application_id": str(outcome.application.id),
        "created": outcome.created_candidate,
        "note": outcome.note
        + (
            " Their pipeline stage stays on the requisition they first applied to; this "
            "application is recorded in their history."
            if elsewhere
            else ""
        ),
        "matched_on": outcome.application.matched_on,
        "possible_duplicates": [
            {"id": str(d.id), "full_name": d.full_name, "stage": d.stage.value}
            for d in duplicates
        ],
    }


# ═══════════════════════════════════════════════════════════════════════════
# Profile
# ═══════════════════════════════════════════════════════════════════════════


async def _names(session: AsyncSession, user: User, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = await session.execute(
        tenant_scoped(select(User.id, User.full_name).where(User.id.in_(ids)), User, user)
    )
    return {row.id: row.full_name for row in rows}


async def candidate_profile(
    session: AsyncSession, user: User, *, candidate_id: uuid.UUID
) -> dict[str, Any]:
    """Everything the candidate screen shows, filtered for this viewer."""
    candidate, requisition = await visibility.load_candidate(session, user, candidate_id)
    is_hr = permissions.is_hr(user)

    history = await ingestion.candidate_history(session, user, candidate_id=candidate.id)
    requisition_ids = {a.requisition_id for a in history} | {candidate.requisition_id}
    requisitions = {
        r.id: r
        for r in (
            await session.scalars(
                tenant_scoped(
                    select(Requisition).where(Requisition.id.in_(requisition_ids)),
                    Requisition,
                    user,
                )
            )
        ).all()
    }

    consents = (
        await session.scalars(
            tenant_scoped(
                select(CandidateConsent)
                .where(CandidateConsent.candidate_id == candidate.id)
                .order_by(CandidateConsent.recorded_at),
                CandidateConsent,
                user,
            )
        )
    ).all()
    latest: dict[ConsentPurpose, CandidateConsent] = {}
    for row in consents:
        latest[row.purpose] = row

    events = (
        await session.scalars(
            tenant_scoped(
                select(CandidateStageEvent)
                .where(CandidateStageEvent.candidate_id == candidate.id)
                .order_by(CandidateStageEvent.occurred_at),
                CandidateStageEvent,
                user,
            )
        )
    ).all()

    interviews = (
        await session.scalars(
            tenant_scoped(
                select(Interview)
                .where(Interview.candidate_id == candidate.id)
                .order_by(Interview.scheduled_start),
                Interview,
                user,
            )
        )
    ).all()
    stage_ids = {i.stage_id for i in interviews}
    stages = {
        s.id: s
        for s in (
            await session.scalars(
                tenant_scoped(
                    select(InterviewStage).where(InterviewStage.id.in_(stage_ids)),
                    InterviewStage,
                    user,
                )
            )
        ).all()
    } if stage_ids else {}
    interview_ids = [i.id for i in interviews]
    panel_counts: dict[uuid.UUID, int] = {}
    feedback_counts: dict[uuid.UUID, int] = {}
    if interview_ids:
        for row in await session.execute(
            tenant_scoped(
                select(InterviewPanelist.interview_id, func.count())
                .where(InterviewPanelist.interview_id.in_(interview_ids))
                .group_by(InterviewPanelist.interview_id),
                InterviewPanelist,
                user,
            )
        ):
            panel_counts[row[0]] = row[1]
        for row in await session.execute(
            tenant_scoped(
                select(InterviewFeedback.interview_id, func.count())
                .where(InterviewFeedback.interview_id.in_(interview_ids))
                .group_by(InterviewFeedback.interview_id),
                InterviewFeedback,
                user,
            )
        ):
            feedback_counts[row[0]] = row[1]

    score_rows = (
        await session.scalars(
            tenant_scoped(
                select(InterviewScore)
                .where(InterviewScore.candidate_id == candidate.id)
                .order_by(InterviewScore.round_number),
                InterviewScore,
                user,
            )
        )
    ).all()
    presented = [
        signals.present_score(
            {
                "id": s.id,
                "round_type": s.round_type,
                "round_number": s.round_number,
                "score": s.score,
                "max_score": s.max_score,
                "confidence": s.confidence,
                "is_ai_generated": s.is_ai_generated,
                "summary": s.summary,
                "signals": s.signals,
                "proctoring_notes": s.proctoring_notes,
            }
        )
        for s in score_rows
    ]

    duplicates = await _possible_duplicates(session, user, candidate)
    ambiguous = [a for a in history if a.matched_on == "ambiguous"]

    documents = (
        await session.scalars(
            tenant_scoped(
                select(CandidateDocument)
                .where(
                    CandidateDocument.candidate_id == candidate.id,
                    CandidateDocument.purged_at.is_(None),
                )
                .order_by(CandidateDocument.uploaded_at.desc()),
                CandidateDocument,
                user,
            )
        )
    ).all()

    offers: list[dict[str, Any]] = []
    referral: dict[str, Any] | None = None
    if is_hr:
        offers = [
            {
                "id": str(o.id),
                "code": o.code,
                "status": o.status.value,
                "designation": o.designation,
                "ctc_annual": str(o.ctc_annual),
                "joining_date": o.joining_date.isoformat(),
            }
            for o in (
                await session.scalars(
                    tenant_scoped(
                        select(Offer)
                        .where(Offer.candidate_id == candidate.id)
                        .order_by(Offer.created_at.desc()),
                        Offer,
                        user,
                    )
                )
            ).all()
        ]
        referral_row = await session.scalar(
            tenant_scoped(
                select(Referral).where(Referral.candidate_id == candidate.id)
                .order_by(Referral.submitted_at),
                Referral,
                user,
            )
        )
        if referral_row is not None:
            referrer = await session.scalar(
                tenant_scoped(
                    select(Employee).where(Employee.id == referral_row.referrer_employee_id),
                    Employee,
                    user,
                )
            )
            referral = {
                "referral_id": str(referral_row.id),
                "status": referral_row.status.value,
                "referrer": (
                    (referrer.display_name or f"{referrer.first_name} {referrer.last_name or ''}")
                    .strip()
                    if referrer
                    else None
                ),
                "submitted_at": referral_row.submitted_at.isoformat(),
            }

    names = await _names(session, user, {e.actor_user_id for e in events if e.actor_user_id})
    has_resume = any(a.resume_storage_key and not a.purged_at for a in history)

    return {
        "candidate": {
            "id": str(candidate.id),
            "full_name": candidate.full_name,
            "email": candidate.email,
            "phone": candidate.phone,
            "current_company": candidate.current_company,
            "total_experience_years": (
                None
                if candidate.total_experience_years is None
                else str(candidate.total_experience_years)
            ),
            "notice_period_days": candidate.notice_period_days,
            # Salary facts are HR's. A hiring manager deciding on a candidate does
            # not need their current pay, and it anchors the offer conversation.
            "current_ctc": (
                str(candidate.current_ctc) if is_hr and candidate.current_ctc is not None else None
            ),
            "expected_ctc": (
                str(candidate.expected_ctc)
                if is_hr and candidate.expected_ctc is not None
                else None
            ),
            "salary_hidden": not is_hr,
            "stage": candidate.stage.value,
            "source": candidate.source,
            "converted_employee_id": (
                str(candidate.converted_employee_id) if candidate.converted_employee_id else None
            ),
            "created_at": candidate.created_at.isoformat(),
            "has_resume": has_resume,
            "resume_text": (
                (candidate.parsed_resume or {}).get("text")
                if (candidate.parsed_resume or {}).get("status") == "extracted"
                else None
            ),
            "resume_note": (candidate.parsed_resume or {}).get("note"),
        },
        "requisition": {
            "id": str(requisition.id),
            "code": requisition.code,
            "title": requisition.title,
            "status": requisition.status.value,
        },
        "allowed_moves": pipeline.allowed_targets(candidate.stage.value),
        "applications": [
            {
                "id": str(a.id),
                "requisition_code": requisitions[a.requisition_id].code
                if a.requisition_id in requisitions
                else None,
                "requisition_title": requisitions[a.requisition_id].title
                if a.requisition_id in requisitions
                else None,
                "source": a.board.value,
                "applied_at": a.applied_at.isoformat(),
                "status": a.status.value,
                "matched_on": a.matched_on,
                "match_reason": a.match_reason,
                "purged": a.purged_at is not None,
            }
            for a in history
        ],
        "consent": {
            "purposes": [
                {
                    "purpose": purpose.value,
                    "granted": latest[purpose].granted if purpose in latest else None,
                    "recorded_at": (
                        latest[purpose].recorded_at.isoformat() if purpose in latest else None
                    ),
                    "notice_version": (
                        latest[purpose].notice_version if purpose in latest else None
                    ),
                    "status": (
                        "not_recorded"
                        if purpose not in latest
                        else "granted"
                        if latest[purpose].granted
                        else "withdrawn"
                    ),
                }
                for purpose in ConsentPurpose
            ],
            "note": (
                "Consent is recorded when a candidate applies through the careers page. "
                "A candidate added by HR or ingested from a board has no consent on "
                "record here, and that is shown as not recorded."
            ),
        },
        "stage_history": [
            {
                "from_stage": e.from_stage.value if e.from_stage else None,
                "to_stage": e.to_stage.value,
                "reason": e.reason,
                "by": names.get(e.actor_user_id) if e.actor_user_id else None,
                "occurred_at": e.occurred_at.isoformat(),
            }
            for e in events
        ],
        "interviews": [
            {
                "id": str(i.id),
                "stage_name": stages[i.stage_id].name if i.stage_id in stages else None,
                "scheduled_start": i.scheduled_start.isoformat(),
                "scheduled_end": i.scheduled_end.isoformat(),
                "mode": i.mode.value,
                "status": i.status.value,
                "panel_size": panel_counts.get(i.id, 0),
                "feedback_submitted": feedback_counts.get(i.id, 0),
            }
            for i in interviews
        ],
        "scores": presented,
        "fit_score": signals.fit_score_view(
            fit_score=candidate.fit_score, presented_scores=presented
        ),
        "duplicates": {
            "count": len(duplicates),
            # Details only for HR: a duplicate may sit on a requisition a hiring
            # manager has no business seeing.
            "records": (
                [
                    {"id": str(d.id), "full_name": d.full_name, "stage": d.stage.value}
                    for d in duplicates
                ]
                if is_hr
                else []
            ),
            "ambiguous_matches": [
                {"application_id": str(a.id), "reason": a.match_reason} for a in ambiguous
            ],
        },
        "documents": [
            {
                "id": str(d.id),
                "category": d.category,
                "file_name": d.file_name,
                "content_type": d.content_type,
                "size_bytes": d.size_bytes,
                "uploaded_at": d.uploaded_at.isoformat(),
            }
            for d in documents
        ],
        "offers": offers,
        "referral": referral,
        "viewer_is_hr": is_hr,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Moving through the pipeline
# ═══════════════════════════════════════════════════════════════════════════


async def _interview_blockers(
    session: AsyncSession, user: User, candidate: Candidate
) -> list[str]:
    """What stops this candidate advancing: past interviews unmarked or missing feedback."""
    now = datetime.now(UTC)
    interviews = (
        await session.scalars(
            tenant_scoped(
                select(Interview).where(
                    Interview.candidate_id == candidate.id,
                    Interview.status != InterviewStatus.CANCELLED,
                    Interview.scheduled_start <= now,
                ),
                Interview,
                user,
            )
        )
    ).all()
    blockers: list[str] = []
    for interview in interviews:
        blockers.extend(
            await interview_service.advance_blockers(session, user, interview_id=interview.id)
        )
    return blockers


async def move_stage(
    session: AsyncSession,
    user: User,
    *,
    candidate_id: uuid.UUID,
    to_stage: CandidateStage,
    reason: str | None,
    request: Request | None = None,
) -> dict[str, Any]:
    """Move a candidate, recording who, why and when."""
    candidate, requisition = await visibility.load_candidate(session, user, candidate_id)
    try:
        transition = pipeline.check_transition(
            candidate.stage.value, to_stage.value, reason=reason
        )
    except pipeline.PipelineError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    if transition.forward:
        blockers = await _interview_blockers(session, user, candidate)
        if blockers:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This candidate cannot move on yet. " + " ".join(blockers),
            )

    previous = candidate.stage
    await _record_event(
        session, user, candidate, from_stage=previous, to_stage=to_stage,
        reason=(reason or "").strip() or None,
    )
    candidate.stage = to_stage

    coarse = pipeline.application_status_for(to_stage.value)
    if coarse is not None:
        application = await session.scalar(
            tenant_scoped(
                select(Application)
                .where(
                    Application.candidate_id == candidate.id,
                    Application.requisition_id == requisition.id,
                )
                .order_by(Application.applied_at.desc()),
                Application,
                user,
            )
        )
        if application is not None:
            application.status = ApplicationStatus(coarse)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="candidate",
        entity_id=candidate.id,
        summary=(
            f"Candidate moved from {previous.value} to {to_stage.value} on "
            f"{requisition.code}" + (f": {reason.strip()}" if reason and reason.strip() else "")
        ),
        changes={"from": previous.value, "to": to_stage.value},
        request=request,
    )
    return {
        "candidate_id": candidate.id,
        "from_stage": previous,
        "to_stage": to_stage,
        "allowed_moves": pipeline.allowed_targets(to_stage.value),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Files
# ═══════════════════════════════════════════════════════════════════════════


async def resume_file(
    session: AsyncSession, user: User, *, candidate_id: uuid.UUID
) -> tuple[bytes, str]:
    """The latest stored resume, for somebody allowed to see the candidate."""
    candidate, requisition = await visibility.load_candidate(session, user, candidate_id)
    application = await session.scalar(
        tenant_scoped(
            select(Application)
            .where(
                Application.candidate_id == candidate.id,
                Application.resume_storage_key.is_not(None),
                Application.purged_at.is_(None),
            )
            .order_by(Application.applied_at.desc()),
            Application,
            user,
        )
    )
    if application is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No resume is stored.")
    data = await resume_files.read_stored(application.resume_storage_key)
    return data, resume_files.attachment_name(f"{requisition.code}-resume", ".pdf")


async def document_file(
    session: AsyncSession, user: User, *, candidate_id: uuid.UUID, document_id: uuid.UUID
) -> tuple[bytes, str, str]:
    candidate, _ = await visibility.load_candidate(session, user, candidate_id)
    document = await session.scalar(
        tenant_scoped(
            select(CandidateDocument).where(
                CandidateDocument.id == document_id,
                CandidateDocument.candidate_id == candidate.id,
                CandidateDocument.purged_at.is_(None),
            ),
            CandidateDocument,
            user,
        )
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    data = await resume_files.read_stored(document.storage_key)
    extension = "." + document.storage_key.rsplit(".", 1)[-1] if "." in document.storage_key else ""
    return (
        data,
        resume_files.attachment_name(document.category, extension),
        document.content_type or "application/octet-stream",
    )
