"""The job feed a feed-reading board (Indeed) crawls, end to end.

Adding the board needs no password, posting puts the job in the feed with the
careers-page apply link, the public feed lists exactly the live postings of an
open requisition, and never the budget band.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.hiring import Requisition, RequisitionStatus
from app.models.jobboard import CareersPageConfig, JobBoard, PostingStatus
from app.models.tenant import Tenant
from app.models.user import User
from app.services.jobboards import distribution, overview
from app.services.jobboards import feed as board_feed
from tests.hiring.db_fixture import engine  # noqa: F401 - shadows the shared fixture
from tests.integration.conftest import requires_db

pytestmark = requires_db


@pytest.fixture(autouse=True)
def _portal_address(monkeypatch):
    monkeypatch.setattr(settings, "WEB_APP_URL", "https://hr.example.com")


async def _requisition(session: AsyncSession, tenant: Tenant, code: str) -> Requisition:
    requisition = Requisition(
        tenant_id=tenant.id,
        code=code,
        title="Payroll Executive & Analyst",
        openings=1,
        grade="L3",
        status=RequisitionStatus.OPEN,
        description="<p>Run payroll &amp; statutory.</p>",
        skills=["Payroll"],
        justification="Growth.",
        experience_min_years=2,
        experience_max_years=5,
        budget_ctc_min=Decimal("600000"),
        budget_ctc_max=Decimal("900000"),
    )
    session.add(requisition)
    await session.commit()
    await session.refresh(requisition)
    return requisition


async def _careers(session: AsyncSession, tenant: Tenant, *, published: bool = True) -> None:
    session.add(
        CareersPageConfig(
            tenant_id=tenant.id,
            slug="acme-feed",
            is_published=published,
            privacy_notice_html="<p>Notice.</p>",
            privacy_notice_version="2026-01",
            resume_retention_days=365,
        )
    )
    await session.commit()


async def test_indeed_needs_no_password_and_its_feed_lists_live_jobs(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    await _careers(session, tenant)
    await overview.save_account(
        session,
        hr_user,
        board=JobBoard.INDEED,
        display_name="Acme",
        username=None,
        secret="",
        settings={},
    )
    await session.commit()

    live = await _requisition(session, tenant, "REQ-F1")
    closed = await _requisition(session, tenant, "REQ-F2")

    result = await distribution.publish_posting(
        session, hr_user, requisition_id=live.id, board=JobBoard.INDEED
    )
    await session.commit()
    assert result["status"] == PostingStatus.LIVE.value
    assert any("feed" in w for w in result["warnings"])

    second = await distribution.publish_posting(
        session, hr_user, requisition_id=closed.id, board=JobBoard.INDEED
    )
    await session.commit()
    await distribution.close_posting(session, hr_user, posting_id=second["posting_id"])
    await session.commit()

    xml = await board_feed.board_feed(session, "acme-feed", "indeed")
    root = ET.fromstring(xml)
    codes = [job.findtext("referencenumber") for job in root.findall("job")]
    assert codes == ["REQ-F1"]
    job = root.find("job")
    assert job.findtext("url") == "https://hr.example.com/careers/acme-feed/jobs/REQ-F1"
    assert job.findtext("title") == "Payroll Executive & Analyst"
    # The budget band is internal and never reaches a public feed.
    assert root.find("job/salary") is None
    assert "600000" not in xml and "900000" not in xml

    status_rows = await overview.board_status(session, hr_user)
    indeed = next(r for r in status_rows if r["board"] == "indeed")
    assert indeed["needs_secret"] is False and indeed["reads_feed"] is True
    assert indeed["feed_path"] == "/api/v1/public/careers/acme-feed/feeds/indeed.xml"
    naukri = next(r for r in status_rows if r["board"] == "naukri")
    assert naukri["needs_secret"] is True and naukri["feed_path"] is None


async def test_a_board_that_is_sent_jobs_still_needs_its_password(
    session: AsyncSession, hr_user: User
) -> None:
    with pytest.raises(HTTPException) as caught:
        await overview.save_account(
            session,
            hr_user,
            board=JobBoard.NAUKRI,
            display_name=None,
            username=None,
            secret="  ",
            settings={},
        )
    assert caught.value.status_code == 422


async def test_no_feed_for_an_unpublished_page_or_a_board_that_reads_none(
    session: AsyncSession, tenant: Tenant
) -> None:
    await _careers(session, tenant, published=False)
    for board in ("indeed", "naukri", "nonsense"):
        with pytest.raises(HTTPException) as caught:
            await board_feed.board_feed(session, "acme-feed", board)
        assert caught.value.status_code == 404


async def test_naukri_without_its_api_address_parks_the_posting_with_the_reason(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """The live transport is wired: without the address Naukri issues, the post
    is refused before anything is sent, and the posting says why."""
    await _careers(session, tenant)
    await overview.save_account(
        session,
        hr_user,
        board=JobBoard.NAUKRI,
        display_name=None,
        username="r",
        secret="key",
        settings={},
    )
    await session.commit()
    requisition = await _requisition(session, tenant, "REQ-F3")
    result = await distribution.publish_posting(
        session, hr_user, requisition_id=requisition.id, board=JobBoard.NAUKRI
    )
    await session.commit()
    assert result["status"] == PostingStatus.PARKED.value
    assert "API address" in result["error"]
    assert result["needs_a_human"] is True
