"""Storing and serving receipts. The validation itself is pure, in `receipts.py`.

A receipt is stored against the employee whose claim it will support, under a
generated key. It is served back only to that employee or to finance, always as
an attachment, never rendered into the portal.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.expenses import ExpenseReceipt
from app.models.user import User
from app.services import audit
from app.services.expenses import receipts


async def store_receipt(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    data: bytes,
    filename: str | None,
    request: Request | None = None,
) -> ExpenseReceipt:
    try:
        verdict = receipts.validate_receipt(data)
    except receipts.ReceiptError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    employee = await session.scalar(
        tenant_scoped(select(Employee.id).where(Employee.id == employee_id), Employee, user)
    )
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    receipt_id = uuid.uuid4()
    row = ExpenseReceipt(
        id=receipt_id,
        tenant_id=user.tenant_id,
        employee_id=employee_id,
        uploaded_by_user_id=user.id,
        storage_key=f"receipts/{user.tenant_id}/{receipt_id}{verdict.extension}",
        content_type=verdict.content_type,
        size_bytes=verdict.size_bytes,
        sha256=verdict.sha256,
        display_name=receipts.safe_display_name(filename, verdict.extension),
        content=data,
    )
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="expense_receipt",
        entity_id=row.id,
        actor=user,
        changes={
            "employee_id": str(employee_id),
            "content_type": verdict.content_type,
            "size_bytes": verdict.size_bytes,
            "sha256": verdict.sha256,
        },
        summary=f"Receipt uploaded ({verdict.content_type}, {verdict.size_bytes} bytes)",
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def receipt_for_download(
    session: AsyncSession, user: User, receipt_id: uuid.UUID
) -> ExpenseReceipt:
    """The receipt, for its owner or finance. Another tenant's is a 404."""
    row = await session.scalar(
        tenant_scoped(
            select(ExpenseReceipt).where(ExpenseReceipt.id == receipt_id), ExpenseReceipt, user
        )
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Receipt not found")
    permissions.require_self_or_hr(
        user, row.employee_id, detail="That receipt belongs to somebody else's claim."
    )
    return row
