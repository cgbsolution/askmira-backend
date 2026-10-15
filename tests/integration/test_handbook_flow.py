"""The handbook builder: write in sections, publish to the library, re-acknowledge on change."""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company_documents import CompanyDocument
from app.models.tenant import Tenant
from app.models.user import User
from app.services.documents import handbook
from app.services.documents.handbook import HandbookIn, SectionIn
from tests.integration.conftest import requires_db
from tests.integration.test_acknowledgement_flows import storage_root  # noqa: F401

pytestmark = requires_db


def test_the_body_is_escaped_and_bullets_become_a_list() -> None:
    html = handbook.render_html(
        "Handbook",
        [{"heading": "Leave <rules>", "body": "Plan ahead.\n\n- Casual: 12\n- Sick: 12"}],
        version=1,
    )
    assert "Leave &lt;rules&gt;" in html and "<rules>" not in html
    assert "<ul><li>Casual: 12</li><li>Sick: 12</li></ul>" in html


async def test_publishing_files_a_pdf_and_republishing_replaces_it(
    session: AsyncSession, tenant: Tenant, hr_user: User, storage_root  # noqa: F811
) -> None:
    draft = await handbook.create_handbook(
        session, hr_user, HandbookIn(title="Employee handbook", sections=[])
    )
    handbook_id = uuid.UUID(draft["id"])
    with pytest.raises(HTTPException) as empty:
        await handbook.publish_handbook(session, hr_user, handbook_id)
    assert empty.value.status_code == 422

    await handbook.update_handbook(
        session, hr_user, handbook_id,
        HandbookIn(
            title="Employee handbook",
            sections=[SectionIn(heading="Working hours", body="9:30 to 6:30.")],
        ),
    )
    first = await handbook.publish_handbook(session, hr_user, handbook_id)
    assert first["version"] == 1 and first["has_unpublished_changes"] is False
    document = await session.scalar(
        select(CompanyDocument).where(CompanyDocument.id == uuid.UUID(first["document_id"]))
    )
    assert document.document_type == "Employee handbook" and document.requires_acknowledgement
    first_hash = document.content_sha256

    edited = await handbook.update_handbook(
        session, hr_user, handbook_id,
        HandbookIn(
            title="Employee handbook",
            sections=[SectionIn(heading="Working hours", body="10:00 to 7:00.")],
        ),
    )
    assert edited["has_unpublished_changes"] is True
    second = await handbook.publish_handbook(session, hr_user, handbook_id)
    assert second["version"] == 2 and second["document_id"] == first["document_id"]
    await session.refresh(document)
    assert document.content_sha256 != first_hash, "a changed handbook is a new version to sign"
