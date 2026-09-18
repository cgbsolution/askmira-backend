"""Contracts shared by the payroll-operations modules (workstream W3).

Money is `Decimal` and serialises as a string.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.models.documents import IssuedLetter


class IssuedDocumentRead(BaseModel):
    """A document issued through the letter engine: Form 12BB, Form 10E, a
    settlement statement, a relieving or experience letter."""

    letter_id: uuid.UUID
    reference_number: str
    status: str
    issued_at: datetime | None
    content_hash: str

    @classmethod
    def from_model(cls, letter: IssuedLetter) -> IssuedDocumentRead:
        return cls(
            letter_id=letter.id,
            reference_number=letter.reference_number,
            status=letter.status.value,
            issued_at=letter.issued_at,
            content_hash=letter.content_hash,
        )
