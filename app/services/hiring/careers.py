"""The careers page, the candidate's own view of their application, and purge.

Three decisions worth stating.

**Apply without an account.** Forced signup measurably depresses application
rates, and a candidate who cannot finish is not a candidate. The config carries
the switch; the apply path never requires a user.

**A candidate portal is cheap for us and disproportionately valued.** Being left
in silence is the loudest complaint about hiring in this market. The portal
shows a deliberately coarse status — applied, under review, shortlisted,
interviewing, offered, closed — because a candidate needs to know somebody is
looking, not that they are at "AI round 2 pending panel", and the internal stage
would leak how many people are ahead of them.

**Purge means the personal data goes and the row stays.** Deleting the
application row too would silently change last year's hiring statistics: the
count of applications is not personal data, the applicant is. So the row is
retained with its dates and its board, and every identifying field is cleared.
"""

from __future__ import annotations

import contextlib
import hashlib
import secrets
import uuid
from datetime import UTC, date, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_filter, tenant_scoped
from app.models.audit import AuditAction
from app.models.background_checks import BackgroundCheck, BackgroundCheckStatus
from app.models.candidate_comms import CandidateMessage, CandidateMessageStatus
from app.models.culture import Club, Shoutout, ShoutoutVisibility
from app.models.hiring import Candidate, Requisition, RequisitionStatus
from app.models.jobboard import (
    Application,
    ApplicationStatus,
    CandidateConsent,
    CandidateDocument,
    CandidatePortalToken,
    CareersPageConfig,
    ConsentPurpose,
)
from app.models.org import Location
from app.models.talent_pools import TalentPoolMember
from app.models.tenant import Tenant
from app.models.user import User
from app.services import audit
from app.services.hiring.structured_data import (
    JobPostingSeo,
    job_posting_jsonld,
    validate_posting,
)
from app.services.storage import StorageError, get_storage

__all__ = [
    "PORTAL_TOKEN_TTL_DAYS",
    "issue_portal_token",
    "portal_view",
    "public_job_detail",
    "public_job_list",
    "purge_expired",
    "purge_on_request",
    "resolve_portal_token",
]

#: Long enough that a candidate who opens the email a fortnight later still gets
#: in; short enough that a forwarded link does not work forever.
PORTAL_TOKEN_TTL_DAYS = 90


# ═══════════════════════════════════════════════════════════════════════════
# Careers page — served without a tenant in scope
# ═══════════════════════════════════════════════════════════════════════════


async def _config_by_slug(session: AsyncSession, slug: str) -> CareersPageConfig:
    """Resolve the tenant from the public URL.

    Deliberately not `tenant_scoped`: there is nobody logged in on a careers
    page, so the slug *is* the tenant resolution. That is why the slug is
    globally unique at the database rather than unique per tenant.
    """
    config = await session.scalar(select(CareersPageConfig).where(CareersPageConfig.slug == slug))
    if config is None or not config.is_published:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No careers page here.")
    return config


async def public_job_list(
    session: AsyncSession,
    slug: str,
    *,
    department_id: uuid.UUID | None = None,
    location_id: uuid.UUID | None = None,
    search: str | None = None,
) -> dict:
    """The jobs a visitor can see, plus the branding to render them in.

    Only OPEN requisitions. A page listing a role that has been filled or put on
    hold collects applications from people who have no chance, which is the
    cheapest way to look like a company that does not respect candidates' time.
    """
    config = await _config_by_slug(session, slug)
    tenant = await session.get(Tenant, config.tenant_id)

    stmt = select(Requisition).where(
        tenant_filter(Requisition, config.tenant_id),
        Requisition.status == RequisitionStatus.OPEN,
    )
    if department_id is not None:
        stmt = stmt.where(Requisition.department_id == department_id)
    if location_id is not None:
        stmt = stmt.where(Requisition.location_id == location_id)
    if search:
        stmt = stmt.where(Requisition.title.ilike(f"%{search.strip()}%"))

    jobs = (await session.scalars(stmt.order_by(Requisition.created_at.desc()))).all()

    culture: dict = {"shoutouts": [], "clubs": []}
    if config.culture_enabled:
        culture = await _culture_highlights(session, config.tenant_id)

    return {
        "branding": {
            "organisation": tenant.name if tenant else "",
            "logo_url": config.logo_url,
            "hero_image_url": config.hero_image_url,
            "hero_heading": config.hero_heading,
            "hero_subheading": config.hero_subheading,
            "about_html": config.about_html,
            "primary_colour": config.primary_colour,
            "accent_colour": config.accent_colour,
            "seo_title": config.seo_title,
            "seo_description": config.seo_description,
        },
        "allow_apply_without_account": config.allow_apply_without_account,
        "privacy_notice_html": config.privacy_notice_html,
        "privacy_notice_version": config.privacy_notice_version,
        "retention_days": config.resume_retention_days,
        "jobs": [
            {
                "code": job.code,
                "title": job.title,
                "department_id": str(job.department_id) if job.department_id else None,
                "location_id": str(job.location_id) if job.location_id else None,
                "employment_type": job.employment_type,
                "experience_min_years": job.experience_min_years,
                "experience_max_years": job.experience_max_years,
                "openings": job.open_vacancies,
                "posted_on": job.created_at.date().isoformat(),
            }
            for job in jobs
        ],
        "culture": culture,
    }


