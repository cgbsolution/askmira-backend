"""Document vault contracts."""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DocumentCategory(str, enum.Enum):
    """What a vault document is. Stored as its value in `employee_documents.category`."""

    ID_PROOF = "id_proof"
    ADDRESS_PROOF = "address_proof"
    EDUCATION = "education"
    EXPERIENCE = "experience"
    CONTRACT = "contract"
    OFFER_LETTER = "offer_letter"
    POLICY_ACK = "policy_ack"
    PAYSLIP = "payslip"
    TAX = "tax"
    MEDICAL = "medical"
    BANK = "bank"
    OTHER = "other"


CATEGORY_LABELS: dict[DocumentCategory, str] = {
    DocumentCategory.ID_PROOF: "Identity proof",
    DocumentCategory.ADDRESS_PROOF: "Address proof",
    DocumentCategory.EDUCATION: "Education",
    DocumentCategory.BANK: "Bank proof",
    DocumentCategory.EXPERIENCE: "Previous employment",
    DocumentCategory.CONTRACT: "Contract",
    DocumentCategory.OFFER_LETTER: "Offer letter",
    DocumentCategory.POLICY_ACK: "Policy acknowledgement",
    DocumentCategory.PAYSLIP: "Payslip",
    DocumentCategory.TAX: "Tax",
    DocumentCategory.MEDICAL: "Medical",
    DocumentCategory.OTHER: "Other",
}


class CategoryRead(BaseModel):
    value: DocumentCategory
    label: str


class DocumentRead(BaseModel):
    """A vault entry. The storage key is never serialised."""

    id: uuid.UUID
    employee_id: uuid.UUID
    employee_name: str
    employee_code: str
    category: str
    title: str
    mime_type: str | None
    file_size_bytes: int | None
    document_number: str | None = None
    remark: str | None = None
    dependant_name: str | None = None
    issued_on: date | None = None
    valid_until: date | None
    is_expired: bool
    is_employee_visible: bool
    uploaded_by_user_id: uuid.UUID | None
    created_at: datetime
    #: `not_required` (HR put it there), `pending`, `verified` or `rejected`.
    verification_status: str
    #: Whether the employee uploaded it themselves.
    self_uploaded: bool
    verified_at: datetime | None
    verification_note: str | None


class DocumentUpdate(BaseModel):
    """HR's edit of a vault entry's metadata. The file itself is immutable —
    replacing it is an upload and a delete, both audited."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=255)
    category: DocumentCategory | None = None
    document_number: str | None = Field(default=None, max_length=64)
    remark: str | None = Field(default=None, max_length=1000)
    dependant_name: str | None = Field(default=None, max_length=128)
    issued_on: date | None = None
    valid_until: date | None = None
    is_employee_visible: bool | None = None


#: What an employee may upload to their own vault. Contracts, payslips and
#: policy acknowledgements are HR's to issue, not the employee's to supply.
SELF_UPLOAD_CATEGORIES: frozenset[DocumentCategory] = frozenset(
    {
        DocumentCategory.ID_PROOF,
        DocumentCategory.ADDRESS_PROOF,
        DocumentCategory.EDUCATION,
        DocumentCategory.EXPERIENCE,
        DocumentCategory.TAX,
        DocumentCategory.MEDICAL,
        DocumentCategory.BANK,
        DocumentCategory.OTHER,
    }
)


class DocumentVerify(BaseModel):
    """HR's decision on a document the employee uploaded."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["verified", "rejected"]
    note: str | None = Field(default=None, max_length=1000)


# ── Company documents (policy library) ─────────────────────────────────────


class CompanyDocumentRead(BaseModel):
    """A company document. The storage key is never serialised."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    document_type: str
    title: str
    document_number: str | None
    remark: str | None
    mime_type: str | None
    file_size_bytes: int | None
    issued_on: date | None
    valid_until: date | None
    is_expired: bool = False
    is_employee_visible: bool
    created_at: datetime
    #: Every employee must read and acknowledge it.
    requires_acknowledgement: bool = False
    #: The caller's own state, where acknowledgement is required; else null.
    acknowledged_by_me: bool | None = None


class CompanyDocumentAcknowledge(BaseModel):
    typed_name: str = Field(min_length=2, max_length=255)


class AcknowledgementRegisterRow(BaseModel):
    employee_id: uuid.UUID
    employee_code: str
    employee_name: str
    acknowledged_at: datetime | None
    typed_name: str | None


class AcknowledgementRegisterRead(BaseModel):
    document_id: uuid.UUID
    title: str
    content_sha256: str | None
    acknowledged: int
    pending: int
    rows: list[AcknowledgementRegisterRow]


class CompanyDocumentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_type: str | None = Field(default=None, min_length=1, max_length=64)
    title: str | None = Field(default=None, min_length=1, max_length=255)
    document_number: str | None = Field(default=None, max_length=64)
    remark: str | None = Field(default=None, max_length=1000)
    requires_acknowledgement: bool | None = None
    issued_on: date | None = None
    valid_until: date | None = None
    is_employee_visible: bool | None = None
