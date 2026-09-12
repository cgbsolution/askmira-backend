"""Letter template and issuance endpoints (slice 03)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.core import permissions
from app.core.config import settings
from app.core.deps import CurrentUser, DbSession, require_roles, tenant_scoped
from app.models.audit import AuditAction
from app.models.documents import (
    FieldSource,
    IssuedLetter,
    LetterCategory,
    LetterStatus,
    LetterTemplate,
    LetterTemplateField,
)
from app.models.user import UserRole
from app.services import audit
from app.services.documents import service as letter_service
from app.services.documents.seed_templates import REVIEW_BANNER, SEED_TEMPLATES
from app.services.documents.signature import SignatureError, get_provider

router = APIRouter(tags=["documents"])

HR_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN, UserRole.HR_OPS)


# ── Contracts ───────────────────────────────────────────────────────────────


class TemplateFieldInput(BaseModel):
    token: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=255)
    source: FieldSource
    source_path: str | None = None
    required: bool = True


class TemplateCreate(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=255)
    category: LetterCategory
    body_html: str = Field(min_length=1)
    header_html: str | None = None
    footer_html: str | None = None
    requires_approval: bool = False
    approver_roles: list[str] = Field(default_factory=list)
    requires_signature: bool = False
    signatory_role: str | None = None
    fields: list[TemplateFieldInput] = Field(default_factory=list)


class TemplateFieldRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    token: str
    label: str
    source: FieldSource
    required: bool


class TemplateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    category: LetterCategory
    version: int
    active: bool
    requires_approval: bool
    requires_signature: bool
    is_seed: bool
    fields: list[TemplateFieldRead]


class IssueRequest(BaseModel):
    employee_id: uuid.UUID
    template_id: uuid.UUID
    manual_values: dict[str, Any] = Field(default_factory=dict)
    as_of: date | None = Field(
        default=None,
        description=(
            "Resolve employee and salary values as of this date. A relieving letter "
            "for a past exit must show the salary in force then, not today's."
        ),
    )


class BulkIssueRequest(BaseModel):
    template_id: uuid.UUID
    employee_ids: list[uuid.UUID] = Field(min_length=1, max_length=1000)
    manual_values: dict[str, Any] = Field(default_factory=dict)
    as_of: date | None = None


class LetterRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    template_id: uuid.UUID
    template_version: int
    category: LetterCategory
    reference_number: str
    content_hash: str
    status: LetterStatus
    issued_at: datetime | None
    signed_at: datetime | None
    pdf_storage_key: str | None


class BulkJobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    template_id: uuid.UUID
    total: int
    succeeded: int
    failed: int
    status: str
    results: list


class SendForSignatureRequest(BaseModel):
    provider: str = "noop"


# ── Templates ───────────────────────────────────────────────────────────────


@router.get("/letter-templates", response_model=list[TemplateRead])
async def list_templates(user: CurrentUser, session: DbSession) -> list[TemplateRead]:
    stmt = tenant_scoped(
        select(LetterTemplate)
        .where(LetterTemplate.active.is_(True))
        .order_by(LetterTemplate.category, LetterTemplate.code),
        LetterTemplate,
        user,
    )
    return [TemplateRead.model_validate(row) for row in (await session.scalars(stmt)).all()]


@router.post(
    "/letter-templates",
    response_model=TemplateRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def create_template(
    payload: TemplateCreate, user: CurrentUser, session: DbSession, request: Request
) -> TemplateRead:
    """Create a template, or a new version of an existing code.

    Editing never mutates an existing version — issued letters name the version
    they used and must stay re-renderable.
    """
    template = await letter_service.create_template_version(
        session,
        user,
        code=payload.code,
        payload=payload.model_dump(exclude={"code", "fields"}),
        request=request,
    )
    for item in payload.fields:
        session.add(
            LetterTemplateField(
                tenant_id=user.tenant_id, template_id=template.id, **item.model_dump()
            )
        )
    await session.commit()
    await session.refresh(template)
    return TemplateRead.model_validate(template)


@router.post(
    "/letter-templates/seed",
    response_model=list[TemplateRead],
    dependencies=[Depends(require_roles(UserRole.TENANT_ADMIN, UserRole.HR_ADMIN))],
)
async def seed_templates(
    user: CurrentUser, session: DbSession, request: Request
) -> list[TemplateRead]:
    """Install the standard Indian-format seed templates.

    Every one is flagged `is_seed`, which the UI renders as a banner: these are
    starting points that the client's HR and legal teams must review before
    anything goes to a real employee.
    """
    created: list[LetterTemplate] = []
    for spec in SEED_TEMPLATES:
        existing = await session.scalar(
            tenant_scoped(
                select(LetterTemplate).where(LetterTemplate.code == spec["code"]),
                LetterTemplate,
                user,
            )
        )
        if existing is not None:
            continue

        fields = spec["fields"]
        template = LetterTemplate(
            tenant_id=user.tenant_id,
            code=spec["code"],
            name=spec["name"],
            category=spec["category"],
            body_html=spec["body_html"],
            footer_html=f'<p style="font-size:8pt;color:#6b788d">{REVIEW_BANNER}</p>',
            version=1,
            active=True,
            is_seed=True,
            requires_approval=spec.get("requires_approval", False),
            approver_roles=spec.get("approver_roles", []),
            requires_signature=spec.get("requires_signature", False),
            signatory_role=spec.get("signatory_role"),
        )
        for item in fields:
            template.fields.append(
                LetterTemplateField(tenant_id=user.tenant_id, **item)
            )
        session.add(template)
        created.append(template)

    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="letter_template",
        actor=user,
        summary=f"Installed {len(created)} seed letter template(s)",
        request=request,
    )
    await session.commit()
    for template in created:
        await session.refresh(template)
    return [TemplateRead.model_validate(t) for t in created]


# ── Issuance ────────────────────────────────────────────────────────────────


@router.post(
    "/letters/issue",
    response_model=LetterRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*HR_ROLES))],
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
    return LetterRead.model_validate(letter)


@router.post(
    "/letters/issue-bulk",
    response_model=BulkJobRead,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def issue_bulk(
    payload: BulkIssueRequest, user: CurrentUser, session: DbSession, request: Request
) -> BulkJobRead:
    """Issue one template to many employees.

    Reports per-employee outcomes. One employee without an assigned salary
    fails on their own row rather than blocking the whole increment cycle.
    """
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
) -> list[LetterRead]:
    """An employee's letter archive."""
    stmt = tenant_scoped(
        select(IssuedLetter).order_by(IssuedLetter.issued_at.desc()), IssuedLetter, user
    )
    if not permissions.is_hr(user):
        # An issued letter is somebody's offer, appointment or relieving
        # letter, usually with their salary in it.
        own = permissions.resolve_employee(
            user, employee_id, detail="You can only see your own letters."
        )
        stmt = stmt.where(IssuedLetter.employee_id == own)
    elif employee_id:
        stmt = stmt.where(IssuedLetter.employee_id == employee_id)
    return [LetterRead.model_validate(row) for row in (await session.scalars(stmt)).all()]


