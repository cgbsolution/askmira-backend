"""The handbook builder: sections written in the app, published as a library PDF.

A section is a heading and plain text. Blank lines separate paragraphs and lines
starting "- " are bullets; nothing else is interpreted, so what HR types is what
employees read. Publishing renders the whole handbook to one PDF, files it as a
company document (published to employees), and, when the handbook needs
acknowledging, asks everyone to read the new version.
"""

from __future__ import annotations

import contextlib
import hashlib
import uuid
from datetime import UTC, datetime
from html import escape
from typing import Any

from fastapi import HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.company_documents import CompanyDocument, Handbook
from app.models.tenant import Tenant
from app.models.user import User
from app.services import audit
from app.services.documents import company
from app.services.documents.pdf import Letterhead, PdfRenderError, render_pdf
from app.services.storage import StorageError, get_storage

__all__ = [
    "HandbookIn",
    "SectionIn",
    "create_handbook",
    "get_handbook",
    "list_handbooks",
    "publish_handbook",
    "render_html",
    "update_handbook",
]

DOCUMENT_TYPE = "Employee handbook"


class SectionIn(BaseModel):
    heading: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=20000)


class HandbookIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    sections: list[SectionIn] = Field(default_factory=list, max_length=100)
    requires_acknowledgement: bool = True


def _read(row: Handbook) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "title": row.title,
        "sections": list(row.sections or []),
        "requires_acknowledgement": row.requires_acknowledgement,
        "document_id": str(row.document_id) if row.document_id else None,
        "version": row.version,
        "published_at": row.published_at.isoformat() if row.published_at else None,
        "has_unpublished_changes": row.has_unpublished_changes,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _body_html(body: str) -> str:
    blocks = [b for b in body.replace("\r\n", "\n").split("\n\n") if b.strip()]
    out = []
    for block in blocks:
        lines = [line.rstrip() for line in block.split("\n") if line.strip()]
        if all(line.lstrip().startswith("- ") for line in lines):
            items = "".join(f"<li>{escape(line.lstrip()[2:])}</li>" for line in lines)
            out.append(f"<ul>{items}</ul>")
        else:
            out.append("<p>" + "<br/>".join(escape(line) for line in lines) + "</p>")
    return "".join(out)


def render_html(title: str, sections: list[dict[str, str]], *, version: int) -> str:
    """The handbook's body, contents first, as the PDF lays it out."""
    contents = "".join(
        f"<li>{escape(s.get('heading', ''))}</li>" for s in sections
    )
    parts = [
        f"<h1>{escape(title)}</h1>",
        f"<p style=\"color:#6b788d\">Version {version}</p>",
        f"<h3>Contents</h3><ol>{contents}</ol>" if sections else "",
    ]
    for number, section in enumerate(sections, start=1):
        parts.append(f"<h2>{number}. {escape(section.get('heading', ''))}</h2>")
        parts.append(_body_html(section.get("body", "")))
    return "".join(parts)


def _clean_sections(sections: list[SectionIn]) -> list[dict[str, str]]:
    return [{"heading": s.heading.strip(), "body": s.body.strip()} for s in sections]


