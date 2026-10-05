"""Letter template and issued-letter contracts (slice 03)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.documents import FieldSource, LetterCategory, LetterStatus
from app.models.user import UserRole

# ── Templates ───────────────────────────────────────────────────────────────


class TemplateFieldInput(BaseModel):
    token: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z][a-zA-Z0-9_.]*$")
    label: str = Field(min_length=1, max_length=255)
    source: FieldSource
    source_path: str | None = Field(default=None, max_length=128)
    required: bool = True


class TemplateContent(BaseModel):
    """Everything a version is made of, apart from its code."""

    name: str = Field(min_length=1, max_length=255)
    category: LetterCategory
    body_html: str = Field(min_length=1, max_length=200_000)
    header_html: str | None = Field(default=None, max_length=50_000)
    footer_html: str | None = Field(default=None, max_length=50_000)
    requires_approval: bool = False
    approver_roles: list[str] = Field(default_factory=list)
    requires_signature: bool = False
    signatory_role: str | None = Field(default=None, max_length=64)
    fields: list[TemplateFieldInput] = Field(default_factory=list)

    @field_validator("approver_roles")
    @classmethod
    def _known_roles(cls, roles: list[str]) -> list[str]:
        known = {role.value for role in UserRole} - {UserRole.SUPER_ADMIN.value}
        unknown = sorted(set(roles) - known)
        if unknown:
            raise ValueError(f"Unknown approver role(s): {', '.join(unknown)}")
        return roles


class TemplateCreate(TemplateContent):
    code: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_\-]+$")


class TemplateUpdate(TemplateContent):
    """An edit. It becomes the next version of the template's code; the version
    being edited is never changed."""


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


class TemplateDetail(TemplateRead):
    body_html: str
    header_html: str | None
    footer_html: str | None
    approver_roles: list[str]
    signatory_role: str | None
    created_at: datetime


class TemplateVersionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    version: int
    name: str
    active: bool
    created_at: datetime


class MergeFieldsRead(BaseModel):
    """What a template may reference, for the editor."""

    sources: dict[str, list[str]]
    computed: list[str]
    manual_note: str


class PreviewRequest(BaseModel):
    employee_id: uuid.UUID | None = Field(
        default=None,
        description="Render against this employee. Omitted: labelled sample values, no salary.",
    )
    manual_values: dict[str, Any] = Field(default_factory=dict)
    as_of: date | None = None


class DraftPreviewRequest(PreviewRequest):
    body_html: str = Field(min_length=1, max_length=200_000)
    header_html: str | None = Field(default=None, max_length=50_000)
    footer_html: str | None = Field(default=None, max_length=50_000)
    fields: list[TemplateFieldInput] = Field(default_factory=list)


class PreviewRead(BaseModel):
    """A rendering for a sandboxed frame. Nothing issued, no number consumed."""

    html: str
    problems: list[str]
    is_sample: bool
    employee_label: str | None
    as_of: date


# ── Issuance ────────────────────────────────────────────────────────────────


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
    id: uuid.UUID
    employee_id: uuid.UUID | None
    candidate_id: uuid.UUID | None
    recipient_name: str
    template_id: uuid.UUID
    template_name: str
    template_version: int
    category: LetterCategory
    reference_number: str
    content_hash: str
    status: LetterStatus
    issued_at: datetime | None
    approved_at: datetime | None
    signed_at: datetime | None
    signature_provider: str | None
    has_pdf: bool
    has_signed_pdf: bool
    revoked_reason: str | None
    # ── In-portal acknowledgement (separate from e-signature) ──────────────
    acknowledged_at: datetime | None = None
    #: The name the employee typed when they accepted.
    acknowledged_name: str | None = None
    acknowledgement_requested_at: datetime | None = None
    #: True when the caller is the employee and may acknowledge it now.
    can_acknowledge: bool = False


class LetterContentRead(BaseModel):
    """The issued letter's merged HTML, for a sandboxed frame only."""

    reference_number: str
    merged_html: str
    intact: bool


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
    provider: str | None = Field(
        default=None, description="Omitted: the provider configured for this deployment."
    )


class RevokeRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class SignatureProviderRead(BaseModel):
    provider: str
    connected: bool
    detail: str


class VerifyRead(BaseModel):
    reference_number: str
    content_hash: str
    intact: bool


class LetterAcknowledgeRequest(BaseModel):
    """The employee's acceptance: their name, typed, and an explicit yes."""

    typed_name: str = Field(min_length=1, max_length=255)
    accept: bool
