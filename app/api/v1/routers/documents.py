"""The employee document vault (FRD 5.1).

Thin: parse, gate, delegate to `app.services.documents.vault`, serialise. Who may
see which document is decided in the service through `app.core.permissions`.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile, status

from app.core import permissions
from app.core.config import settings
from app.core.deps import CurrentUser, DbSession, require_roles
from app.schemas.documents import (
    CATEGORY_LABELS,
    CategoryRead,
    DocumentCategory,
    DocumentRead,
    DocumentUpdate,
)
from app.services.documents import vault

router = APIRouter(prefix="/documents", tags=["documents"])

HR_ONLY = [Depends(require_roles(*permissions.HR_ROLES))]


@router.get("/categories", response_model=list[CategoryRead])
async def list_categories(user: CurrentUser) -> list[CategoryRead]:
    return [CategoryRead(value=value, label=label) for value, label in CATEGORY_LABELS.items()]


@router.get("", response_model=list[DocumentRead])
async def list_documents(
    user: CurrentUser,
    session: DbSession,
    employee_id: uuid.UUID | None = None,
    category: DocumentCategory | None = None,
) -> list[DocumentRead]:
    return await vault.list_documents(session, user, employee_id=employee_id, category=category)


@router.get("/expiring", response_model=list[DocumentRead], dependencies=HR_ONLY)
async def expiring(
    user: CurrentUser,
    session: DbSession,
    within_days: int = Query(default=30, ge=0, le=365),
) -> list[DocumentRead]:
    return await vault.expiring_documents(session, user, within_days=within_days)


@router.post(
    "",
    response_model=DocumentRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=HR_ONLY,
)
async def upload(
    user: CurrentUser,
    session: DbSession,
    request: Request,
    employee_id: Annotated[uuid.UUID, Form()],
    category: Annotated[DocumentCategory, Form()],
    title: Annotated[str, Form(min_length=1, max_length=255)],
    file: Annotated[UploadFile, File()],
    valid_until: Annotated[date | None, Form()] = None,
    is_employee_visible: Annotated[bool, Form()] = True,
) -> DocumentRead:
    """Upload a PDF, PNG or JPEG. Type is read from the bytes; the name is ignored."""
    # One byte past the limit is enough to know it is over the limit.
    data = await file.read(settings.MAX_UPLOAD_BYTES + 1)
    return await vault.upload_document(
        session,
        user,
        employee_id=employee_id,
        category=category,
        title=title,
        data=data,
        valid_until=valid_until,
        is_employee_visible=is_employee_visible,
        request=request,
    )


@router.get("/{document_id}/download")
async def download(
    document_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> Response:
    filename, mime, data = await vault.download_document(
        session, user, document_id, request=request
    )
    return Response(
        content=data,
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.patch("/{document_id}", response_model=DocumentRead, dependencies=HR_ONLY)
async def update(
    document_id: uuid.UUID,
    payload: DocumentUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> DocumentRead:
    return await vault.update_document(session, user, document_id, payload, request=request)


@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    dependencies=HR_ONLY,
)
async def delete(
    document_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> None:
    await vault.delete_document(session, user, document_id, request=request)
