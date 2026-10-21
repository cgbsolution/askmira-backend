"""Application ingestion: every source into one shape, one candidate per person.

Two things this module is careful about.

**A repeat applicant is linked, not duplicated.** Candidates here apply
repeatedly — across roles, across boards, over years — and "applied three times,
rejected twice at final round" is the most useful sentence a recruiter can read
before opening a resume. It only exists if applications attach to the person
rather than replacing them.

**An ingest that ran twice does not create two applications.** Boards re-send on
their own schedule, a poll can overlap a webhook, and a retry after a timeout is
normal. The unique constraint on `(tenant, board, external_application_id)` is
the backstop; this module checks first so the second delivery is a no-op with an
explanation rather than an integrity error somebody has to interpret.

Consent is captured at ingestion, and retention is computed at ingestion from
the tenant's policy — stored per row rather than recomputed later, so a policy
change does not silently re-date data that was collected under the old notice.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.hiring import Candidate, CandidateStage, Requisition
from app.models.jobboard import (
    Application,
    ApplicationStatus,
    CandidateConsent,
    CareersPageConfig,
    ConsentPurpose,
    JobBoard,
    JobPosting,
)
from app.models.user import User
from app.services import audit
from app.services.hiring import dedup
from app.services.jobboards.base import RawApplication, normalise_email, normalise_phone

__all__ = [
    "IngestOutcome",
    "candidate_history",
    "ingest_application",
    "ingest_batch",
    "record_consent",
]

#: Fallback where the tenant has not configured a careers page. Two years is a
#: common Indian talent-pool period; it is a stated default, not an indefinite
#: hold, and the notice the candidate sees has to say whatever the number is.
DEFAULT_RETENTION_DAYS = 730


class IngestOutcome:
    """What happened to one application.

    A small class rather than a tuple because callers act differently on each
    branch, and `result[2]` at a call site is how a duplicate ends up counted as
    a new candidate in a report.
    """

    __slots__ = ("application", "candidate", "created_candidate", "duplicate", "note")

    def __init__(
        self,
        *,
        application: Application | None,
        candidate: Candidate | None,
        created_candidate: bool,
        duplicate: bool,
        note: str,
    ) -> None:
        self.application = application
        self.candidate = candidate
        self.created_candidate = created_candidate
        self.duplicate = duplicate
        self.note = note

    def as_dict(self) -> dict:
        return {
            "application_id": str(self.application.id) if self.application else None,
            "candidate_id": str(self.candidate.id) if self.candidate else None,
            "created_candidate": self.created_candidate,
            "duplicate": self.duplicate,
            "note": self.note,
        }


async def _retention_days(session: AsyncSession, user: User) -> int:
    config = await session.scalar(tenant_scoped(select(CareersPageConfig), CareersPageConfig, user))
    return config.resume_retention_days if config else DEFAULT_RETENTION_DAYS


async def _existing_identities(
    session: AsyncSession, user: User, *, email: str | None, phone: str | None
) -> list[dedup.CandidateIdentity]:
    """Candidates who could plausibly be this person.

    Narrowed by email or phone in SQL rather than loading every candidate: a
    tenant with forty thousand candidates would otherwise pull all of them into
    memory on every application, and the ingestion runs on a poll.
    """
    normalised_email = normalise_email(email)
    normalised_phone = normalise_phone(phone)
    if not normalised_email and not normalised_phone:
        return []

    conditions = []
    if normalised_email:
        conditions.append(Candidate.email.ilike(normalised_email))
    if normalised_phone:
        # The same last-ten-digits rule as `normalise_phone`, in SQL.
        #
        # A plain suffix `LIKE '%9876543210'` looks like it would work and does
        # not: a number stored as "+91 98765 43210" has a space inside the last
        # ten digits, so it matches nothing, and the engine above never gets to
        # see the candidate it would have matched. The narrowing query has to
        # apply the same normalisation the rule does, or it silently decides the
        # answer by deciding what the rule is allowed to look at.
        conditions.append(
            func.right(func.regexp_replace(Candidate.phone, r"\D", "", "g"), 10) == normalised_phone
        )

    from sqlalchemy import or_

    rows = await session.scalars(
        tenant_scoped(select(Candidate).where(or_(*conditions)), Candidate, user)
    )
    return [
        dedup.identity_of(c.id, email=c.email, phone=c.phone, full_name=c.full_name)
        for c in rows.all()
    ]


async def ingest_application(
    session: AsyncSession,
    user: User,
    raw: RawApplication,
    *,
    requisition_id: uuid.UUID | None = None,
    consent_purposes: list[ConsentPurpose] | None = None,
    notice_text: str | None = None,
    notice_version: str | None = None,
    source_ip: str | None = None,
    now: datetime | None = None,
) -> IngestOutcome:
    """One application, from any source, landed against the right candidate."""
    moment = now or datetime.now(UTC)

    if not raw.email and not raw.phone:
        return IngestOutcome(
            application=None,
            candidate=None,
            created_candidate=False,
            duplicate=False,
            note=(
                f"{raw.full_name or 'This application'} arrived from {raw.board} with "
                "neither an email nor a phone number. There is no way to contact them "
                "and no way to tell them apart from anyone else, so nothing was created."
            ),
        )

    # ── Already have it? ────────────────────────────────────────────────────
    if raw.external_application_id:
        existing = await session.scalar(
            tenant_scoped(
                select(Application).where(
                    Application.board == JobBoard(raw.board),
                    Application.external_application_id == raw.external_application_id,
                ),
                Application,
                user,
            )
        )
        if existing is not None:
            candidate = await session.get(Candidate, existing.candidate_id)
            return IngestOutcome(
                application=existing,
                candidate=candidate,
                created_candidate=False,
                duplicate=True,
                note=(
                    f"Already ingested from {raw.board} on "
                    f"{existing.applied_at.date()}. Boards re-send on their own schedule; "
                    "nothing was changed."
                ),
            )

    # ── Which requisition? ──────────────────────────────────────────────────
    target_requisition_id = requisition_id
    if target_requisition_id is None and raw.external_job_id:
        posting = await session.scalar(
            tenant_scoped(
                select(JobPosting).where(
                    JobPosting.board == JobBoard(raw.board),
                    JobPosting.external_job_id == raw.external_job_id,
                ),
                JobPosting,
                user,
            )
        )
        if posting is not None:
            target_requisition_id = posting.requisition_id
    if target_requisition_id is None:
        return IngestOutcome(
            application=None,
            candidate=None,
            created_candidate=False,
            duplicate=False,
            note=(
                f"No requisition matches {raw.board} job '{raw.external_job_id}'. The "
                "posting may have been created outside AskMira; nothing was created, "
                "because an application filed against the wrong role is worse than one "
                "held back."
            ),
        )

    requisition = await session.scalar(
        tenant_scoped(
            select(Requisition).where(Requisition.id == target_requisition_id),
            Requisition,
            user,
        )
    )
    if requisition is None:
        return IngestOutcome(
            application=None,
            candidate=None,
            created_candidate=False,
            duplicate=False,
            note="The requisition this application belongs to no longer exists.",
        )

    # ── Which person? ───────────────────────────────────────────────────────
    identities = await _existing_identities(session, user, email=raw.email, phone=raw.phone)
    match = dedup.match_candidate(
        email=raw.email, phone=raw.phone, full_name=raw.full_name, existing=identities
    )

    created = False
    if match.is_match:
        candidate = await session.get(Candidate, uuid.UUID(match.candidate_id))
    else:
        candidate = None

    if candidate is None:
        candidate = Candidate(
            tenant_id=user.tenant_id,
            requisition_id=target_requisition_id,
            full_name=raw.full_name or (raw.email or "").split("@")[0],
            email=(normalise_email(raw.email) or ""),
            phone=raw.phone,
            resume_url=raw.resume_url,
            current_company=raw.current_company,
            current_ctc=raw.current_ctc,
            expected_ctc=raw.expected_ctc,
            total_experience_years=raw.total_experience_years,
            notice_period_days=raw.notice_period_days,
            source=raw.board,
            stage=CandidateStage.APPLIED,
        )
        session.add(candidate)
        await session.flush()
        created = True
    else:
        # A repeat applicant's newer facts are better than their older ones —
        # they have changed jobs, their notice period has changed — but a blank
        # from one board must never wipe what another board supplied.
        for field, value in (
            ("current_company", raw.current_company),
            ("current_ctc", raw.current_ctc),
            ("expected_ctc", raw.expected_ctc),
            ("total_experience_years", raw.total_experience_years),
            ("notice_period_days", raw.notice_period_days),
            ("phone", raw.phone),
            ("resume_url", raw.resume_url),
        ):
            if value not in (None, ""):
                setattr(candidate, field, value)

    retention_days = await _retention_days(session, user)
    application = Application(
        tenant_id=user.tenant_id,
        candidate_id=candidate.id,
        requisition_id=target_requisition_id,
        board=JobBoard(raw.board),
        external_application_id=raw.external_application_id or None,
        status=ApplicationStatus.APPLIED,
        applied_at=raw.applied_at or moment,
        matched_on=match.matched_on or ("ambiguous" if match.needs_a_human else "new"),
        match_reason=match.reason,
        raw_payload=raw.raw,
        # Computed now, from the policy in force now. Recomputing later would
        # re-date data collected under a notice that said something else.
        retention_until=(moment.date() + timedelta(days=retention_days)),
    )
    session.add(application)
    await session.flush()

    for purpose in consent_purposes or []:
        await record_consent(
            session,
            user,
            candidate_id=candidate.id,
            purpose=purpose,
            granted=True,
            notice_text=notice_text or "",
            notice_version=notice_version or "1",
            source_ip=source_ip,
            now=moment,
        )

    if created:
        note = f"New candidate from {raw.board}."
    else:
        prior = await candidate_history(session, user, candidate_id=candidate.id)
        note = f"{match.reason} This is application {len(prior)} for {candidate.full_name}."

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="application",
        entity_id=application.id,
        summary=(f"Application from {raw.board} for {requisition.code}: {note}"),
        changes={"matched_on": application.matched_on, "board": raw.board},
    )
    return IngestOutcome(
        application=application,
        candidate=candidate,
        created_candidate=created,
        duplicate=False,
        note=note,
    )


async def ingest_batch(
    session: AsyncSession,
    user: User,
    applications: list[RawApplication],
    *,
    now: datetime | None = None,
) -> dict:
    """Ingest a poll's worth, reporting every branch separately.

    Counted apart rather than summed, because "40 applications" that turns out
    to be 38 duplicates is a very different day from 40 new people, and a single
    total hides which one it was.
    """
    outcomes = [await ingest_application(session, user, raw, now=now) for raw in applications]
    return {
        "received": len(applications),
        "new_candidates": sum(1 for o in outcomes if o.created_candidate),
        # A re-delivery of something already ingested is not a repeat applicant.
        # Counting it as one would report a poll that re-sent yesterday's forty
        # applications as forty people applying again.
        "linked_to_existing": sum(
            1
            for o in outcomes
            if o.application is not None and not o.created_candidate and not o.duplicate
        ),
        "already_ingested": sum(1 for o in outcomes if o.duplicate),
        "not_created": sum(1 for o in outcomes if o.application is None),
        "notes": [o.note for o in outcomes if o.application is None or o.duplicate],
    }


async def candidate_history(
    session: AsyncSession, user: User, *, candidate_id: uuid.UUID
) -> list[Application]:
    """Everything this person has ever applied for here, oldest first.

    The sentence a recruiter wants before opening a resume.
    """
    rows = await session.scalars(
        tenant_scoped(
            select(Application)
            .where(Application.candidate_id == candidate_id)
            .order_by(Application.applied_at),
            Application,
            user,
        )
    )
    return list(rows.all())


async def record_consent(
    session: AsyncSession,
    user: User,
    *,
    candidate_id: uuid.UUID,
    purpose: ConsentPurpose,
    granted: bool,
    notice_text: str,
    notice_version: str,
    source_ip: str | None = None,
    now: datetime | None = None,
) -> CandidateConsent:
    """Insert-only.

    A withdrawal is a new row with `granted` false, never an edit. "Did they
    consent on the day we processed this" has to stay answerable after they
    withdraw, because that is exactly the question that gets asked.
    """
    consent = CandidateConsent(
        tenant_id=user.tenant_id,
        candidate_id=candidate_id,
        purpose=purpose,
        granted=granted,
        notice_text=notice_text,
        notice_version=notice_version,
        recorded_at=now or datetime.now(UTC),
        source_ip=source_ip,
    )
    session.add(consent)
    await session.flush()
    return consent


async def effective_consent(
    session: AsyncSession, user: User, *, candidate_id: uuid.UUID, purpose: ConsentPurpose
) -> bool:
    """The latest word on one purpose.

    Latest, not any — a granted row from last year does not survive a withdrawal
    from last week.
    """
    latest = await session.scalar(
        tenant_scoped(
            select(CandidateConsent)
            .where(
                CandidateConsent.candidate_id == candidate_id,
                CandidateConsent.purpose == purpose,
            )
            .order_by(CandidateConsent.recorded_at.desc()),
            CandidateConsent,
            user,
        )
    )
    return bool(latest and latest.granted)


def retention_expiry(*, applied_on: date, retention_days: int) -> date:
    return applied_on + timedelta(days=retention_days)
