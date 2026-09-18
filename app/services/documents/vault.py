"""The employee document vault.

**Who sees what.** HR sees and manages every document in the tenant. An employee
sees their own documents that HR has marked visible, and nothing else — a
document they may not see is a 404 to them, not a 403, so its existence is not
confirmed either. Managers get no special view: a reportee's ID proof or medical
certificate is not a manager's business.

**Nobody manages their own record through HR's path.** An HR admin cannot upload
to, edit or delete from their own vault; another HR admin has to. A warning
letter quietly removed from one's own file is exactly the case this stops.

**The file never trusts the client.** Size and type are checked server-side from
the bytes, the storage key is generated, and the stored file is written before
the row and removed again if the row does not commit.
"""

from __future__ import annotations

import contextlib
import re
import uuid
from datetime import date, timedelta

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.config import settings
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee, EmployeeDocument
from app.models.user import User
from app.schemas.documents import DocumentCategory, DocumentRead, DocumentUpdate
from app.services import audit
from app.services.storage import FileRejectedError, StorageError, get_storage, validate_upload

__all__ = [
    "delete_document",
    "download_document",
    "expiring_documents",
    "list_documents",
    "update_document",
    "upload_document",
]

_EXTENSIONS = {"application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg"}

_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")


def _label(employee: Employee) -> str:
    return employee.full_name


def _read(document: EmployeeDocument, employee: Employee, *, today: date) -> DocumentRead:
    return DocumentRead(
        id=document.id,
        employee_id=document.employee_id,
        employee_name=_label(employee),
        employee_code=employee.employee_code,
        category=document.category,
        title=document.title,
        mime_type=document.mime_type,
        file_size_bytes=document.file_size_bytes,
        valid_until=document.valid_until,
        is_expired=document.valid_until is not None and document.valid_until < today,
        is_employee_visible=document.is_employee_visible,
        uploaded_by_user_id=document.uploaded_by_user_id,
        created_at=document.created_at,
    )


def _refuse_own_record(user: User, employee_id: uuid.UUID) -> None:
    if user.employee_id is not None and employee_id == user.employee_id:
        raise permissions.forbidden(
            "Documents on your own record are managed by another HR admin, not by you."
        )


async def _employee(session: AsyncSession, user: User, employee_id: uuid.UUID) -> Employee:
    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
    )
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
    return employee


async def _document(
    session: AsyncSession, user: User, document_id: uuid.UUID
) -> tuple[EmployeeDocument, Employee]:
    row = (
        await session.execute(
            tenant_scoped(
                select(EmployeeDocument, Employee)
                .join(Employee, Employee.id == EmployeeDocument.employee_id)
                .where(EmployeeDocument.id == document_id),
                EmployeeDocument,
                user,
            )
        )
    ).first()
    if row is None:
        raise _NOT_FOUND
    return row[0], row[1]


# ═══════════════════════════════════════════════════════════════════════════
# Reads
# ═══════════════════════════════════════════════════════════════════════════


async def list_documents(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID | None = None,
    category: DocumentCategory | None = None,
) -> list[DocumentRead]:
    """HR: anybody's, or everybody's. Everyone else: their own visible ones."""
    stmt = tenant_scoped(
        select(EmployeeDocument, Employee)
        .join(Employee, Employee.id == EmployeeDocument.employee_id)
        .order_by(EmployeeDocument.created_at.desc()),
        EmployeeDocument,
        user,
    )
    if permissions.is_hr(user):
        if employee_id is not None:
            stmt = stmt.where(EmployeeDocument.employee_id == employee_id)
    else:
        own = permissions.resolve_employee(
            user, employee_id, detail="You can only see your own documents."
        )
        stmt = stmt.where(
            EmployeeDocument.employee_id == own,
            EmployeeDocument.is_employee_visible.is_(True),
        )
    if category is not None:
        stmt = stmt.where(EmployeeDocument.category == category.value)

    today = date.today()
    rows = (await session.execute(stmt)).all()
    return [_read(document, employee, today=today) for document, employee in rows]


async def expiring_documents(
    session: AsyncSession, user: User, *, within_days: int = 30
) -> list[DocumentRead]:
    """Documents already expired or expiring soon, soonest first (HR)."""
    permissions.require_hr(user, detail="Only HR can review expiring documents.")
    today = date.today()
    stmt = tenant_scoped(
        select(EmployeeDocument, Employee)
        .join(Employee, Employee.id == EmployeeDocument.employee_id)
        .where(
            EmployeeDocument.valid_until.is_not(None),
            EmployeeDocument.valid_until <= today + timedelta(days=within_days),
            Employee.deleted_at.is_(None),
        )
        .order_by(EmployeeDocument.valid_until),
        EmployeeDocument,
        user,
    )
    rows = (await session.execute(stmt)).all()
    return [_read(document, employee, today=today) for document, employee in rows]


