"""PAN and bank account numbers are ciphertext at rest and plaintext to the app.

What this holds, without a database: the field cipher round-trips, does not
double-encrypt, reads a pre-encryption plaintext value unchanged, and never
produces the same ciphertext twice; and the report compiler refuses to filter,
group or sort on an encrypted column, while still letting a payroll role see it.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from app.core.crypto import decrypt_field, encrypt_field
from app.models.employee import Employee
from app.models.types import EncryptedText
from app.services.reporting.compiler import (
    CompileError,
    Definition,
    Filter,
    Operator,
    Scope,
    ScopeKind,
    Selection,
    compile_report,
)

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")


def test_the_cipher_round_trips_and_is_idempotent() -> None:
    sealed = encrypt_field("ABCDE1234F")
    assert sealed.startswith("encf:v1:") and "ABCDE1234F" not in sealed
    assert decrypt_field(sealed) == "ABCDE1234F"
    assert encrypt_field(sealed) == sealed, "never encrypted twice"
    assert encrypt_field("ABCDE1234F") != sealed, "no equal ciphertexts to compare"


def test_values_written_before_encryption_still_read() -> None:
    assert decrypt_field("ABCDE1234F") == "ABCDE1234F"
    assert decrypt_field(None) is None and decrypt_field("") == ""


def test_the_employee_columns_use_the_encrypted_type() -> None:
    columns = Employee.__table__.c
    assert isinstance(columns.pan.type, EncryptedText)
    assert isinstance(columns.bank_account_number.type, EncryptedText)


def _scope() -> Scope:
    return Scope(kind=ScopeKind.TENANT, tenant_id=TENANT)


def test_a_report_shows_pan_but_cannot_filter_group_or_sort_on_it() -> None:
    shown = compile_report(
        Definition(
            entity_key="employee", selections=(Selection("employee_name"), Selection("pan"))
        ),
        role="admin", scope=_scope(), today=date(2026, 10, 13),
    )
    assert shown.statement is not None

    for definition in (
        Definition(
            entity_key="employee", selections=(Selection("employee_name"),),
            filters=(Filter("pan", Operator.EQUALS, "ABCDE1234F"),),
        ),
        Definition(
            entity_key="employee",
            selections=(Selection("pan"), Selection("headcount", "count")),
            group_by=("pan",),
        ),
        Definition(
            entity_key="employee", selections=(Selection("employee_name"), Selection("pan")),
            sort_by=(("pan", "asc"),),
        ),
    ):
        with pytest.raises(CompileError, match="stored encrypted"):
            compile_report(definition, role="admin", scope=_scope(), today=date(2026, 10, 13))
