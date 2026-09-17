"""Document vault contracts."""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

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
    valid_until: date | None
    is_expired: bool
    is_employee_visible: bool
    uploaded_by_user_id: uuid.UUID | None
    created_at: datetime


class DocumentUpdate(BaseModel):
    """HR's edit of a vault entry's metadata. The file itself is immutable —
    replacing it is an upload and a delete, both audited."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=255)
    category: DocumentCategory | None = None
    valid_until: date | None = None
    is_employee_visible: bool | None = None
