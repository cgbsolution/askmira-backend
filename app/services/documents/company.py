"""The company document library: policies and handbooks.

HR files them; everyone reads the ones published to employees. Files go
through the same storage and the same byte-sniffing check as the vault, and
every change and every download is audited in the same transaction.

A document can require acknowledgement — the POSH policy, the code of conduct.
Every active employee is then asked to read it and acknowledge it by typing
their own name. The acknowledgement records the SHA-256 of the file it was
given, so it attests to exactly that file: replace the file and everybody is
asked again. HR sees a register of who has and who has not.
"""

from __future__ import annotations

import contextlib
import hashlib
import uuid
from datetime import UTC, date, datetime

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.company_documents import CompanyDocument, CompanyDocumentAcknowledgement
from app.models.employee import Employee, EmploymentStatus
from app.models.user import User
from app.schemas.documents import (
    AcknowledgementRegisterRead,
    AcknowledgementRegisterRow,
    CompanyDocumentRead,
    CompanyDocumentUpdate,
)
from app.services import audit
from app.services.attendance.service import local_today
from app.services.documents.vault import _check_dates, _checked, _clean, _download_name
from app.services.notifications.dispatch import notify, register_event
from app.services.storage import StorageError, get_storage

__all__ = [
    "acknowledge_company_document",
    "acknowledgement_register",
    "delete_company_document",
    "download_company_document",
    "list_company_documents",
    "update_company_document",
    "upload_company_document",
]

_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

register_event(
    "documents.policy.acknowledgement_requested",
    "Please read and acknowledge: {title}",
    "{company} has published {title} and asks every employee to read and acknowledge it. "
    "Open Documents → Company policies to do it.",
)


def _read(
    row: CompanyDocument, *, today: date, acknowledged: bool | None = None
) -> CompanyDocumentRead:
    read = CompanyDocumentRead.model_validate(row)
    read.is_expired = row.valid_until is not None and row.valid_until < today
    read.acknowledged_by_me = acknowledged
    return read


async def _my_acknowledgements(
    session: AsyncSession, user: User, rows: list[CompanyDocument]
) -> set[uuid.UUID]:
    """Which of these documents the caller acknowledged in their current version."""
    if user.employee_id is None or not rows:
        return set()
    acks = (
        await session.execute(
            tenant_scoped(
                select(
                    CompanyDocumentAcknowledgement.document_id,
                    CompanyDocumentAcknowledgement.content_sha256,
                ).where(
                    CompanyDocumentAcknowledgement.employee_id == user.employee_id,
                    CompanyDocumentAcknowledgement.document_id.in_([r.id for r in rows]),
                ),
                CompanyDocumentAcknowledgement,
                user,
            )
        )
    ).all()
    current = {r.id: r.content_sha256 for r in rows}
    return {doc_id for doc_id, digest in acks if current.get(doc_id) == digest}


async def _ask_everyone(session: AsyncSession, user: User, row: CompanyDocument) -> None:
    """Notify every active employee with a login that this document needs them."""
    from app.models.tenant import Tenant

    recipients = list(
        (
            await session.scalars(
                tenant_scoped(
                    select(User.id)
                    .join(Employee, Employee.id == User.employee_id)
                    .where(
                        User.is_active.is_(True),
                        Employee.deleted_at.is_(None),
                        Employee.status != EmploymentStatus.EXITED,
                    ),
                    User,
                    user,
                )
            )
        ).all()
    )
    if not recipients:
        return
    tenant = await session.get(Tenant, user.tenant_id)
    await notify(
        session,
        tenant_id=user.tenant_id,
        event_key="documents.policy.acknowledgement_requested",
        recipient_user_ids=recipients,
        payload={"title": row.title, "company": tenant.name if tenant else "Your company"},
        commit=False,
    )


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
    rows = list((await session.scalars(tenant_scoped(stmt, CompanyDocument, user))).all())
    today = await local_today(session, user)
    mine = await _my_acknowledgements(session, user, rows)
    return [
        _read(
            row,
            today=today,
            acknowledged=(row.id in mine) if row.requires_acknowledgement else None,
        )
        for row in rows
    ]


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
    requires_acknowledgement: bool = False,
    request: Request | None = None,
) -> CompanyDocumentRead:
    permissions.require_hr(user, detail="Only HR can add company documents.")
    if requires_acknowledgement and not is_employee_visible:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A document employees must acknowledge has to be published to them.",
        )
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
        requires_acknowledgement=requires_acknowledgement,
        content_sha256=hashlib.sha256(data).hexdigest(),
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
            "requires_acknowledgement": requires_acknowledgement,
        },
        summary=f"Added company document '{row.title}' ({row.document_type})",
        request=request,
    )
    if requires_acknowledgement:
        await _ask_everyone(session, user, row)
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
    for key in ("document_type", "title", "is_employee_visible", "requires_acknowledgement"):
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
    newly_required = changes.get("requires_acknowledgement") is True and (
        not row.requires_acknowledgement
    )
    for key, value in changes.items():
        setattr(row, key, value)
    if row.requires_acknowledgement and not row.is_employee_visible:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A document employees must acknowledge has to be published to them.",
        )
    if newly_required:
        await _ask_everyone(session, user, row)
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


