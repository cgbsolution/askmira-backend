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
    OTHER = "other"


CATEGORY_LABELS: dict[DocumentCategory, str] = {
    DocumentCategory.ID_PROOF: "Identity proof",
    DocumentCategory.ADDRESS_PROOF: "Address proof",
    DocumentCategory.EDUCATION: "Education",
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
        DocumentCategory.OTHER,
    }
)


class DocumentVerify(BaseModel):
    """HR's decision on a document the employee uploaded."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["verified", "rejected"]
    note: str | None = Field(default=None, max_length=1000)