@router.get("/letters/{letter_id}/pdf")
async def download_pdf(letter_id: uuid.UUID, user: CurrentUser, session: DbSession) -> Response:
    letter, intact = await letter_service.verify_letter(session, user, letter_id)
    permissions.require_self_or_hr(
        user, letter.employee_id, detail="You can only open your own letters."
    )
    if not intact:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Letter {letter.reference_number} does not match its stored content hash. "
                "It may have been tampered with; do not distribute it."
            ),
        )

    data = letter_service.stored_pdf(letter.pdf_storage_key or "")
    if data is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The rendered PDF is not available. Re-issue the letter.",
        )

    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'inline; filename="{letter.reference_number.replace("/", "_")}.pdf"'
            )
        },
    )


@router.get("/letters/{letter_id}/verify")
async def verify(letter_id: uuid.UUID, user: CurrentUser, session: DbSession) -> dict:
    """Confirm stored content still matches the hash recorded at issuance."""
    letter, intact = await letter_service.verify_letter(session, user, letter_id)
    permissions.require_self_or_hr(
        user, letter.employee_id, detail="You can only open your own letters."
    )
    return {
        "reference_number": letter.reference_number,
        "content_hash": letter.content_hash,
        "intact": intact,
    }


@router.post(
    "/letters/{letter_id}/send-for-signature",
    response_model=LetterRead,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def send_for_signature(
    letter_id: uuid.UUID,
    payload: SendForSignatureRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> LetterRead:
    from app.models.employee import Employee

    letter = await session.scalar(
        tenant_scoped(select(IssuedLetter).where(IssuedLetter.id == letter_id), IssuedLetter, user)
    )
    if letter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Letter not found")
    if letter.status not in {LetterStatus.APPROVED, LetterStatus.SENT}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A letter in status '{letter.status.value}' cannot be sent for signature",
        )

    document = letter_service.stored_pdf(letter.pdf_storage_key or "")
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="This letter has no rendered PDF"
        )

    employee = await session.get(Employee, letter.employee_id)

    try:
        provider = _build_provider(payload.provider)
        signature_request = await provider.send_for_signature(
            document=document,
            file_name=f"{letter.reference_number.replace('/', '_')}.pdf",
            signer_name=employee.full_name if employee else "Employee",
            signer_email=employee.work_email if employee else "",
            reason=letter.category.value,
        )
    except SignatureError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    letter.signature_provider = signature_request.provider
    letter.signature_request_id = signature_request.request_id
    letter.status = LetterStatus.SENT

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="issued_letter",
        entity_id=letter.id,
        actor=user,
        changes={
            "provider": signature_request.provider,
            "request_id": signature_request.request_id,
        },
        summary=f"Sent {letter.reference_number} for signature",
        request=request,
    )
    await session.commit()
    await session.refresh(letter)
    return LetterRead.model_validate(letter)


