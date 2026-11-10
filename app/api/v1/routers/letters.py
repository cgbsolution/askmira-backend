"""Letter template and issuance endpoints (slice 03).

Thin: parse, gate, delegate to `app.services.documents.service`, serialise. Letter
HTML leaves this API only for a sandboxed frame or as a PDF attachment.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Request, Response, UploadFile, status

from app.core import permissions
from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.documents import LetterCategory, LetterStatus
from app.models.user import UserRole
from app.schemas.letters import (
    BulkIssueRequest,
    BulkJobRead,
    DraftPreviewRequest,
    IssueRequest,
    LetterAcknowledgeRequest,
    LetterContentRead,
    LetterheadRead,
    LetterPdfPreviewRequest,
    LetterRead,
    MergeFieldsRead,
    PrefillRead,
    PrefillRequest,
    PreviewRead,
    PreviewRequest,
    RevokeRequest,
    SendForSignatureRequest,
    SignatureProviderRead,
    TemplateCreate,
    TemplateDetail,
    TemplateRead,
    TemplateUpdate,
    TemplateVersionRead,
    VerifyRead,
)
from app.services import uploads
from app.services.documents import acknowledgement
from app.services.documents import letterhead as letterhead_service
from app.services.documents import service as letter_service
from app.services.documents.letterhead import LetterheadSettings

router = APIRouter(tags=["documents"])

HR_ONLY = [Depends(require_roles(*permissions.HR_ROLES))]
ADMIN_ONLY = [Depends(require_roles(UserRole.ADMIN))]


# ── Templates ───────────────────────────────────────────────────────────────


@router.get("/letter-templates", response_model=list[TemplateRead])
async def list_templates(
    user: CurrentUser, session: DbSession, include_inactive: bool = False
) -> list[TemplateRead]:
    rows = await letter_service.list_templates(session, user, include_inactive=include_inactive)
    return [TemplateRead.model_validate(row) for row in rows]


@router.get("/letter-templates/merge-fields", response_model=MergeFieldsRead)
async def merge_fields(user: CurrentUser) -> MergeFieldsRead:
    return letter_service.merge_fields()


@router.post(
    "/letter-templates",
    response_model=TemplateDetail,
    status_code=status.HTTP_201_CREATED,
    dependencies=HR_ONLY,
)
async def create_template(
    payload: TemplateCreate, user: CurrentUser, session: DbSession, request: Request
) -> TemplateDetail:
    """Create a template, or a new version of an existing code.

    Editing never mutates an existing version — issued letters name the version
    they used and must stay re-renderable.
    """
    template = await letter_service.create_template_version(
        session,
        user,
        code=payload.code,
        payload=payload.model_dump(exclude={"code", "fields"}),
        fields=payload.fields,
        request=request,
    )
    return TemplateDetail.model_validate(template)


@router.post("/letter-templates/preview", response_model=PreviewRead, dependencies=HR_ONLY)
async def preview_draft(
    payload: DraftPreviewRequest, user: CurrentUser, session: DbSession
) -> PreviewRead:
    """Render unsaved template content. Nothing is issued or numbered."""
    return await letter_service.preview_draft(session, user, payload)


@router.post("/letter-templates/preview-pdf", dependencies=HR_ONLY)
async def preview_pdf(
    payload: LetterPdfPreviewRequest, user: CurrentUser, session: DbSession
) -> Response:
    """The letter as it would print, on the letterhead. Nothing is issued or numbered."""
    data = await letter_service.pdf_preview(session, user, payload)
    return _pdf(data, "preview.pdf", inline=True)


@router.post(
    "/letter-templates/{template_id}/prefill",
    response_model=PrefillRead,
    dependencies=HR_ONLY,
)
async def prefill(
    template_id: uuid.UUID, payload: PrefillRequest, user: CurrentUser, session: DbSession
) -> PrefillRead:
    """The issue form's starting values for one person (notice period and the like)."""
    return await letter_service.prefill(
        session, user, template_id, payload.employee_id, payload.as_of
    )


@router.post(
    "/letter-templates/seed",
    response_model=list[TemplateRead],
    dependencies=ADMIN_ONLY,
)
async def seed_templates(
    user: CurrentUser, session: DbSession, request: Request
) -> list[TemplateRead]:
    """Install the standard Indian-format seed templates.

    Every one is flagged `is_seed`, which the UI renders as a banner: these are
    starting points that the client's HR and legal teams must review before
    anything goes to a real employee.
    """
    rows = await letter_service.seed_templates(session, user, request=request)
    return [TemplateRead.model_validate(row) for row in rows]


