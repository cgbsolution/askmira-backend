"""Job distribution, ingestion, the careers page and the candidate portal (slice 20).

Two audiences in one file, and the split is the important thing:

- `/hiring/...` and `/job-boards/...` require a logged-in user and go through
  `tenant_scoped` like everything else.
- `/public/careers/...` and `/public/candidate/...` have **no authentication at
  all**, because the people using them are candidates who have no account and
  should not need one. Tenant resolution comes from the URL slug or the portal
  token, never from a request body — a tenant id a caller can supply is a tenant
  id a caller can change.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.core.crypto import encrypt_secret
from app.core.deps import CurrentUser, DbSession, require_roles, tenant_scoped
from app.models.jobboard import (
    Application,
    ApplicationStatus,
    CandidateDocument,
    CareersPageConfig,
    ConsentPurpose,
    JobBoard,
    JobBoardAccount,
    JobPosting,
)
from app.models.user import UserRole
from app.services.hiring import careers as careers_service
from app.services.hiring import ingestion
from app.services.jobboards import distribution
from app.services.jobboards.base import RawApplication

router = APIRouter(tags=["careers"])

HR_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN, UserRole.HR_OPS)
ADMIN_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN)


# ── Contracts ───────────────────────────────────────────────────────────────


class BoardAccountCreate(BaseModel):
    board: JobBoard
    display_name: str | None = Field(default=None, max_length=128)
    username: str | None = Field(default=None, max_length=255)
    # Encrypted before it touches the database and never returned. A board
    # credential can post jobs in the client's name and read their resume
    # database.
    secret: str = Field(min_length=1)
    settings: dict[str, Any] = Field(default_factory=dict)


class BoardAccountRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    board: JobBoard
    display_name: str | None
    username: str | None
    settings: dict
    is_active: bool
    last_polled_at: datetime | None
    last_error: str | None


class PostingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    requisition_id: uuid.UUID
    board: JobBoard
    external_job_id: str | None
    status: str
    published_at: datetime | None
    expires_at: datetime | None
    next_refresh_at: datetime | None
    next_attempt_at: datetime | None
    last_error: str | None
    warnings: list
    view_count: int
    application_count: int


class PublishRequest(BaseModel):
    requisition_id: uuid.UUID
    board: JobBoard
    apply_url: str | None = None
    refresh_interval_days: int | None = Field(default=None, ge=1, le=90)


class CareersConfigUpsert(BaseModel):
    slug: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    is_published: bool = False
    hero_heading: str | None = Field(default=None, max_length=255)
    hero_subheading: str | None = None
    about_html: str | None = None
    logo_url: str | None = Field(default=None, max_length=1024)
    hero_image_url: str | None = Field(default=None, max_length=1024)
    primary_colour: str | None = Field(default=None, max_length=9)
    accent_colour: str | None = Field(default=None, max_length=9)
    culture_enabled: bool = True
    allow_apply_without_account: bool = True
    privacy_notice_html: str | None = None
    privacy_notice_version: str = Field(default="1", max_length=32)
    resume_retention_days: int = Field(default=730, ge=1, le=3650)
    seo_title: str | None = Field(default=None, max_length=255)
    seo_description: str | None = None


class PublicApplication(BaseModel):
    """An application from the careers page.

    No account, deliberately. Consent is explicit and separate per purpose —
    a single bundled checkbox is not consent under DPDP, and it cannot be
    withdrawn from one purpose without the other.
    """

    full_name: str = Field(min_length=1, max_length=255)
    email: str = Field(min_length=3, max_length=320)
    phone: str | None = Field(default=None, max_length=32)
    resume_url: str | None = Field(default=None, max_length=1024)
    current_company: str | None = Field(default=None, max_length=255)
    current_ctc: Decimal | None = Field(default=None, ge=0)
    expected_ctc: Decimal | None = Field(default=None, ge=0)
    total_experience_years: Decimal | None = Field(default=None, ge=0)
    notice_period_days: int | None = Field(default=None, ge=0, le=365)

    # Required to process the application at all. Refused rather than assumed.
    consent_application_processing: bool
    # Separate, and genuinely optional: being kept on file for future roles is
    # not something somebody agreed to by applying for one.
    consent_talent_pool: bool = False
    notice_version: str = Field(default="1", max_length=32)


class ApplicationStatusUpdate(BaseModel):
    status: ApplicationStatus


class DocumentUpload(BaseModel):
    category: str = Field(min_length=1, max_length=64)
    file_name: str = Field(min_length=1, max_length=255)
    storage_key: str = Field(min_length=1, max_length=512)
    content_type: str | None = Field(default=None, max_length=128)
    size_bytes: int | None = Field(default=None, ge=0)


# ═══════════════════════════════════════════════════════════════════════════
# Board accounts and distribution — authenticated
# ═══════════════════════════════════════════════════════════════════════════


@router.post(
    "/job-boards/accounts",
    response_model=BoardAccountRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def create_board_account(
    payload: BoardAccountCreate, user: CurrentUser, session: DbSession
) -> BoardAccountRead:
    existing = await session.scalar(
        tenant_scoped(
            select(JobBoardAccount).where(JobBoardAccount.board == payload.board),
            JobBoardAccount,
            user,
        )
    )
    account = existing or JobBoardAccount(tenant_id=user.tenant_id, board=payload.board)
    account.display_name = payload.display_name
    account.username = payload.username
    account.settings = payload.settings
    account.credentials_encrypted = encrypt_secret(payload.secret, user.tenant_id)
    account.is_active = True
    account.last_error = None
    session.add(account)
    await session.commit()
    await session.refresh(account)
    return BoardAccountRead.model_validate(account)


@router.get(
    "/job-boards/accounts",
    response_model=list[BoardAccountRead],
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def list_board_accounts(
    user: CurrentUser, session: DbSession
) -> list[BoardAccountRead]:
    rows = await session.scalars(
        tenant_scoped(select(JobBoardAccount), JobBoardAccount, user)
    )
    return [BoardAccountRead.model_validate(r) for r in rows.all()]


@router.post("/job-boards/postings", dependencies=[Depends(require_roles(*HR_ROLES))])
async def publish_posting(
    payload: PublishRequest, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    """Post a requisition to a board.

    A failure here comes back as a posting row that says why, never as a silent
    absence — see `app/services/jobboards/distribution.py`.
    """
    result = await distribution.publish_posting(
        session,
        user,
        requisition_id=payload.requisition_id,
        board=payload.board,
        apply_url=payload.apply_url,
        refresh_interval_days=payload.refresh_interval_days,
        request=request,
    )
    await session.commit()
    return result


@router.get(
    "/job-boards/postings",
    response_model=list[PostingRead],
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def list_postings(
    user: CurrentUser, session: DbSession, requisition_id: uuid.UUID | None = None
) -> list[PostingRead]:
    stmt = select(JobPosting).order_by(JobPosting.created_at.desc())
    if requisition_id is not None:
        stmt = stmt.where(JobPosting.requisition_id == requisition_id)
    rows = await session.scalars(tenant_scoped(stmt, JobPosting, user))
    return [PostingRead.model_validate(r) for r in rows.all()]


@router.post(
    "/job-boards/postings/{posting_id}/refresh",
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def refresh_posting(
    posting_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    result = await distribution.refresh_posting(session, user, posting_id=posting_id)
    await session.commit()
    return result


@router.post(
    "/job-boards/postings/{posting_id}/close",
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def close_posting(
    posting_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    result = await distribution.close_posting(session, user, posting_id=posting_id)
    await session.commit()
    return result


@router.get(
    "/job-boards/attention", dependencies=[Depends(require_roles(*HR_ROLES))]
)
async def postings_needing_attention(
    user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    """Postings that are not live and are not going to become live on their own.

    The whole reason PARKED exists: a job nobody posted is otherwise invisible
    until somebody notices no applications arrived.
    """
    from app.models.jobboard import PostingStatus

    parked = (
        await session.scalars(
            tenant_scoped(
                select(JobPosting).where(JobPosting.status == PostingStatus.PARKED),
                JobPosting,
                user,
            )
        )
    ).all()
    due = await distribution.retryable_postings(session, user)
    refreshes = await distribution.due_refreshes(session, user)
    return {
        "parked": [
            {
                "posting_id": str(p.id),
                "board": p.board.value,
                "requisition_id": str(p.requisition_id),
                "reason": p.last_error,
                "attempts": p.attempt_count,
            }
            for p in parked
        ],
        "retry_due": [str(p.id) for p in due],
        "refresh_due": [str(p.id) for p in refreshes],
    }


# ═══════════════════════════════════════════════════════════════════════════
# Careers page configuration — authenticated
# ═══════════════════════════════════════════════════════════════════════════


@router.put("/careers/config", dependencies=[Depends(require_roles(*ADMIN_ROLES))])
async def upsert_careers_config(
    payload: CareersConfigUpsert, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    config = await session.scalar(
        tenant_scoped(select(CareersPageConfig), CareersPageConfig, user)
    )
    if config is None:
        config = CareersPageConfig(tenant_id=user.tenant_id, slug=payload.slug)
        session.add(config)

    if payload.is_published and not payload.privacy_notice_html:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "A published careers page needs a privacy notice. Collecting a resume "
                "without telling the candidate what happens to it or for how long is "
                "not something DPDP allows, and it is the notice they consent to."
            ),
        )

    for field, value in payload.model_dump().items():
        setattr(config, field, value)
    await session.commit()
    await session.refresh(config)
    return {"slug": config.slug, "is_published": config.is_published}


@router.post(
    "/careers/candidates/{candidate_id}/portal-link",
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def issue_portal_link(
    candidate_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    """Mint a candidate portal link.

    The plaintext token is returned exactly once — only its hash is stored.
    """
    row, token = await careers_service.issue_portal_token(
        session, user, candidate_id=candidate_id
    )
    await session.commit()
    return {
        "token": token,
        "expires_at": row.expires_at.isoformat(),
        "note": (
            "This token is shown once and cannot be retrieved again. Send it to the "
            "candidate now; issue a new link if it is lost."
        ),
    }


@router.patch(
    "/careers/applications/{application_id}/status",
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def set_application_status(
    application_id: uuid.UUID,
    payload: ApplicationStatusUpdate,
    user: CurrentUser,
    session: DbSession,
) -> dict[str, Any]:
    application = await session.scalar(
        tenant_scoped(
            select(Application).where(Application.id == application_id), Application, user
        )
    )
    if application is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    application.status = payload.status
    await session.commit()
    return {"application_id": str(application.id), "status": application.status.value}


@router.get(
    "/careers/candidates/{candidate_id}/history",
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def candidate_application_history(
    candidate_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[dict[str, Any]]:
    """Everything this person has ever applied for here.

    "Applied three times, rejected twice at final round" is the sentence this
    endpoint exists to make available before somebody opens a resume.
    """
    rows = await ingestion.candidate_history(session, user, candidate_id=candidate_id)
    return [
        {
            "application_id": str(a.id),
            "requisition_id": str(a.requisition_id),
            "board": a.board.value,
            "applied_on": a.applied_at.date().isoformat(),
            "status": a.status.value,
            "matched_on": a.matched_on,
        }
        for a in rows
    ]


# ═══════════════════════════════════════════════════════════════════════════
# DPDP
# ═══════════════════════════════════════════════════════════════════════════


@router.post(
    "/careers/candidates/{candidate_id}/erase",
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def erase_candidate(
    candidate_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    reason: str = "Candidate requested deletion",
) -> dict[str, Any]:
    result = await careers_service.purge_on_request(
        session, user, candidate_id=candidate_id, reason=reason
    )
    await session.commit()
    return result


@router.post("/careers/purge-expired", dependencies=[Depends(require_roles(*ADMIN_ROLES))])
async def purge_expired(
    user: CurrentUser, session: DbSession, as_of: date | None = None
) -> dict[str, Any]:
    result = await careers_service.purge_expired(session, user, as_of=as_of)
    await session.commit()
    return result


# ═══════════════════════════════════════════════════════════════════════════
# Public — no authentication, tenant resolved from the URL
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/public/careers/{slug}")
async def careers_page(
    slug: str,
    session: DbSession,
    department_id: uuid.UUID | None = None,
    location_id: uuid.UUID | None = None,
    search: str | None = None,
) -> dict[str, Any]:
    """The careers page payload, for server rendering.

    Server-rendered rather than fetched by the browser, because a job page that
    only exists after JavaScript runs is a job page Google indexes
    inconsistently — and indexing is most of the point of having one.
    """
    return await careers_service.public_job_list(
        session, slug, department_id=department_id, location_id=location_id, search=search
    )


@router.get("/public/careers/{slug}/jobs/{code}")
async def careers_job(slug: str, code: str, session: DbSession) -> dict[str, Any]:
    """One job, with its schema.org JobPosting markup ready to embed."""
    return await careers_service.public_job_detail(session, slug, code)


@router.post("/public/careers/{slug}/jobs/{code}/apply", status_code=status.HTTP_201_CREATED)
async def apply(
    slug: str, code: str, payload: PublicApplication, session: DbSession, request: Request
) -> dict[str, Any]:
    """Apply, without an account.

    Forced signup measurably depresses application rates, and a candidate who
    cannot finish is not a candidate.
    """
    config = await session.scalar(
        select(CareersPageConfig).where(CareersPageConfig.slug == slug)
    )
    if config is None or not config.is_published:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No careers page here.")

    if not payload.consent_application_processing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "We cannot process an application without consent to handle the "
                "information in it. Nothing has been saved."
            ),
        )

    from app.models.hiring import Requisition, RequisitionStatus

    requisition = await session.scalar(
        select(Requisition).where(
            Requisition.tenant_id == config.tenant_id,
            Requisition.code == code,
            Requisition.status == RequisitionStatus.OPEN,
        )
    )
    if requisition is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="This role is no longer open."
        )

    # A candidate has no user, so the ingestion service is handed a
    # tenant-bearing stand-in rather than a request-supplied tenant id. The
    # tenant came from the slug; nothing the caller sent decides it.
    actor = _PublicActor(tenant_id=config.tenant_id)

    purposes = [ConsentPurpose.APPLICATION_PROCESSING]
    if payload.consent_talent_pool:
        purposes.append(ConsentPurpose.TALENT_POOL_RETENTION)

    outcome = await ingestion.ingest_application(
        session,
        actor,
        RawApplication(
            external_application_id="",
            board=JobBoard.CAREERS.value,
            external_job_id="",
            full_name=payload.full_name,
            email=payload.email,
            phone=payload.phone,
            resume_url=payload.resume_url,
            current_company=payload.current_company,
            current_ctc=payload.current_ctc,
            expected_ctc=payload.expected_ctc,
            total_experience_years=payload.total_experience_years,
            notice_period_days=payload.notice_period_days,
            applied_at=datetime.now(UTC),
        ),
        requisition_id=requisition.id,
        consent_purposes=purposes,
        notice_text=config.privacy_notice_html or "",
        notice_version=payload.notice_version,
        source_ip=request.client.host if request.client else None,
    )
    if outcome.application is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=outcome.note
        )

    row, token = await careers_service.issue_portal_token(
        session, actor, candidate_id=outcome.candidate.id
    )
    await session.commit()
    return {
        "applied": True,
        "portal_token": token,
        "portal_expires_at": row.expires_at.isoformat(),
        "retention_days": config.resume_retention_days,
        "message": (
            "Your application is in. Use the link to check its progress — you will not "
            "need an account."
        ),
    }


@router.get("/public/candidate/{token}")
async def candidate_portal(token: str, session: DbSession) -> dict[str, Any]:
    """What a candidate can see about their own applications."""
    return await careers_service.portal_view(session, token)


@router.post(
    "/public/candidate/{token}/documents", status_code=status.HTTP_201_CREATED
)
async def upload_candidate_document(
    token: str, payload: DocumentUpload, session: DbSession
) -> dict[str, Any]:
    """Pre-joining document upload, feeding slice 24.

    Held under the same retention clock as the resume: a PAN card belonging to
    somebody who never joined should not sit in a bucket for six years.
    """
    row = await careers_service.resolve_portal_token(session, token)
    config = await session.scalar(
        select(CareersPageConfig).where(CareersPageConfig.tenant_id == row.tenant_id)
    )
    retention_days = config.resume_retention_days if config else ingestion.DEFAULT_RETENTION_DAYS
    now = datetime.now(UTC)

    document = CandidateDocument(
        tenant_id=row.tenant_id,
        candidate_id=row.candidate_id,
        category=payload.category,
        file_name=payload.file_name,
        storage_key=payload.storage_key,
        content_type=payload.content_type,
        size_bytes=payload.size_bytes,
        uploaded_at=now,
        retention_until=now.date() + timedelta(days=retention_days),
    )
    session.add(document)
    await session.commit()
    return {
        "document_id": str(document.id),
        "retention_until": document.retention_until.isoformat(),
    }


class _PublicActor:
    """A tenant-bearing stand-in for services that expect a `User`.

    A careers-page applicant has no account. What the services actually need
    from a `User` is `tenant_id` and `id`, and both are supplied here from the
    slug rather than from anything the caller sent — a tenant id a caller can
    supply is a tenant id a caller can change.
    """

    __slots__ = ("employee_id", "id", "is_active", "role", "tenant_id")

    def __init__(self, *, tenant_id: uuid.UUID) -> None:
        self.tenant_id = tenant_id
        self.id = None
        self.role = UserRole.EMPLOYEE
        self.employee_id = None
        self.is_active = True