async def _culture_highlights(session: AsyncSession, tenant_id: uuid.UUID) -> dict:
    """Real shoutouts and clubs from the Culture module, or nothing.

    Never a placeholder and never an example. An invented testimonial on a
    careers page is precisely the kind of thing a new joiner finds out about in
    their first week, and it costs far more than the empty section it replaced.

    **Two things are deliberately withheld.** Employee names never leave the
    company: a shoutout is visible to colleagues, which is not the same as
    consenting to appear on the public internet, and nobody asked the person
    being praised whether they wanted to be recruitment material. Private clubs
    are excluded for the same reason they are private internally.
    """
    shoutouts = await session.scalars(
        select(Shoutout)
        .where(
            tenant_filter(Shoutout, tenant_id),
            Shoutout.visibility == ShoutoutVisibility.COMPANY,
        )
        .order_by(Shoutout.created_at.desc())
        .limit(6)
    )
    clubs = await session.scalars(
        select(Club)
        .where(
            tenant_filter(Club, tenant_id),
            Club.is_active.is_(True),
            Club.is_private.is_(False),
        )
        .order_by(Club.name)
        .limit(12)
    )
    return {
        "shoutouts": [
            {
                # No names, on purpose. See the docstring.
                "message": row.message,
                "value": row.value_tag,
                "given_on": row.created_at.date().isoformat(),
            }
            for row in shoutouts.all()
        ],
        "clubs": [
            {"name": club.name, "description": club.description, "icon": club.icon}
            for club in clubs.all()
        ],
    }