@router.get("/letter-templates/{template_id}", response_model=TemplateDetail, dependencies=HR_ONLY)
async def get_template(
    template_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> TemplateDetail:
    return TemplateDetail.model_validate(
        await letter_service.get_template(session, user, template_id)
    )


@router.get(
    "/letter-templates/{template_id}/versions",
    response_model=list[TemplateVersionRead],
    dependencies=HR_ONLY,
)
async def template_versions(
    template_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[TemplateVersionRead]:
    rows = await letter_service.template_versions(session, user, template_id)
    return [TemplateVersionRead.model_validate(row) for row in rows]


@router.put("/letter-templates/{template_id}", response_model=TemplateDetail, dependencies=HR_ONLY)
async def update_template(
    template_id: uuid.UUID,
    payload: TemplateUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> TemplateDetail:
    """Edit a template: saved as the next version of its code."""
    template = await letter_service.update_template(
        session, user, template_id, payload, request=request
    )
    return TemplateDetail.model_validate(template)


@router.post(
    "/letter-templates/{template_id}/preview",
    response_model=PreviewRead,
    dependencies=HR_ONLY,
)
async def preview_template(
    template_id: uuid.UUID, payload: PreviewRequest, user: CurrentUser, session: DbSession
) -> PreviewRead:
    """Render a saved version against an employee, or labelled sample values."""
    return await letter_service.preview_template(session, user, template_id, payload)


# ── Issuance ────────────────────────────────────────────────────────────────


@router.post(
    "/letters/issue",
    response_model=LetterRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=HR_ONLY,
)
async def issue(
    payload: IssueRequest, user: CurrentUser, session: DbSession, request: Request
) -> LetterRead:
    letter = await letter_service.issue_letter(
        session,
        user,
        employee_id=payload.employee_id,
        template_id=payload.template_id,
        manual_values=payload.manual_values,
        as_of=payload.as_of,
        request=request,
    )
    return (await letter_service.list_letters(session, user, employee_id=letter.employee_id))[0]


@router.post(
    "/letters/issue-bulk",
    response_model=BulkJobRead,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=HR_ONLY,
)
async def issue_bulk(
    payload: BulkIssueRequest, user: CurrentUser, session: DbSession, request: Request
) -> BulkJobRead:
    """Issue one template to many employees, with per-employee outcomes."""
    job = await letter_service.issue_bulk(
        session,
        user,
        template_id=payload.template_id,
        employee_ids=payload.employee_ids,
        manual_values=payload.manual_values,
        as_of=payload.as_of,
        request=request,
    )
    return BulkJobRead.model_validate(job)


@router.get("/letters", response_model=list[LetterRead])
async def list_letters(
    user: CurrentUser,
    session: DbSession,
    employee_id: uuid.UUID | None = None,
    letter_status: LetterStatus | None = None,
    category: LetterCategory | None = None,
) -> list[LetterRead]:
    """An employee's letter archive, or every letter for HR."""
    return await letter_service.list_letters(
        session, user, employee_id=employee_id, letter_status=letter_status, category=category
    )


@router.get("/letters/signature-provider", response_model=SignatureProviderRead)
async def signature_provider(user: CurrentUser, session: DbSession) -> SignatureProviderRead:
    return await letter_service.signature_provider_status(session, user)


@router.get("/letters/{letter_id}/pdf")
async def download_pdf(
    letter_id: uuid.UUID, user: CurrentUser, session: DbSession, signed: bool = False
) -> Response:
    filename, data = await letter_service.letter_pdf(session, user, letter_id, signed=signed)
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/letters/{letter_id}/content", response_model=LetterContentRead)
async def letter_content(
    letter_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> LetterContentRead:
    """The merged HTML. The portal renders it only inside a sandboxed iframe."""
    return await letter_service.letter_content(session, user, letter_id)


@router.get("/letters/{letter_id}/verify", response_model=VerifyRead)
async def verify(letter_id: uuid.UUID, user: CurrentUser, session: DbSession) -> VerifyRead:
    """Confirm stored content still matches the hash recorded at issuance."""
    return await letter_service.letter_verify(session, user, letter_id)


@router.post("/letters/{letter_id}/approve", response_model=LetterRead, dependencies=ADMIN_ONLY)
async def approve(
    letter_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> LetterRead:
    return await letter_service.approve_letter(session, user, letter_id, request=request)


@router.post("/letters/{letter_id}/revoke", response_model=LetterRead, dependencies=ADMIN_ONLY)
async def revoke(
    letter_id: uuid.UUID,
    payload: RevokeRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> LetterRead:
    return await letter_service.revoke_letter(
        session, user, letter_id, reason=payload.reason, request=request
    )


@router.post(
    "/letters/{letter_id}/send-for-signature",
    response_model=LetterRead,
    dependencies=HR_ONLY,
)
async def send_for_signature(
    letter_id: uuid.UUID,
    payload: SendForSignatureRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> LetterRead:
    return await letter_service.send_for_signature(
        session, user, letter_id, provider_name=payload.provider, request=request
    )


@router.post(
    "/letters/{letter_id}/signature-status",
    response_model=LetterRead,
    dependencies=HR_ONLY,
)
async def check_signature_status(
    letter_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> LetterRead:
    """Ask the e-sign provider now whether the letter has been signed."""
    return await letter_service.refresh_signature_status(session, user, letter_id)


# ── In-portal acknowledgement ──────────────────────────────────────────────


@router.post(
    "/letters/{letter_id}/request-acknowledgement",
    response_model=LetterRead,
    dependencies=HR_ONLY,
)
async def request_acknowledgement(
    letter_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> LetterRead:
    """Ask the employee to read and acknowledge the letter. Asking again reminds them."""
    return await acknowledgement.request_acknowledgement(session, user, letter_id, request=request)


@router.post("/letters/{letter_id}/acknowledge", response_model=LetterRead)
async def acknowledge(
    letter_id: uuid.UUID,
    payload: LetterAcknowledgeRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> LetterRead:
    """The employee accepts a letter issued to them, with their typed name.

    Separate from e-signature: this records acceptance in the portal, with the
    letter's content hash, the time and the address it came from.
    """
    return await acknowledgement.acknowledge(session, user, letter_id, payload, request=request)


# ── Webhook ─────────────────────────────────────────────────────────────────


@router.post("/webhooks/signature/digio/w/{connection_id}", include_in_schema=False)
async def workspace_signature_webhook(
    connection_id: uuid.UUID, request: Request, session: DbSession
) -> dict[str, str]:
    """A signature completion on a workspace's own Digio account. The workspace's
    webhook secret, checked before anything is read, is the authentication."""
    return await letter_service.record_tenant_signature_webhook(
        session,
        connection_id=connection_id,
        raw_body=await request.body(),
        headers=dict(request.headers),
    )


@router.post("/webhooks/signature/{provider_name}", include_in_schema=False)
async def signature_webhook(
    provider_name: str, request: Request, session: DbSession
) -> dict[str, str]:
    """Receive a signature completion.

    Deliberately unauthenticated in the usual sense — the provider has no bearer
    token — so the HMAC, verified in the service before anything is parsed, *is*
    the authentication.
    """
    return await letter_service.record_signature_webhook(
        session,
        provider_name=provider_name,
        raw_body=await request.body(),
        headers=dict(request.headers),
    )


# ── Letterhead ──────────────────────────────────────────────────────────────


def _pdf(data: bytes, filename: str, *, inline: bool = False) -> Response:
    disposition = "inline" if inline else "attachment"
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'{disposition}; filename="{filename}"'},
    )


async def _letterhead_read(session, user) -> LetterheadRead:
    from app.models.tenant import Tenant

    chosen = await letterhead_service.get_settings(session, user)
    tenant = await session.get(Tenant, user.tenant_id)
    return LetterheadRead(
        **LetterheadSettings.model_validate(chosen.model_dump()).model_dump(),
        has_own_logo=bool(chosen.logo_key),
        has_workspace_logo=bool(tenant and tenant.logo_key),
        has_signature=bool(chosen.signature_key),
    )


@router.get("/letterhead", response_model=LetterheadRead, dependencies=HR_ONLY)
async def get_letterhead(user: CurrentUser, session: DbSession) -> LetterheadRead:
    """The design, colours and contact lines every letter is printed with."""
    return await _letterhead_read(session, user)


@router.put("/letterhead", response_model=LetterheadRead, dependencies=ADMIN_ONLY)
async def save_letterhead(
    payload: LetterheadSettings, user: CurrentUser, session: DbSession, request: Request
) -> LetterheadRead:
    await letterhead_service.save_settings(session, user, payload, request=request)
    return await _letterhead_read(session, user)


@router.post("/letterhead/preview", dependencies=HR_ONLY)
async def preview_letterhead(
    payload: LetterheadSettings, user: CurrentUser, session: DbSession
) -> Response:
    """A sample letter on unsaved choices."""
    data = await letter_service.letterhead_preview(session, user, payload)
    return _pdf(data, "letterhead-preview.pdf", inline=True)


@router.get("/letterhead/{kind}", response_model=None, dependencies=HR_ONLY)
async def letterhead_image(
    kind: letterhead_service.ImageKind, user: CurrentUser, session: DbSession
) -> Response:
    data, media_type = await letterhead_service.image_bytes(session, user, kind)
    return Response(content=data, media_type=media_type, headers={"Cache-Control": "no-store"})


@router.post("/letterhead/{kind}", response_model=LetterheadRead, dependencies=ADMIN_ONLY)
async def upload_letterhead_image(
    kind: letterhead_service.ImageKind,
    user: CurrentUser,
    session: DbSession,
    request: Request,
    file: UploadFile = File(...),
) -> LetterheadRead:
    """The logo printed on letters (otherwise the workspace logo), or the signatory's
    signature. PNG or JPEG, 1 MB."""
    data = await uploads.read_file(file, session, user.tenant_id)
    await letterhead_service.save_image(session, user, kind, data, request=request)
    return await _letterhead_read(session, user)


@router.delete("/letterhead/{kind}", response_model=LetterheadRead, dependencies=ADMIN_ONLY)
async def remove_letterhead_image(
    kind: letterhead_service.ImageKind, user: CurrentUser, session: DbSession, request: Request
) -> LetterheadRead:
    await letterhead_service.remove_image(session, user, kind, request=request)
    return await _letterhead_read(session, user)