# ── Webhook ─────────────────────────────────────────────────────────────────


@router.post("/webhooks/signature/{provider_name}", include_in_schema=False)
async def signature_webhook(
    provider_name: str, request: Request, session: DbSession
) -> dict[str, str]:
    """Receive a signature completion.

    Verification happens **before** the payload is parsed or trusted. An
    unverified webhook here would let anyone mark any letter signed, and a
    signed appointment letter is a legal document.

    Deliberately unauthenticated in the usual sense — the provider has no
    bearer token — so the HMAC *is* the authentication.
    """
    import json

    raw_body = await request.body()

    try:
        provider = _build_provider(provider_name)
    except SignatureError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    if not provider.verify_webhook(raw_body=raw_body, headers=dict(request.headers)):
        # No detail about *why* — an attacker probing the endpoint learns
        # nothing about the expected signature format.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Webhook signature is not valid"
        )

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Webhook body is not valid JSON"
        ) from exc

    signature_status = provider.parse_webhook(payload)
    if signature_status.state != "signed":
        return {"status": "ignored", "state": signature_status.state}

    # The webhook is not tenant-scoped — the provider does not know about our
    # tenants. The request id is the lookup key, and it was minted by us.
    letter = await session.scalar(
        select(IssuedLetter).where(
            IssuedLetter.signature_request_id == signature_status.request_id
        )
    )
    if letter is None:
        # 200 rather than 404: providers retry on non-2xx, and retrying a
        # webhook for a letter we do not have will never succeed.
        return {"status": "unknown_request"}

    letter.status = LetterStatus.SIGNED
    letter.signed_at = signature_status.signed_at

    try:
        signed_document = await provider.download_signed(signature_status.request_id)
        key = (letter.pdf_storage_key or "").replace(".pdf", "-signed.pdf")
        letter_service._PDF_CACHE[key] = signed_document
        letter.signed_pdf_storage_key = key
    except SignatureError:
        # The status change stands even if the download fails; a retry can
        # fetch the document later.
        pass

    await audit.record(
        session,
        tenant_id=letter.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="issued_letter",
        entity_id=letter.id,
        actor=None,
        actor_type="system",
        changes={"provider": provider_name, "request_id": signature_status.request_id},
        summary=f"Letter {letter.reference_number} signed",
    )
    await session.commit()
    return {"status": "ok"}


def _build_provider(name: str):
    """Construct a provider from configuration.

    Production credentials come from settings; the noop provider is only ever
    selectable outside production.
    """
    if name == "noop":
        if settings.is_production:
            raise SignatureError("The noop signature provider is not available in production")
        return get_provider("noop", webhook_secret=settings.JWT_SECRET_KEY)

    if name == "digio":
        return get_provider(
            "digio",
            client_id=getattr(settings, "DIGIO_CLIENT_ID", "") or "",
            client_secret=getattr(settings, "DIGIO_CLIENT_SECRET", "") or "",
            webhook_secret=getattr(settings, "DIGIO_WEBHOOK_SECRET", "") or "",
        )

    raise SignatureError(f"Unknown signature provider '{name}'")
