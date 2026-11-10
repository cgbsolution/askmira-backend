"""What the job boards screen shows: accounts, postings, and what needs a person.

No board is named here — everything iterates the registry — so this module
stays on the right side of the adapter boundary.

**No live board account has ever been exercised from this deployment**, and the
status says so for every board. An account row with credentials is a stored
login, not a verified connection; nothing here claims otherwise.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_secret
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.hiring import Requisition
from app.models.jobboard import JobBoard, JobBoardAccount, JobPosting, PostingStatus
from app.models.user import User
from app.services import audit
from app.services.jobboards import distribution, registry
from app.services.jobboards.feed import feed_path

__all__ = ["attention", "board_status", "list_postings", "save_account"]

_NOT_LIVE = (
    "Not verified with a live account from this deployment yet. A stored account is a "
    "login, not proof of reach: the first post shows the board's own answer, and a refusal "
    "is parked with the board's reason rather than lost."
)


async def board_status(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    accounts = {
        a.board: a
        for a in (
            await session.scalars(tenant_scoped(select(JobBoardAccount), JobBoardAccount, user))
        ).all()
    }
    slug = await distribution.published_slug(session, user)
    out = []
    for board in registry.supported_boards():
        account = accounts.get(board)
        builder = registry.adapter_class(board)
        reads_feed = bool(builder is not None and builder.FEED)
        out.append(
            {
                "board": board.value,
                "account_configured": account is not None,
                "account": (
                    {
                        "id": str(account.id),
                        "display_name": account.display_name,
                        "username": account.username,
                        "has_credentials": bool(account.credentials_encrypted),
                        "is_active": account.is_active,
                        "settings": account.settings,
                        "last_polled_at": (
                            account.last_polled_at.isoformat() if account.last_polled_at else None
                        ),
                        "last_error": account.last_error,
                    }
                    if account
                    else None
                ),
                "live_connection_verified": False,
                "message": _NOT_LIVE,
                "needs_secret": builder.NEEDS_SECRET if builder else True,
                "connection_note": builder.CONNECTION_NOTE if builder else "",
                "feed_path": feed_path(slug, board) if reads_feed and slug else None,
                "reads_feed": reads_feed,
            }
        )
    return out


async def save_account(
    session: AsyncSession,
    user: User,
    *,
    board: JobBoard,
    display_name: str | None,
    username: str | None,
    secret: str,
    settings: dict[str, Any],
    request: Request | None = None,
) -> JobBoardAccount:
    builder = registry.adapter_class(board)
    if builder is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{board.value} is not a job board jobs are posted to.",
        )
    secret = (secret or "").strip()
    if builder.NEEDS_SECRET and not secret:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Enter the API key or password for this board.",
        )
    existing = await session.scalar(
        tenant_scoped(
            select(JobBoardAccount).where(JobBoardAccount.board == board), JobBoardAccount, user
        )
    )
    account = existing or JobBoardAccount(tenant_id=user.tenant_id, board=board)
    account.display_name = display_name
    account.username = username
    account.settings = settings
    account.credentials_encrypted = encrypt_secret(secret, user.tenant_id) if secret else None
    account.is_active = True
    account.last_error = None
    session.add(account)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE if existing else AuditAction.CREATE,
        entity_type="job_board_account",
        entity_id=account.id,
        # The credential itself never reaches the audit log.
        summary=f"Job board account {'updated' if existing else 'added'} for {board.value}",
        request=request,
    )
    return account


async def list_postings(
    session: AsyncSession, user: User, *, requisition_id: uuid.UUID | None
) -> list[dict[str, Any]]:
    stmt = (
        select(JobPosting, Requisition.code, Requisition.title)
        .join(Requisition, Requisition.id == JobPosting.requisition_id)
        .order_by(JobPosting.created_at.desc())
    )
    if requisition_id is not None:
        stmt = stmt.where(JobPosting.requisition_id == requisition_id)
    rows = (await session.execute(tenant_scoped(stmt, JobPosting, user))).all()
    return [
        {
            "id": str(p.id),
            "requisition_id": str(p.requisition_id),
            "requisition_code": code,
            "requisition_title": title,
            "board": p.board.value,
            "external_job_id": p.external_job_id,
            "status": p.status.value,
            "published_at": p.published_at.isoformat() if p.published_at else None,
            "expires_at": p.expires_at.isoformat() if p.expires_at else None,
            "next_refresh_at": p.next_refresh_at.isoformat() if p.next_refresh_at else None,
            "next_attempt_at": p.next_attempt_at.isoformat() if p.next_attempt_at else None,
            "attempt_count": p.attempt_count,
            "last_error": p.last_error,
            "warnings": list(p.warnings or []),
            "view_count": p.view_count,
            "application_count": p.application_count,
        }
        for p, code, title in rows
    ]


async def attention(session: AsyncSession, user: User) -> dict[str, Any]:
    """Postings that are not live and will not become live on their own."""
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