async def download_document(
    session: AsyncSession, user: User, document_id: uuid.UUID, *, request: Request | None = None
) -> tuple[str, str, bytes]:
    """The file, its safe download name and its sniffed type.

    A document the caller may not see is reported as missing. Every download is
    audited: "who took a copy of this ID proof" is a question somebody asks.
    """
    document, employee = await _document(session, user, document_id)
    if not permissions.is_hr(user) and (
        document.employee_id != user.employee_id or not document.is_employee_visible
    ):
        raise _NOT_FOUND

    try:
        data = await get_storage().get(document.file_url)
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
        entity_type="employee_document",
        entity_id=document.id,
        actor=user,
        summary=f"Downloaded '{document.title}' for {employee.employee_code}",
        request=request,
    )
    await session.commit()

    mime = document.mime_type or "application/octet-stream"
    return _download_name(document.title, mime), mime, data


def _download_name(title: str, mime: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9 ._\-]+", "_", title).strip(" ._") or "document"
    extension = _EXTENSIONS.get(mime, "")
    if stem.lower().endswith(extension):
        stem = stem[: -len(extension)] if extension else stem
    return f"{stem[:80]}{extension}"


# ═══════════════════════════════════════════════════════════════════════════
# Mutations (HR)
# ═══════════════════════════════════════════════════════════════════════════


async def upload_document(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    category: DocumentCategory,
    title: str,
    data: bytes,
    valid_until: date | None = None,
    is_employee_visible: bool = True,
    request: Request | None = None,
) -> DocumentRead:
    permissions.require_hr(user, detail="Only HR can upload to an employee's vault.")
    _refuse_own_record(user, employee_id)
    employee = await _employee(session, user, employee_id)

    if len(data) > settings.MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"The file is larger than the {settings.MAX_UPLOAD_BYTES // (1024 * 1024)} MB "
                "limit."
            ),
        )
    try:
        kind = validate_upload(data, max_bytes=settings.MAX_UPLOAD_BYTES)
    except FileRejectedError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)
        ) from exc

    storage = get_storage()
    key = storage.new_key(user.tenant_id, "documents", kind.extension)
    try:
        await storage.put(key, data)
    except StorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    try:
        document = EmployeeDocument(
            tenant_id=user.tenant_id,
            employee_id=employee.id,
            category=category.value,
            title=title.strip(),
            file_url=key,
            file_size_bytes=len(data),
            mime_type=kind.mime_type,
            valid_until=valid_until,
            is_employee_visible=is_employee_visible,
            uploaded_by_user_id=user.id,
        )
        session.add(document)
        await session.flush()
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.CREATE,
            entity_type="employee_document",
            entity_id=document.id,
            actor=user,
            changes={
                "employee_code": employee.employee_code,
                "category": category.value,
                "title": document.title,
                "mime_type": kind.mime_type,
                "size_bytes": len(data),
                "employee_visible": is_employee_visible,
            },
            summary=f"Uploaded '{document.title}' to {employee.employee_code}'s vault",
            request=request,
        )
        await session.commit()
    except BaseException:
        # The row did not land, so the file must not linger unreferenced. A
        # storage failure here must not replace the exception that got us here.
        with contextlib.suppress(StorageError):
            await storage.delete(key)
        raise

    await session.refresh(document)
    return _read(document, employee, today=date.today())


async def update_document(
    session: AsyncSession,
    user: User,
    document_id: uuid.UUID,
    payload: DocumentUpdate,
    *,
    request: Request | None = None,
) -> DocumentRead:
    permissions.require_hr(user, detail="Only HR can change a vault document.")
    document, employee = await _document(session, user, document_id)
    _refuse_own_record(user, document.employee_id)

    changes = payload.model_dump(exclude_unset=True)
    if "category" in changes and changes["category"] is not None:
        changes["category"] = changes["category"].value
    for key in ("title", "category", "is_employee_visible"):
        if key in changes and changes[key] is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"'{key}' cannot be cleared.",
            )

    diff = {
        key: {"from": str(getattr(document, key)), "to": str(value)}
        for key, value in changes.items()
        if getattr(document, key) != value
    }
    for key, value in changes.items():
        setattr(document, key, value)

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="employee_document",
        entity_id=document.id,
        actor=user,
        changes=diff,
        summary=f"Updated '{document.title}' in {employee.employee_code}'s vault",
        request=request,
    )
    await session.commit()
    await session.refresh(document)
    return _read(document, employee, today=date.today())


async def delete_document(
    session: AsyncSession, user: User, document_id: uuid.UUID, *, request: Request | None = None
) -> None:
    """Remove a document. The audit entry keeps what it was and who removed it."""
    permissions.require_hr(user, detail="Only HR can delete a vault document.")
    document, employee = await _document(session, user, document_id)
    _refuse_own_record(user, document.employee_id)

    key = document.file_url
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.DELETE,
        entity_type="employee_document",
        entity_id=document.id,
        actor=user,
        changes={
            "employee_code": employee.employee_code,
            "category": document.category,
            "title": document.title,
        },
        summary=f"Deleted '{document.title}' from {employee.employee_code}'s vault",
        request=request,
    )
    await session.delete(document)
    await session.commit()

    # After the commit: a failed commit must leave the file for the row that
    # still points at it.
    with contextlib.suppress(StorageError):
        await get_storage().delete(key)
