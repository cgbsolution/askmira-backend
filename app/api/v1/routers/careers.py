"""Job distribution, the careers site, and the candidate portal (slice 20).

Two audiences in one file, and the split is the important thing:

- `/job-boards/...` and `/careers/...` require a logged-in user and go through
  `tenant_scoped` like everything else.
- `/public/careers/...` and `/public/candidate/...` have **no authentication**,
  because the people using them are candidates with no account. Tenant
  resolution comes from the URL slug or the portal token, never from a request
  body. These routes are throttled per client address, bound in size, and the
  apply form carries a honeypot (`app.services.hiring.public_guard`).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field, TypeAdapter, ValidationError

from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.jobboard import ApplicationStatus, JobBoard
from app.models.user import UserRole
from app.services.hiring import (
    candidate_portal,
    careers_admin,
    ingestion,
    public_apply,
    public_guard,
    resume_files,
)
from app.services.hiring import careers as careers_service
from app.services.jobboards import distribution, overview

router = APIRouter(tags=["careers"])

HR_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN, UserRole.HR_OPS)
ADMIN_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN)

#: Room for the form fields around the largest file each route accepts.
_FORM_OVERHEAD = 64 * 1024


# ── Contracts ───────────────────────────────────────────────────────────────


class BoardAccountCreate(BaseModel):
    board: JobBoard
    display_name: str | None = Field(default=None, max_length=128)
    username: str | None = Field(default=None, max_length=255)
    # Encrypted before it touches the database and never returned.
    secret: str = Field(min_length=1, max_length=4096)
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


class PublishRequest(BaseModel):
    requisition_id: uuid.UUID
    board: JobBoard
    apply_url: str | None = Field(default=None, max_length=1024)
    refresh_interval_days: int | None = Field(default=None, ge=1, le=90)


class CareersConfigUpsert(BaseModel):
    slug: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    is_published: bool = False
    hero_heading: str | None = Field(default=None, max_length=255)
    hero_subheading: str | None = Field(default=None, max_length=2000)
    about_html: str | None = Field(default=None, max_length=20000)
    logo_url: str | None = Field(default=None, max_length=1024)
    hero_image_url: str | None = Field(default=None, max_length=1024)
    primary_colour: str | None = Field(default=None, max_length=9)
    accent_colour: str | None = Field(default=None, max_length=9)
    culture_enabled: bool = True
    allow_apply_without_account: bool = True
    privacy_notice_html: str | None = Field(default=None, max_length=40000)
    privacy_notice_version: str = Field(default="1", min_length=1, max_length=32)
    resume_retention_days: int = Field(default=730, ge=1, le=3650)
    seo_title: str | None = Field(default=None, max_length=255)
    seo_description: str | None = Field(default=None, max_length=1000)


class ApplicationStatusUpdate(BaseModel):
    status: ApplicationStatus


class RescheduleRequestIn(BaseModel):
    reason: str = Field(min_length=5, max_length=1000)


_EMAIL = TypeAdapter(EmailStr)


def _refuse_oversize(request: Request, limit: int) -> None:
    """Refuse a body larger than the route could ever accept.

    The socket still has to receive it; a reverse proxy limit is what stops the
    bytes arriving at all. This stops the application doing anything with them.
    """
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit + _FORM_OVERHEAD:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="This upload is larger than we accept.",
        )


async def _read_upload(upload: UploadFile | None, limit: int) -> bytes | None:
    if upload is None:
        return None
    data = await upload.read(limit + 1)
    if not data:
        return None
    if len(data) > limit:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"The file is larger than {limit // (1024 * 1024)} MB.",
        )
    return data


def _optional_decimal(value: str | None, *, field: str, maximum: Decimal) -> Decimal | None:
    if value is None or not value.strip():
        return None
    try:
        number = Decimal(value.strip())
    except InvalidOperation as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"{field} must be a number."
        ) from exc
    if number < 0 or number > maximum or not number.is_finite():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{field} is outside the accepted range.",
        )
    return number


# ═══════════════════════════════════════════════════════════════════════════
# Board accounts and distribution — authenticated
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/job-boards/status", dependencies=[Depends(require_roles(*HR_ROLES))])
async def board_status(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    """Every supported board, its stored account, and plainly: no live connection verified."""
    return await overview.board_status(session, user)


@router.post(
    "/job-boards/accounts",
    response_model=BoardAccountRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def create_board_account(
    payload: BoardAccountCreate, user: CurrentUser, session: DbSession, request: Request
) -> BoardAccountRead:
    account = await overview.save_account(
        session,
        user,
        board=payload.board,
        display_name=payload.display_name,
        username=payload.username,
        secret=payload.secret,
        settings=payload.settings,
        request=request,
    )
    await session.commit()
    await session.refresh(account)
    return BoardAccountRead.model_validate(account)


@router.post("/job-boards/postings", dependencies=[Depends(require_roles(*HR_ROLES))])
async def publish_posting(
    payload: PublishRequest, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    """Post a requisition to a board. A failure comes back as a posting that says why."""
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


@router.get("/job-boards/postings", dependencies=[Depends(require_roles(*HR_ROLES))])
async def list_postings(
    user: CurrentUser, session: DbSession, requisition_id: uuid.UUID | None = None
) -> list[dict[str, Any]]:
    return await overview.list_postings(session, user, requisition_id=requisition_id)


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


@router.get("/job-boards/attention", dependencies=[Depends(require_roles(*HR_ROLES))])
async def postings_needing_attention(user: CurrentUser, session: DbSession) -> dict[str, Any]:
    """Postings that are not live and are not going to become live on their own."""
    return await overview.attention(session, user)


# ═══════════════════════════════════════════════════════════════════════════
# Careers site configuration and candidate records — authenticated
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/careers/config", dependencies=[Depends(require_roles(*ADMIN_ROLES))])
async def get_careers_config(user: CurrentUser, session: DbSession) -> dict[str, Any]:
    return await careers_admin.careers_config(session, user)


@router.put("/careers/config", dependencies=[Depends(require_roles(*ADMIN_ROLES))])
async def upsert_careers_config(
    payload: CareersConfigUpsert, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    result = await careers_admin.save_careers_config(
        session, user, values=payload.model_dump(), request=request
    )
    await session.commit()
    return result


@router.post(
    "/careers/candidates/{candidate_id}/portal-link",
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def issue_portal_link(
    candidate_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    """Mint a candidate portal link. The token is returned exactly once."""
    result = await careers_admin.issue_portal_link(
        session, user, candidate_id=candidate_id, request=request
    )
    await session.commit()
    return result


@router.patch(
    "/careers/applications/{application_id}/status",
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def set_application_status(
    application_id: uuid.UUID,
    payload: ApplicationStatusUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    result = await careers_admin.set_application_status(
        session, user, application_id=application_id, to_status=payload.status, request=request
    )
    await session.commit()
    return result


@router.get(
    "/careers/candidates/{candidate_id}/history",
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def candidate_application_history(
    candidate_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[dict[str, Any]]:
    """Everything this person has ever applied for here."""
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
        session, user, candidate_id=candidate_id, reason=reason[:500]
    )
    await session.commit()
    return result


@router.post("/careers/purge-expired", dependencies=[Depends(require_roles(*ADMIN_ROLES))])
async def purge_expired(
    user: CurrentUser, session: DbSession, as_of: date | None = None
) -> dict[str, Any]:
    """Retention purge. Also scheduler-callable (`careers.purge_expired`)."""
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
    """The careers page payload, text only — no HTML reaches the page."""
    return await candidate_portal.public_jobs(
        session,
        slug[:64],
        department_id=department_id,
        location_id=location_id,
        search=(search or "")[:80] or None,
    )


@router.get("/public/careers/{slug}/jobs/{code}")
async def careers_job(slug: str, code: str, session: DbSession) -> dict[str, Any]:
    """One job, text only, with its schema.org JobPosting data."""
    return await candidate_portal.public_job(session, slug[:64], code[:32])


@router.post("/public/careers/{slug}/jobs/{code}/apply", status_code=status.HTTP_201_CREATED)
async def apply(
    slug: str,
    code: str,
    request: Request,
    session: DbSession,
    full_name: Annotated[str, Form(min_length=1, max_length=255)],
    email: Annotated[str, Form(min_length=3, max_length=320)],
    consent_application_processing: Annotated[bool, Form()],
    phone: Annotated[str | None, Form(max_length=32)] = None,
    current_company: Annotated[str | None, Form(max_length=255)] = None,
    total_experience_years: Annotated[str | None, Form(max_length=8)] = None,
    notice_period_days: Annotated[int | None, Form(ge=0, le=365)] = None,
    expected_ctc: Annotated[str | None, Form(max_length=16)] = None,
    consent_talent_pool: Annotated[bool, Form()] = False,
    notice_version: Annotated[str, Form(max_length=32)] = "1",
    #: Invisible to people. Anything in it means a script filled the form.
    website: Annotated[str | None, Form(max_length=255)] = None,
    resume: Annotated[UploadFile | None, File()] = None,
) -> dict[str, Any]:
    """Apply without an account: consent required, resume checked from its bytes."""
    public_guard.throttle(request, public_guard.APPLY, scope=slug[:64])
    public_guard.refuse_honeypot(website)
    _refuse_oversize(request, resume_files.RESUME_MAX_BYTES)
    try:
        clean_email = str(_EMAIL.validate_python(email.strip()))
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="That email address does not look right.",
        ) from exc

    data = await _read_upload(resume, resume_files.RESUME_MAX_BYTES)
    result = await public_apply.submit_application(
        session,
        slug=slug[:64],
        code=code[:32],
        full_name=full_name,
        email=clean_email,
        phone=(phone or "").strip() or None,
        current_company=(current_company or "").strip() or None,
        total_experience_years=_optional_decimal(
            total_experience_years, field="Experience", maximum=Decimal(60)
        ),
        notice_period_days=notice_period_days,
        expected_ctc=_optional_decimal(
            expected_ctc, field="Expected CTC", maximum=Decimal("999999999999")
        ),
        consent_application_processing=consent_application_processing,
        consent_talent_pool=consent_talent_pool,
        notice_version=notice_version,
        resume=data,
        source_ip=public_guard.client_address(request),
    )
    await session.commit()
    return result


@router.get("/public/candidate/{token}")
async def candidate_portal_view(token: str, request: Request, session: DbSession) -> dict[str, Any]:
    """What a candidate can see about their own applications and interviews."""
    public_guard.throttle(request, public_guard.PORTAL_READ)
    result = await candidate_portal.portal_overview(session, token[:128])
    await session.commit()
    return result


@router.post("/public/candidate/{token}/documents", status_code=status.HTTP_201_CREATED)
async def upload_candidate_document(
    token: str,
    request: Request,
    session: DbSession,
    category: Annotated[str, Form(min_length=1, max_length=64)],
    file: Annotated[UploadFile, File()],
) -> dict[str, Any]:
    """Pre-joining document upload. Type from the bytes; key generated; retention applied."""
    public_guard.throttle(request, public_guard.DOCUMENT_UPLOAD)
    _refuse_oversize(request, resume_files.DOCUMENT_MAX_BYTES)
    data = await _read_upload(file, resume_files.DOCUMENT_MAX_BYTES)
    if not data:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="The file is empty."
        )
    result = await candidate_portal.upload_document(
        session, token[:128], category=category, file_name=file.filename, data=data
    )
    await session.commit()
    return result


@router.post("/public/candidate/{token}/interviews/{interview_id}/reschedule-request")
async def request_reschedule(
    token: str,
    interview_id: uuid.UUID,
    payload: RescheduleRequestIn,
    request: Request,
    session: DbSession,
) -> dict[str, Any]:
    """The candidate asks for a different time. The recruiter is told; nothing moves yet."""
    public_guard.throttle(request, public_guard.RESCHEDULE_REQUEST)
    result = await candidate_portal.request_reschedule(
        session, token[:128], interview_id=interview_id, reason=payload.reason
    )
    await session.commit()
    return result