async def list_handbooks(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    permissions.require_hr(user, detail="Handbooks are written by HR.")
    rows = (
        await session.scalars(
            tenant_scoped(select(Handbook).order_by(Handbook.title), Handbook, user)
        )
    ).all()
    return [_read(row) for row in rows]


async def _get(session: AsyncSession, user: User, handbook_id: uuid.UUID) -> Handbook:
    permissions.require_hr(user, detail="Handbooks are written by HR.")
    row = await session.scalar(
        tenant_scoped(select(Handbook).where(Handbook.id == handbook_id), Handbook, user)
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Handbook not found")
    return row


async def get_handbook(session: AsyncSession, user: User, handbook_id: uuid.UUID) -> dict:
    return _read(await _get(session, user, handbook_id))


async def create_handbook(
    session: AsyncSession, user: User, payload: HandbookIn, *, request: Request | None = None
) -> dict[str, Any]:
    permissions.require_hr(user, detail="Handbooks are written by HR.")
    row = Handbook(
        tenant_id=user.tenant_id,
        title=payload.title.strip(),
        sections=_clean_sections(payload.sections),
        requires_acknowledgement=payload.requires_acknowledgement,
        updated_by_user_id=user.id,
    )
    session.add(row)
    await session.flush()
    await audit.record(
        session, tenant_id=user.tenant_id, action=AuditAction.CREATE, entity_type="handbook",
        entity_id=row.id, actor=user, changes={"title": row.title,
        "sections": len(row.sections)}, summary=f"Started the handbook '{row.title}'",
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return _read(row)


async def update_handbook(
    session: AsyncSession,
    user: User,
    handbook_id: uuid.UUID,
    payload: HandbookIn,
    *,
    request: Request | None = None,
) -> dict[str, Any]:
    """Save the draft. Employees keep reading the published version until it is republished."""
    row = await _get(session, user, handbook_id)
    row.title = payload.title.strip()
    row.sections = _clean_sections(payload.sections)
    row.requires_acknowledgement = payload.requires_acknowledgement
    row.has_unpublished_changes = True
    row.updated_by_user_id = user.id
    await audit.record(
        session, tenant_id=user.tenant_id, action=AuditAction.UPDATE, entity_type="handbook",
        entity_id=row.id, actor=user, changes={"title": row.title,
        "sections": len(row.sections)}, summary=f"Edited the handbook '{row.title}'",
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return _read(row)


async def publish_handbook(
    session: AsyncSession, user: User, handbook_id: uuid.UUID, *, request: Request | None = None
) -> dict[str, Any]:
    """Render the draft and put it in the library, replacing the last published version."""
    row = await _get(session, user, handbook_id)
    sections = list(row.sections or [])
    if not sections:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Add at least one section before publishing.",
        )
    version = row.version + 1
    tenant = await session.get(Tenant, user.tenant_id)
    try:
        data = render_pdf(
            merged_html=render_html(row.title, sections, version=version),
            reference_number=f"{row.title[:40]} v{version}",
            letterhead=Letterhead(company_name=escape(tenant.name if tenant else "")),
        )
    except PdfRenderError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    storage = get_storage()
    key = storage.new_key(user.tenant_id, "company-documents", ".pdf")
    try:
        await storage.put(key, data)
    except StorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    document = (
        await session.scalar(
            tenant_scoped(
                select(CompanyDocument).where(CompanyDocument.id == row.document_id),
                CompanyDocument,
                user,
            )
        )
        if row.document_id
        else None
    )
    old_key = document.file_url if document else None
    if document is None:
        document = CompanyDocument(
            tenant_id=user.tenant_id,
            document_type=DOCUMENT_TYPE,
            title=row.title,
            file_url=key,
            uploaded_by_user_id=user.id,
        )
        session.add(document)
    document.title = row.title
    document.file_url = key
    document.file_size_bytes = len(data)
    document.mime_type = "application/pdf"
    document.content_sha256 = hashlib.sha256(data).hexdigest()
    document.is_employee_visible = True
    document.requires_acknowledgement = row.requires_acknowledgement
    document.issued_on = datetime.now(UTC).date()
    document.document_number = f"v{version}"
    await session.flush()

    row.document_id = document.id
    row.version = version
    row.published_at = datetime.now(UTC)
    row.has_unpublished_changes = False
    await audit.record(
        session, tenant_id=user.tenant_id, action=AuditAction.UPDATE, entity_type="handbook",
        entity_id=row.id, actor=user,
        changes={"version": version, "document_id": str(document.id),
                 "content_sha256": document.content_sha256},
        summary=f"Published version {version} of the handbook '{row.title}'",
        request=request,
    )
    if row.requires_acknowledgement:
        await company._ask_everyone(session, user, document)
    await session.commit()
    if old_key and old_key != key:
        with contextlib.suppress(StorageError):
            await storage.delete(old_key)
    await session.refresh(row)
    return _read(row)