async def public_job_detail(session: AsyncSession, slug: str, code: str) -> dict:
    """One job, with its structured data ready to embed.

    The JSON-LD is built and validated here rather than in the page template,
    so a job that would not be indexed is reported as a problem to whoever
    published it instead of quietly failing to appear in search.
    """
    config = await _config_by_slug(session, slug)
    tenant = await session.get(Tenant, config.tenant_id)

    job = await session.scalar(
        select(Requisition).where(
            tenant_filter(Requisition, config.tenant_id),
            Requisition.code == code,
            Requisition.status == RequisitionStatus.OPEN,
        )
    )
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This role is no longer open.",
        )

    city = state = None
    if job.location_id:
        location = await session.get(Location, job.location_id)
        if location is not None:
            city = location.name
            state = getattr(location, "state_code", None)

    published = [v for v in (job.jd_versions or []) if v.get("body")]
    description = published[-1]["body"] if published else (job.description or "")

    seo = JobPostingSeo(
        title=job.title,
        description_html=description,
        date_posted=job.created_at.date(),
        organisation_name=tenant.name if tenant else "",
        organisation_logo_url=config.logo_url,
        valid_through=job.target_close_date,
        employment_type=job.employment_type,
        city=city,
        state=state,
        identifier=job.code,
        direct_apply_url=f"/careers/{slug}/jobs/{job.code}/apply",
        # The budget band on a requisition is an internal number. Emitting it
        # into search results because it happens to be available would publish
        # the client's own salary bands.
        show_salary=False,
    )

    return {
        "job": {
            "code": job.code,
            "title": job.title,
            "description_html": description,
            "skills": job.skills,
            "employment_type": job.employment_type,
            "experience_min_years": job.experience_min_years,
            "experience_max_years": job.experience_max_years,
            "openings": job.open_vacancies,
            "location": city,
            "posted_on": job.created_at.date().isoformat(),
        },
        "structured_data": job_posting_jsonld(seo),
        # Non-empty means this page will not index correctly. Returned rather
        # than logged: the person who can fix it is the one configuring the page.
        "seo_problems": validate_posting(seo),
        "privacy_notice_html": config.privacy_notice_html,
        "privacy_notice_version": config.privacy_notice_version,
        "retention_days": config.resume_retention_days,
        "allow_apply_without_account": config.allow_apply_without_account,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Candidate portal
# ═══════════════════════════════════════════════════════════════════════════


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def issue_portal_token(
    session: AsyncSession,
    user: User,
    *,
    candidate_id: uuid.UUID,
    now: datetime | None = None,
) -> tuple[CandidatePortalToken, str]:
    """A link the candidate can open without an account.

    Returns the row and the plaintext token, which is the only time the token
    exists — only its hash is stored, so a leaked database does not hand over
    every candidate's portal.
    """
    moment = now or datetime.now(UTC)
    token = secrets.token_urlsafe(32)
    row = CandidatePortalToken(
        tenant_id=user.tenant_id,
        candidate_id=candidate_id,
        token_hash=_hash(token),
        expires_at=moment + timedelta(days=PORTAL_TOKEN_TTL_DAYS),
    )
    session.add(row)
    await session.flush()
    return row, token


async def resolve_portal_token(
    session: AsyncSession, token: str, *, now: datetime | None = None
) -> CandidatePortalToken:
    """Find the candidate behind a portal link.

    Not tenant-scoped, because a candidate has no tenant context — the token
    carries it. The token is the credential, which is why it is 32 random bytes
    and why only its hash is stored.
    """
    moment = now or datetime.now(UTC)
    row = await session.scalar(
        select(CandidatePortalToken).where(CandidatePortalToken.token_hash == _hash(token))
    )
    if row is None or row.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="This link is no longer valid."
        )
    if row.expires_at < moment:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=(
                "This link has expired. Ask the recruiter for a new one — links are "
                "time-limited so a forwarded email does not stay open indefinitely."
            ),
        )
    row.last_used_at = moment
    return row


async def portal_view(session: AsyncSession, token: str, *, now: datetime | None = None) -> dict:
    """What a candidate sees about themselves.

    Coarse by design. A candidate needs to know somebody is looking; the
    internal stage would tell them how far through a funnel they are and how
    many people are ahead.
    """
    row = await resolve_portal_token(session, token, now=now)
    candidate = await session.get(Candidate, row.candidate_id)
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")

    applications = (
        await session.scalars(
            select(Application)
            .where(
                tenant_filter(Application, row.tenant_id),
                Application.candidate_id == row.candidate_id,
            )
            .order_by(Application.applied_at.desc())
        )
    ).all()

    roles: dict[uuid.UUID, Requisition] = {}
    for application in applications:
        if application.requisition_id not in roles:
            requisition = await session.get(Requisition, application.requisition_id)
            if requisition is not None:
                roles[application.requisition_id] = requisition

    documents = (
        await session.scalars(
            select(CandidateDocument).where(
                tenant_filter(CandidateDocument, row.tenant_id),
                CandidateDocument.candidate_id == row.candidate_id,
                CandidateDocument.purged_at.is_(None),
            )
        )
    ).all()

    return {
        "candidate": {"name": candidate.full_name, "email": candidate.email},
        "applications": [
            {
                "role": roles[a.requisition_id].title if a.requisition_id in roles else "",
                "applied_on": a.applied_at.date().isoformat(),
                "status": a.status.value,
                # Said in words, because a status chip alone still leaves
                # somebody wondering whether anyone has looked.
                "status_note": _status_note(a.status),
            }
            for a in applications
        ],
        "documents": [
            {
                "category": d.category,
                "file_name": d.file_name,
                "uploaded_on": d.uploaded_at.date().isoformat(),
            }
            for d in documents
        ],
        "retention_note": (
            "Your application and any documents you have uploaded are kept for as long "
            "as the privacy notice you accepted says, and then deleted. You can ask for "
            "them to be deleted sooner."
        ),
    }


