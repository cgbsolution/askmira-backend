"""The company document library: policies and handbooks. Thin — rules in the service."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile, status

from app.core import permissions
from app.core.config import settings
from app.core.deps import CurrentUser, DbSession, require_roles
from app.schemas.documents import (
    AcknowledgementRegisterRead,
    CompanyDocumentAcknowledge,
    CompanyDocumentRead,
    CompanyDocumentUpdate,
)
from app.services.documents import company

router = APIRouter(prefix="/documents/company", tags=["documents"])

HR_ONLY = [Depends(require_roles(*permissions.HR_ROLES))]


@router.get("", response_model=list[CompanyDocumentRead])
async def list_documents(
    user: CurrentUser,
    session: DbSession,
    document_type: str | None = Query(default=None, max_length=64),
) -> list[CompanyDocumentRead]:
    """Every company document for HR; the published ones for everyone else."""
    return await company.list_company_documents(session, user, document_type=document_type)


@router.post(
    "",
    response_model=CompanyDocumentRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=HR_ONLY,
)
async def upload(
    user: CurrentUser,
    session: DbSession,
    request: Request,
    document_type: Annotated[str, Form(min_length=1, max_length=64)],
    title: Annotated[str, Form(min_length=1, max_length=255)],
    file: Annotated[UploadFile, File()],
    document_number: Annotated[str | None, Form(max_length=64)] = None,
    remark: Annotated[str | None, Form(max_length=1000)] = None,
    issued_on: Annotated[date | None, Form()] = None,
    valid_until: Annotated[date | None, Form()] = None,
    is_employee_visible: Annotated[bool, Form()] = True,
    requires_acknowledgement: Annotated[bool, Form()] = False,
) -> CompanyDocumentRead:
    """A PDF, PNG or JPEG. Its type is read from the bytes, not the file name."""
    data = await file.read(settings.MAX_UPLOAD_BYTES + 1)
    return await company.upload_company_document(
        session,
        user,
        document_type=document_type,
        title=title,
        data=data,
        document_number=document_number,
        remark=remark,
        issued_on=issued_on,
        valid_until=valid_until,
        is_employee_visible=is_employee_visible,
        requires_acknowledgement=requires_acknowledgement,
        request=request,
    )


@router.get("/{document_id}/download")
async def download(
    document_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> Response:
    filename, mime, data = await company.download_company_document(
        session, user, document_id, request=request
    )
    return Response(
        content=data,
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{document_id}/acknowledge", response_model=CompanyDocumentRead)
async def acknowledge(
    document_id: uuid.UUID,
    payload: CompanyDocumentAcknowledge,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> CompanyDocumentRead:
    """The caller reads and accepts the current version, by typing their own name."""
    if user.employee_id is None:
        raise permissions.forbidden("Only an employee acknowledges a company policy.")
    return await company.acknowledge_company_document(
        session, user, document_id, typed_name=payload.typed_name, request=request
    )


@router.get(
    "/{document_id}/acknowledgements",
    response_model=AcknowledgementRegisterRead,
    dependencies=HR_ONLY,
)
async def acknowledgements(
    document_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> AcknowledgementRegisterRead:
    return await company.acknowledgement_register(session, user, document_id)


@router.patch("/{document_id}", response_model=CompanyDocumentRead, dependencies=HR_ONLY)
async def update(
    document_id: uuid.UUID,
    payload: CompanyDocumentUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> CompanyDocumentRead:
    return await company.update_company_document(
        session, user, document_id, payload, request=request
    )


@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    dependencies=HR_ONLY,
)
async def delete(
    document_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> None:
    await company.delete_company_document(session, user, document_id, request=request)
