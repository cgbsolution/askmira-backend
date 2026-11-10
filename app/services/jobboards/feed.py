"""The public job feed a feed-reading board crawls.

A board that lists jobs by reading a feed (its adapter has `FEED = True`) gets
the workspace's LIVE postings on that board, rendered by the adapter itself, at
`/public/careers/{slug}/feeds/{board}.xml`. No board is named here.

Public on purpose — the board's crawler has no login — so it shows only what the
careers page already shows: open requisitions the workspace chose to post to
that board, text and apply link. Never a budget band (`show_salary` stays off).
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_filter
from app.models.hiring import Requisition, RequisitionStatus
from app.models.jobboard import JobBoard, JobPosting, PostingStatus
from app.models.tenant import Tenant
from app.services.hiring import careers
from app.services.jobboards import distribution, registry

__all__ = ["board_feed", "feed_path"]


def feed_path(slug: str, board: JobBoard) -> str:
    """Where the feed is served, relative to the portal."""
    return f"/api/v1/public/careers/{slug}/feeds/{board.value}.xml"


async def board_feed(session: AsyncSession, slug: str, board_value: str) -> str:
    """The feed XML for one board, or 404 when the board reads no feed."""
    try:
        board = JobBoard(board_value)
    except ValueError:
        board = None
    builder = registry.adapter_class(board) if board is not None else None
    if builder is None or not builder.FEED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No feed here.")

    config = await careers._config_by_slug(session, slug)
    tenant = await session.get(Tenant, config.tenant_id)
    rows = (
        await session.execute(
            select(JobPosting, Requisition)
            .join(Requisition, Requisition.id == JobPosting.requisition_id)
            .where(
                tenant_filter(JobPosting, config.tenant_id),
                tenant_filter(Requisition, config.tenant_id),
                JobPosting.board == board,
                JobPosting.status == PostingStatus.LIVE,
                Requisition.status == RequisitionStatus.OPEN,
            )
            .order_by(JobPosting.published_at.desc().nullslast())
        )
    ).all()

    careers_url = distribution.public_careers_url(slug)
    jobs = []
    for posting, requisition in rows:
        apply_url = (posting.accepted_as or {}).get("apply_url") or (
            f"{careers_url}/jobs/{requisition.code}"
        )
        spec = await distribution.requisition_spec(session, requisition, apply_url=apply_url)
        jobs.append((spec, posting.published_at))
    return builder.render_feed(
        jobs, publisher=tenant.name if tenant else "", publisher_url=careers_url
    )