def _status_note(value: ApplicationStatus) -> str:
    return {
        ApplicationStatus.APPLIED: "We have your application. Nobody has reviewed it yet.",
        ApplicationStatus.UNDER_REVIEW: "Someone is reading your application now.",
        ApplicationStatus.SHORTLISTED: (
            "You have been shortlisted. Expect to hear about next steps."
        ),
        ApplicationStatus.INTERVIEWING: "You are in the interview process for this role.",
        ApplicationStatus.OFFERED: "An offer has been made for this role.",
        ApplicationStatus.CLOSED: ("This application is closed. We are not taking it further."),
    }[value]


# ═══════════════════════════════════════════════════════════════════════════
# Retention and deletion
# ═══════════════════════════════════════════════════════════════════════════

#: What a purge writes over an identifying field. A recognisable marker rather
#: than an empty string, so a purged row is obviously purged and not obviously
#: broken.
REDACTED = "[deleted on request]"


async def _purge_hiring_extras(
    session: AsyncSession, tenant_id: uuid.UUID, candidate: Candidate
) -> None:
    """What an erasure clears beyond the candidate row: pools, emails, background checks.

    Kept apart from `_purge_candidate`, which must never delete a row: removing
    a pool membership or an agency's report file does not touch the hiring
    numbers that function protects.
    """
    # Kept "for later" is exactly what an erasure ends.
    await session.execute(
        delete(TalentPoolMember).where(
            tenant_filter(TalentPoolMember, tenant_id),
            TalentPoolMember.candidate_id == candidate.id,
        )
    )
    # The emails said their name and went to their address; the log keeps only
    # that something was sent, and nothing queued goes out after the request.
    messages = (
        await session.scalars(
            select(CandidateMessage).where(
                tenant_filter(CandidateMessage, tenant_id),
                CandidateMessage.candidate_id == candidate.id,
            )
        )
    ).all()
    for message in messages:
        message.to_address = f"purged+{candidate.id}@invalid"
        message.subject = REDACTED
        message.body = REDACTED
        message.secret_link_encrypted = None
        if message.status is CandidateMessageStatus.QUEUED:
            message.status = CandidateMessageStatus.CANCELLED
            message.failure_reason = "Withdrawn: the candidate's data was erased."
            message.next_attempt_at = None

    # Background checks keep their shape (that one was run, and how it ended);
    # the consent signature, the notes and the agency's report go.
    checks = (
        await session.scalars(
            select(BackgroundCheck).where(
                tenant_filter(BackgroundCheck, tenant_id),
                BackgroundCheck.candidate_id == candidate.id,
            )
        )
    ).all()
    for check in checks:
        if check.report_storage_key:
            with contextlib.suppress(StorageError):
                await get_storage().delete(check.report_storage_key)
        check.report_storage_key = None
        check.report_filename = None
        check.consent_typed_name = None
        check.consent_ip = None
        check.consent_user_agent = None
        check.summary = None
        check.checks = [
            {"type": c.get("type"), "status": c.get("status"), "note": None}
            for c in check.checks or []
        ]
        if check.status in (
            BackgroundCheckStatus.AWAITING_CONSENT,
            BackgroundCheckStatus.IN_PROGRESS,
        ):
            check.status = BackgroundCheckStatus.CANCELLED
            check.cancelled_reason = "The candidate's data was erased."


async def _purge_candidate(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    candidate: Candidate,
    *,
    note: str,
    now: datetime,
) -> dict:
    """Clear the person, keep the shape.

    Deleting the rows outright would silently change last year's hiring numbers.
    The count of applications is not personal data; the applicant is.
    """
    candidate.full_name = REDACTED
    candidate.email = f"purged+{candidate.id}@invalid"
    candidate.phone = None
    candidate.resume_url = None
    candidate.parsed_resume = {}
    candidate.current_company = None
    candidate.current_ctc = None
    candidate.expected_ctc = None
    candidate.tags = []

    await _purge_hiring_extras(session, tenant_id, candidate)

    applications = (
        await session.scalars(
            select(Application).where(
                tenant_filter(Application, tenant_id),
                Application.candidate_id == candidate.id,
            )
        )
    ).all()
    for application in applications:
        application.raw_payload = {}
        application.resume_storage_key = None
        application.purged_at = now
        application.purge_note = note

    documents = (
        await session.scalars(
            select(CandidateDocument).where(
                tenant_filter(CandidateDocument, tenant_id),
                CandidateDocument.candidate_id == candidate.id,
                CandidateDocument.purged_at.is_(None),
            )
        )
    ).all()
    for document in documents:
        document.storage_key = ""
        document.purged_at = now

    # Portal links die with the data they showed.
    tokens = (
        await session.scalars(
            select(CandidatePortalToken).where(
                tenant_filter(CandidatePortalToken, tenant_id),
                CandidatePortalToken.candidate_id == candidate.id,
                CandidatePortalToken.revoked_at.is_(None),
            )
        )
    ).all()
    for token in tokens:
        token.revoked_at = now

    await session.flush()
    return {
        "candidate_id": str(candidate.id),
        "applications_purged": len(applications),
        "documents_purged": len(documents),
        "tokens_revoked": len(tokens),
    }