# ── Acknowledgement ─────────────────────────────────────────────────────────


async def acknowledge_company_document(
    session: AsyncSession,
    user: User,
    document_id: uuid.UUID,
    *,
    typed_name: str,
    request: Request | None = None,
) -> CompanyDocumentRead:
    """The caller reads and accepts this version of the document."""
    if user.employee_id is None:
        raise permissions.forbidden("Only an employee acknowledges a company policy.")
    row = await _get(session, user, document_id)
    if not row.requires_acknowledgement:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This document does not ask for an acknowledgement.",
        )
    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == user.employee_id), Employee, user)
    )
    if employee is None:
        raise _NOT_FOUND
    typed = " ".join(typed_name.split())
    expected = {
        " ".join(name.split()).lower()
        for name in (employee.full_name, employee.display_name)
        if name
    }
    if typed.lower() not in expected:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Type your own name as it is on record ({employee.full_name}) to acknowledge.",
        )
    digest = row.content_sha256 or ""
    existing = await session.scalar(
        tenant_scoped(
            select(CompanyDocumentAcknowledgement.id).where(
                CompanyDocumentAcknowledgement.document_id == row.id,
                CompanyDocumentAcknowledgement.employee_id == employee.id,
                CompanyDocumentAcknowledgement.content_sha256 == digest,
            ),
            CompanyDocumentAcknowledgement,
            user,
        )
    )
    if existing is None:
        ack = CompanyDocumentAcknowledgement(
            tenant_id=user.tenant_id,
            document_id=row.id,
            employee_id=employee.id,
            user_id=user.id,
            typed_name=typed,
            content_sha256=digest,
            acknowledged_at=datetime.now(UTC),
            ip_address=request.client.host if request and request.client else None,
            user_agent=(request.headers.get("user-agent") or "")[:512] if request else None,
        )
        session.add(ack)
        await session.flush()
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.CREATE,
            entity_type="company_document_acknowledgement",
            entity_id=ack.id,
            actor=user,
            changes={"document": row.title, "content_sha256": digest},
            summary=f"{employee.employee_code} acknowledged '{row.title}'",
            request=request,
        )
        await session.commit()
    return _read(row, today=await local_today(session, user), acknowledged=True)


async def acknowledgement_register(
    session: AsyncSession, user: User, document_id: uuid.UUID
) -> AcknowledgementRegisterRead:
    """Who acknowledged the current version, and who is still to."""
    permissions.require_hr(user, detail="The acknowledgement register is for HR.")
    row = await _get(session, user, document_id)
    acks = {
        ack.employee_id: ack
        for ack in (
            await session.scalars(
                tenant_scoped(
                    select(CompanyDocumentAcknowledgement).where(
                        CompanyDocumentAcknowledgement.document_id == row.id,
                        CompanyDocumentAcknowledgement.content_sha256 == (row.content_sha256 or ""),
                    ),
                    CompanyDocumentAcknowledgement,
                    user,
                )
            )
        ).all()
    }
    employees = (
        await session.scalars(
            tenant_scoped(
                select(Employee)
                .where(Employee.deleted_at.is_(None), Employee.status != EmploymentStatus.EXITED)
                .order_by(Employee.employee_code),
                Employee,
                user,
            )
        )
    ).all()
    rows = [
        AcknowledgementRegisterRow(
            employee_id=employee.id,
            employee_code=employee.employee_code,
            employee_name=employee.full_name,
            acknowledged_at=acks[employee.id].acknowledged_at if employee.id in acks else None,
            typed_name=acks[employee.id].typed_name if employee.id in acks else None,
        )
        for employee in employees
    ]
    done = sum(1 for r in rows if r.acknowledged_at is not None)
    return AcknowledgementRegisterRead(
        document_id=row.id,
        title=row.title,
        content_sha256=row.content_sha256,
        acknowledged=done,
        pending=len(rows) - done,
        rows=rows,
    )
