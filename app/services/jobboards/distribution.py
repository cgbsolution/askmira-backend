"""Publishing requisitions to boards, refreshing them, and never dropping one.

The rule this module exists to hold: **a posting that failed is a row that says
so.** Not a log line, not an exception that propagated somewhere, not an absence.
A recruiter who thinks a job is on Naukri and finds out three weeks later that it
never posted has lost three weeks, and the only thing that prevents it is that
the failure has a place to live where somebody is already looking.

So every path out of `publish_posting` leaves the row in exactly one of four
states — LIVE, PUBLISHING with a retry time, PARKED with the board's own words,
or DRAFT untouched — and there is no fifth.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.hiring import Requisition, RequisitionStatus
from app.models.jobboard import JobBoard, JobBoardAccount, JobPosting, PostingStatus
from app.models.org import Location
from app.models.user import User
from app.services import audit
from app.services.jobboards import backoff, registry
from app.services.jobboards.base import (
    BoardPermanentFailure,
    BoardTransientFailure,
    JobSpec,
    RateLimited,
    Transport,
)

__all__ = [
    "close_posting",
    "due_refreshes",
    "job_spec_for",
    "publish_posting",
    "refresh_posting",
    "retryable_postings",
]


async def job_spec_for(
    session: AsyncSession, user: User, requisition: Requisition, *, apply_url: str | None = None
) -> JobSpec:
    """Our requisition in our own vocabulary.

    Nothing board-shaped here on purpose. The adapters translate; the moment a
    Naukri concept reached this function, every other board's adapter would have
    to decide what to do with a field it has no equivalent for.
    """
    location_name = ""
    state_code = None
    if requisition.location_id:
        location = await session.get(Location, requisition.location_id)
        if location is not None:
            location_name = location.name
            state_code = getattr(location, "state_code", None)

    published = [v for v in (requisition.jd_versions or []) if v.get("body")]
    description = published[-1]["body"] if published else (requisition.description or "")

    return JobSpec(
        requisition_code=requisition.code,
        title=requisition.title,
        description_html=description,
        skills=tuple(str(s) for s in (requisition.skills or [])),
        location=location_name,
        state_code=state_code,
        experience_min_years=requisition.experience_min_years,
        experience_max_years=requisition.experience_max_years,
        ctc_min=(
            Decimal(str(requisition.budget_ctc_min))
            if requisition.budget_ctc_min is not None
            else None
        ),
        ctc_max=(
            Decimal(str(requisition.budget_ctc_max))
            if requisition.budget_ctc_max is not None
            else None
        ),
        # The requisition's budget band is an internal number. Publishing it
        # because it happens to be available would put the client's own salary
        # bands on a public advert.
        show_salary=False,
        employment_type=requisition.employment_type,
        openings=requisition.openings,
        apply_url=apply_url,
        expires_on=requisition.target_close_date,
    )


async def _account(session: AsyncSession, user: User, board: JobBoard) -> JobBoardAccount:
    account = await session.scalar(
        tenant_scoped(
            select(JobBoardAccount).where(
                JobBoardAccount.board == board, JobBoardAccount.is_active.is_(True)
            ),
            JobBoardAccount,
            user,
        )
    )
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"No active {board.value} account is configured for this tenant. Nothing "
                "was posted."
            ),
        )
    return account


async def publish_posting(
    session: AsyncSession,
    user: User,
    *,
    requisition_id: uuid.UUID,
    board: JobBoard,
    transport: Transport | None = None,
    apply_url: str | None = None,
    refresh_interval_days: int | None = None,
    now: datetime | None = None,
    request: Request | None = None,
) -> dict:
    """Post one requisition to one board, or leave a row saying why not."""
    moment = now or datetime.now(UTC)

    requisition = await session.scalar(
        tenant_scoped(
            select(Requisition).where(Requisition.id == requisition_id), Requisition, user
        )
    )
    if requisition is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Requisition not found")
    if requisition.status is not RequisitionStatus.OPEN:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Requisition {requisition.code} is {requisition.status.value}. Advertising "
                "a role that has not been approved is how a company ends up interviewing "
                "for a headcount it does not have."
            ),
        )

    posting = await session.scalar(
        tenant_scoped(
            select(JobPosting).where(
                JobPosting.requisition_id == requisition_id, JobPosting.board == board
            ),
            JobPosting,
            user,
        )
    )
    if posting is None:
        posting = JobPosting(tenant_id=user.tenant_id, requisition_id=requisition_id, board=board)
        session.add(posting)
        await session.flush()

    if posting.status is PostingStatus.LIVE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This requisition is already live on {board.value} as "
                f"{posting.external_job_id}. Update it rather than posting a second time — "
                "two live adverts split the applications and confuse candidates."
            ),
        )

    account = await _account(session, user, board)
    spec = await job_spec_for(session, user, requisition, apply_url=apply_url)
    posting.status = PostingStatus.PUBLISHING
    posting.attempt_count += 1
    await session.flush()

    try:
        adapter = registry.get_adapter(board, transport)
        credentials = registry.credentials_for(account, user.tenant_id)
        result = await adapter.publish_job(spec, credentials)
    except (BoardTransientFailure, BoardPermanentFailure, registry.AdapterUnavailable) as exc:
        return await _handle_failure(
            session, user, posting, exc, moment=moment, account=account, request=request
        )

    posting.status = PostingStatus.LIVE
    posting.external_job_id = result.external_job_id
    posting.published_at = result.published_at
    posting.expires_at = result.expires_at
    posting.warnings = list(result.warnings)
    posting.accepted_as = result.accepted_as
    posting.last_error = None
    posting.next_attempt_at = None

    interval = refresh_interval_days
    if interval is None:
        # Only where the board actually has the concept. A refresh scheduled on
        # a board that cannot refresh would fail forever on a timer.
        interval = _default_refresh_interval(board)
    posting.refresh_interval_days = interval
    posting.next_refresh_at = moment + timedelta(days=interval) if interval else None
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="job_posting",
        entity_id=posting.id,
        summary=(
            f"{requisition.code} published to {board.value} as {result.external_job_id}"
            + (f" with {len(result.warnings)} warning(s)" if result.warnings else "")
        ),
        changes={"warnings": list(result.warnings), "accepted_as": result.accepted_as},
        request=request,
    )
    return {
        "posting_id": str(posting.id),
        "status": posting.status.value,
        "external_job_id": posting.external_job_id,
        "warnings": list(result.warnings),
        "accepted_as": result.accepted_as,
        "next_refresh_at": (
            posting.next_refresh_at.isoformat() if posting.next_refresh_at else None
        ),
    }


def _default_refresh_interval(board: JobBoard) -> int | None:
    """How often to boost, where boosting exists.

    Asked of the adapter class rather than hard-coded per board here, so the
    knowledge stays in the one file that knows about that board.
    """
    adapter_class = registry.adapter_class(board)
    if adapter_class is None:
        return None
    # Read off the adapter rather than branched on here. A board name in this
    # file is the first step to a dozen of them, and the one that gets missed
    # when a board is added is always the error path.
    return getattr(adapter_class, "default_refresh_interval_days", None)


async def _handle_failure(
    session: AsyncSession,
    user: User,
    posting: JobPosting,
    exc: Exception,
    *,
    moment: datetime,
    account: JobBoardAccount | None = None,
    request: Request | None = None,
) -> dict:
    """Turn a board failure into a row somebody will see.

    The one thing that must not happen here is returning quietly.
    """
    permanent = isinstance(exc, BoardPermanentFailure | registry.AdapterUnavailable)
    retry_after = getattr(exc, "retry_after_seconds", None)

    decision = backoff.plan_attempt(
        attempt=posting.attempt_count,
        permanent=permanent,
        message=str(exc),
        now=moment,
        retry_after_seconds=retry_after,
    )

    posting.last_error = decision.reason
    if decision.should_retry:
        posting.status = PostingStatus.PUBLISHING
        posting.next_attempt_at = decision.next_attempt_at
    else:
        posting.status = PostingStatus.PARKED
        posting.next_attempt_at = None

    if account is not None:
        account.last_error = str(exc)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="job_posting",
        entity_id=posting.id,
        summary=(
            f"Posting to {posting.board.value} "
            f"{'parked for a human' if decision.needs_a_human else 'will retry'}: {exc}"
        ),
        request=request,
    )
    return {
        "posting_id": str(posting.id),
        "status": posting.status.value,
        "external_job_id": posting.external_job_id,
        "error": str(exc),
        "reason": decision.reason,
        "rate_limited": isinstance(exc, RateLimited),
        "next_attempt_at": (
            decision.next_attempt_at.isoformat() if decision.next_attempt_at else None
        ),
        "needs_a_human": decision.needs_a_human,
    }


async def refresh_posting(
    session: AsyncSession,
    user: User,
    *,
    posting_id: uuid.UUID,
    transport: Transport | None = None,
    now: datetime | None = None,
) -> dict:
    """Boost a posting back up the board's listing.

    Naukri postings decay in listing position within days and Indian clients
    expect this as part of what they pay the board for.
    """
    moment = now or datetime.now(UTC)
    posting = await session.scalar(
        tenant_scoped(select(JobPosting).where(JobPosting.id == posting_id), JobPosting, user)
    )
    if posting is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Posting not found")
    if posting.status is not PostingStatus.LIVE or not posting.external_job_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This posting is {posting.status.value}; there is nothing live to refresh.",
        )

    account = await _account(session, user, posting.board)
    try:
        adapter = registry.get_adapter(posting.board, transport)
        credentials = registry.credentials_for(account, user.tenant_id)
        await adapter.refresh_job(posting.external_job_id, credentials)
    except (BoardTransientFailure, BoardPermanentFailure, registry.AdapterUnavailable) as exc:
        posting.attempt_count += 1
        return await _handle_failure(session, user, posting, exc, moment=moment, account=account)

    posting.last_refreshed_at = moment
    posting.next_refresh_at = (
        moment + timedelta(days=posting.refresh_interval_days)
        if posting.refresh_interval_days
        else None
    )
    posting.last_error = None
    await session.flush()
    return {
        "posting_id": str(posting.id),
        "last_refreshed_at": moment.isoformat(),
        "next_refresh_at": (
            posting.next_refresh_at.isoformat() if posting.next_refresh_at else None
        ),
    }


async def close_posting(
    session: AsyncSession,
    user: User,
    *,
    posting_id: uuid.UUID,
    transport: Transport | None = None,
) -> dict:
    """Take a job down.

    A posting left live after the role is filled keeps collecting applications
    from people who have no chance, which is the single cheapest way to make a
    company look like it does not respect candidates' time.
    """
    posting = await session.scalar(
        tenant_scoped(select(JobPosting).where(JobPosting.id == posting_id), JobPosting, user)
    )
    if posting is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Posting not found")

    if posting.external_job_id:
        account = await _account(session, user, posting.board)
        try:
            adapter = registry.get_adapter(posting.board, transport)
            credentials = registry.credentials_for(account, user.tenant_id)
            await adapter.close_job(posting.external_job_id, credentials)
        except (BoardTransientFailure, BoardPermanentFailure, registry.AdapterUnavailable) as exc:
            posting.attempt_count += 1
            return await _handle_failure(
                session, user, posting, exc, moment=datetime.now(UTC), account=account
            )

    posting.status = PostingStatus.CLOSED
    posting.next_refresh_at = None
    posting.next_attempt_at = None
    await session.flush()
    return {"posting_id": str(posting.id), "status": posting.status.value}


async def due_refreshes(
    session: AsyncSession, user: User, *, as_of: datetime | None = None
) -> list[JobPosting]:
    moment = as_of or datetime.now(UTC)
    rows = await session.scalars(
        tenant_scoped(
            select(JobPosting).where(
                JobPosting.status == PostingStatus.LIVE,
                JobPosting.next_refresh_at.is_not(None),
                JobPosting.next_refresh_at <= moment,
            ),
            JobPosting,
            user,
        )
    )
    return list(rows.all())


async def retryable_postings(
    session: AsyncSession, user: User, *, as_of: datetime | None = None
) -> list[JobPosting]:
    """Postings whose retry time has come.

    Deliberately excludes PARKED. A parked posting is waiting for a person to
    change something; retrying it on a timer would spin forever against a value
    the board has already refused.
    """
    moment = as_of or datetime.now(UTC)
    rows = await session.scalars(
        tenant_scoped(
            select(JobPosting).where(
                JobPosting.status == PostingStatus.PUBLISHING,
                JobPosting.next_attempt_at.is_not(None),
                JobPosting.next_attempt_at <= moment,
            ),
            JobPosting,
            user,
        )
    )
    return list(rows.all())