async def purge_on_request(
    session: AsyncSession,
    user: User,
    *,
    candidate_id: uuid.UUID,
    reason: str = "Candidate requested deletion",
    now: datetime | None = None,
) -> dict:
    """A DPDP erasure request, honoured.

    The consent rows survive deliberately. "Did they consent, and did they later
    withdraw" is the question a regulator asks about an erasure, and deleting the
    record of the request along with the data leaves nothing to answer it with.
    """
    moment = now or datetime.now(UTC)
    candidate = await session.scalar(
        tenant_scoped(select(Candidate).where(Candidate.id == candidate_id), Candidate, user)
    )
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found")
    if candidate.converted_employee_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This candidate became an employee. Their record is now held under "
                "employment law retention, not candidate retention, and cannot be "
                "erased through this route."
            ),
        )

    result = await _purge_candidate(session, user.tenant_id, candidate, note=reason, now=moment)

    # Withdrawal is recorded rather than the consent being deleted.
    for purpose in ConsentPurpose:
        session.add(
            CandidateConsent(
                tenant_id=user.tenant_id,
                candidate_id=candidate.id,
                purpose=purpose,
                granted=False,
                notice_text=reason,
                notice_version="withdrawal",
                recorded_at=moment,
            )
        )

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.DELETE,
        entity_type="candidate",
        entity_id=candidate.id,
        summary=(
            f"Candidate personal data erased on request: {result['applications_purged']} "
            f"application(s), {result['documents_purged']} document(s). Application rows "
            "kept without personal data so hiring statistics do not change."
        ),
    )
    await session.flush()
    return result | {"reason": reason}


async def purge_expired(session: AsyncSession, user: User, *, as_of: date | None = None) -> dict:
    """Scheduled retention purge.

    Runs against `retention_until`, which was computed at ingestion from the
    notice in force then. A candidate who applied under a one-year notice does
    not acquire a two-year retention because the client changed the page.
    """
    today = as_of or date.today()
    moment = datetime.now(UTC)

    due = (
        await session.scalars(
            tenant_scoped(
                select(Application).where(
                    Application.retention_until.is_not(None),
                    Application.retention_until < today,
                    Application.purged_at.is_(None),
                ),
                Application,
                user,
            )
        )
    ).all()

    candidates: dict[uuid.UUID, Candidate] = {}
    for application in due:
        if application.candidate_id in candidates:
            continue
        candidate = await session.get(Candidate, application.candidate_id)
        if candidate is None or candidate.converted_employee_id is not None:
            # Somebody who joined is held under employment retention now.
            continue
        # Only if nothing of theirs is still inside its window. A person with a
        # newer application has not aged out.
        live = await session.scalar(
            tenant_scoped(
                select(Application).where(
                    Application.candidate_id == candidate.id,
                    Application.purged_at.is_(None),
                    Application.retention_until >= today,
                ),
                Application,
                user,
            )
        )
        if live is None:
            candidates[candidate.id] = candidate

    purged = [
        await _purge_candidate(
            session,
            user.tenant_id,
            candidate,
            note=f"Retention period expired on or before {today}.",
            now=moment,
        )
        for candidate in candidates.values()
    ]

    if purged:
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            actor=user,
            action=AuditAction.DELETE,
            entity_type="candidate",
            entity_id=None,
            summary=(
                f"Retention purge: {len(purged)} candidate(s) past their stated retention "
                f"period as of {today}."
            ),
        )
    await session.flush()
    return {
        "as_of": today.isoformat(),
        "applications_past_retention": len(due),
        "candidates_purged": len(purged),
        "detail": purged,
    }
