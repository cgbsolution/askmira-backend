"""Applying from the careers page, without an account.

Forced signup measurably depresses application rates, and a candidate who cannot
finish is not a candidate. So there is no user here at all: the tenant comes from
the careers slug, a tenant-bearing stand-in carries it into the ingestion service,
and nothing the caller sends can choose it.

Consent to process the application is required and refused rather than assumed.
Consent to be kept in the talent pool is separate and optional — a bundled
checkbox is not consent under DPDP.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_filter
from app.models.hiring import CandidateStage, CandidateStageEvent, Requisition, RequisitionStatus
from app.models.jobboard import CareersPageConfig, ConsentPurpose, JobBoard
from app.models.user import UserRole
from app.services.hiring import careers, ingestion, resume_files
from app.services.jobboards.base import RawApplication
from app.services.storage import StorageError, get_storage

__all__ = ["PublicActor", "submit_application"]


class PublicActor:
    """A tenant-bearing stand-in for services that expect a `User`.

    A careers-page applicant has no account. What the services need from a
    `User` is `tenant_id` and `id`, and both are supplied from the slug rather
    than from anything the caller sent.
    """

    __slots__ = ("employee_id", "id", "is_active", "role", "tenant_id")

    def __init__(self, *, tenant_id: uuid.UUID) -> None:
        self.tenant_id = tenant_id
        self.id = None
        self.role = UserRole.EMPLOYEE
        self.employee_id = None
        self.is_active = True


async def submit_application(
    session: AsyncSession,
    *,
    slug: str,
    code: str,
    full_name: str,
    email: str,
    phone: str | None,
    current_company: str | None,
    total_experience_years: Decimal | None,
    notice_period_days: int | None,
    expected_ctc: Decimal | None,
    consent_application_processing: bool,
    consent_talent_pool: bool,
    notice_version: str,
    resume: bytes | None,
    source_ip: str | None,
) -> dict[str, Any]:
    config = await session.scalar(select(CareersPageConfig).where(CareersPageConfig.slug == slug))
    if config is None or not config.is_published:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No careers page here.")
    if not config.allow_apply_without_account:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This organisation is not taking applications through this page.",
        )
    if not consent_application_processing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "We cannot process an application without consent to handle the "
                "information in it. Nothing has been saved."
            ),
        )

    requisition = await session.scalar(
        select(Requisition).where(
            tenant_filter(Requisition, config.tenant_id),
            Requisition.code == code,
            Requisition.status == RequisitionStatus.OPEN,
        )
    )
    if requisition is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="This role is no longer open."
        )

    actor = PublicActor(tenant_id=config.tenant_id)
    resume_key: str | None = None
    extracted: dict[str, Any] | None = None
    if resume:
        resume_key, extracted = await resume_files.accept_resume(resume, config.tenant_id)

    purposes = [ConsentPurpose.APPLICATION_PROCESSING]
    if consent_talent_pool:
        purposes.append(ConsentPurpose.TALENT_POOL_RETENTION)

    outcome = await ingestion.ingest_application(
        session,
        actor,
        RawApplication(
            external_application_id="",
            board=JobBoard.CAREERS.value,
            external_job_id="",
            full_name=full_name.strip(),
            email=email,
            phone=phone,
            current_company=current_company,
            expected_ctc=expected_ctc,
            total_experience_years=total_experience_years,
            notice_period_days=notice_period_days,
            applied_at=datetime.now(UTC),
        ),
        requisition_id=requisition.id,
        consent_purposes=purposes,
        notice_text=config.privacy_notice_html or "",
        notice_version=notice_version,
        source_ip=source_ip,
    )
    if outcome.application is None or outcome.candidate is None:
        if resume_key:
            with contextlib.suppress(StorageError):
                await get_storage().delete(resume_key)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=outcome.note)

    if resume_key:
        outcome.application.resume_storage_key = resume_key
        outcome.candidate.parsed_resume = extracted or {}
    if outcome.created_candidate:
        session.add(
            CandidateStageEvent(
                tenant_id=config.tenant_id,
                candidate_id=outcome.candidate.id,
                requisition_id=requisition.id,
                from_stage=None,
                to_stage=CandidateStage.APPLIED,
                reason="Applied through the careers page",
                actor_user_id=None,
                occurred_at=datetime.now(UTC),
            )
        )
    await session.flush()

    row, token = await careers.issue_portal_token(
        session, actor, candidate_id=outcome.candidate.id
    )
    from app.services.hiring import candidate_comms

    await candidate_comms.on_portal_link(
        session, actor, candidate=outcome.candidate, token=token,
        event_key=candidate_comms.APPLICATION_RECEIVED,
        # The role applied for, which for a returning candidate may not be the
        # one their record was first created against.
        payload={"job_title": requisition.title},
    )
    return {
        "applied": True,
        "portal_token": token,
        "portal_expires_at": row.expires_at.isoformat(),
        "retention_days": config.resume_retention_days,
        "resume_stored": resume_key is not None,
        "message": (
            "Your application is in. We have emailed you a link that shows its progress, "
            "with no account needed; it is also below, shown once."
        ),
    }
