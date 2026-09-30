"""The company document library: policies and handbooks.

HR files them; everyone reads the ones published to employees. Files go
through the same storage and the same byte-sniffing check as the vault, and
every change and every download is audited in the same transaction.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import date

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.company_documents import CompanyDocument
from app.models.user import User
from app.schemas.documents import CompanyDocumentRead, CompanyDocumentUpdate
from app.services import audit
from app.services.attendance.service import local_today
from app.services.documents.vault import _check_dates, _checked, _clean, _download_name
from app.services.storage import StorageError, get_storage

__all__ = [
    "delete_company_document",
    "download_company_document",
    "list_company_documents",
    "update_company_document",
    "upload_company_document",
]

_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")


def _read(row: CompanyDocument, *, today: date) -> CompanyDocumentRead:
    read = CompanyDocumentRead.model_validate(row)
    read.is_expired = row.valid_until is not None and row.valid_until < today
    return read


async def _get(session: AsyncSession, user: User, document_id: uuid.UUID) -> CompanyDocument:
    row = await session.scalar(
        tenant_scoped(
            select(CompanyDocument).where(CompanyDocument.id == document_id),
            CompanyDocument,
            user,
        )
    )
    # Unpublished documents do not exist for anyone outside HR.
    if row is None or (not permissions.is_hr(user) and not row.is_employee_visible):
        raise _NOT_FOUND
    return row


async def list_company_documents(
    session: AsyncSession, user: User, *, document_type: str | None = None
) -> list[CompanyDocumentRead]:
    """HR: every company document. Everyone else: the published ones."""
    stmt = select(CompanyDocument).order_by(CompanyDocument.document_type, CompanyDocument.title)
    if not permissions.is_hr(user):
        stmt = stmt.where(CompanyDocument.is_employee_visible.is_(True))
    if document_type:
        stmt = stmt.where(CompanyDocument.document_type == document_type)
    rows = (await session.scalars(tenant_scoped(stmt, CompanyDocument, user))).all()
    today = await local_today(session, user)
    return [_read(row, today=today) for row in rows]


async def upload_company_document(
    session: AsyncSession,
    user: User,
    *,
    document_type: str,
    title: str,
    data: bytes,
    document_number: str | None = None,
    remark: str | None = None,
    issued_on: date | None = None,
    valid_until: date | None = None,
    is_employee_visible: bool = True,
    request: Request | None = None,
) -> CompanyDocumentRead:
    permissions.require_hr(user, detail="Only HR can add company documents.")
    _check_dates(issued_on, valid_until)
    kind = _checked(data)

    storage = get_storage()
    key = storage.new_key(user.tenant_id, "company-documents", kind.extension)
    try:
        await storage.put(key, data)
    except StorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    row = CompanyDocument(
        tenant_id=user.tenant_id,
        document_type=document_type.strip(),
        title=title.strip(),
        document_number=_clean(document_number),
        remark=_clean(remark),
        file_url=key,
        file_size_bytes=len(data),
        mime_type=kind.mime_type,
        issued_on=issued_on,
        valid_until=valid_until,
        is_employee_visible=is_employee_visible,
        uploaded_by_user_id=user.id,
    )
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="company_document",
        entity_id=row.id,
        actor=user,
        changes={
            "document_type": row.document_type,
            "title": row.title,
            "published": is_employee_visible,
        },
        summary=f"Added company document '{row.title}' ({row.document_type})",
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return _read(row, today=await local_today(session, user))


async def update_company_document(
    session: AsyncSession,
    user: User,
    document_id: uuid.UUID,
    payload: CompanyDocumentUpdate,
    *,
    request: Request | None = None,
) -> CompanyDocumentRead:
    permissions.require_hr(user, detail="Only HR can change company documents.")
    row = await _get(session, user, document_id)
    changes = payload.model_dump(exclude_unset=True)
    for key in ("document_type", "title", "is_employee_visible"):
        if key in changes and changes[key] is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"'{key}' cannot be cleared.",
            )
    for key in ("document_number", "remark"):
        if key in changes:
            changes[key] = _clean(changes[key])
    _check_dates(
        changes.get("issued_on", row.issued_on), changes.get("valid_until", row.valid_until)
    )
    diff = {
        key: {"from": str(getattr(row, key)), "to": str(value)}
        for key, value in changes.items()
        if getattr(row, key) != value
    }
    for key, value in changes.items():
        setattr(row, key, value)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="company_document",
        entity_id=row.id,
        actor=user,
        changes=diff,
        summary=f"Updated company document '{row.title}'",
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return _read(row, today=await local_today(session, user))


async def delete_company_document(
    session: AsyncSession, user: User, document_id: uuid.UUID, *, request: Request | None = None
) -> None:
    permissions.require_hr(user, detail="Only HR can remove company documents.")
    row = await _get(session, user, document_id)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.DELETE,
        entity_type="company_document",
        entity_id=row.id,
        actor=user,
        changes={"document_type": row.document_type, "title": row.title},
        summary=f"Removed company document '{row.title}'",
        request=request,
    )
    key = row.file_url
    await session.delete(row)
    await session.commit()
    # The record is gone; an orphaned file is a clean-up job, not an error to
    # show the person who removed it.
    with contextlib.suppress(StorageError):
        await get_storage().delete(key)


async def download_company_document(
    session: AsyncSession, user: User, document_id: uuid.UUID, *, request: Request | None = None
) -> tuple[str, str, bytes]:
    """The file, a safe name and its type. Audited: who took a copy, and when."""
    row = await _get(session, user, document_id)
    try:
        data = await get_storage().get(row.file_url)
    except StorageError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if data is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The stored file for this document is missing. Upload it again.",
        )
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.EXPORT,
        entity_type="company_document",
        entity_id=row.id,
        actor=user,
        summary=f"Downloaded company document '{row.title}'",
        request=request,
    )
    await session.commit()
    mime = row.mime_type or "application/octet-stream"
    return _download_name(row.title, mime), mime, data
