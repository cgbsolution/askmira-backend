"""Column types shared across models."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from app.core.crypto import decrypt_field, encrypt_field

__all__ = ["EncryptedText"]


class EncryptedText(TypeDecorator[str]):
    """Text encrypted at rest, plaintext to the application.

    For identifiers read often and never searched by value — PAN, a bank
    account number. Comparing, grouping or sorting on the column in SQL would
    operate on ciphertext, so the report compiler refuses to (see
    `reporting/compiler.py`).
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        return encrypt_field(str(value))

    def process_result_value(self, value: Any, dialect: Any) -> str | None:
        return decrypt_field(value)
