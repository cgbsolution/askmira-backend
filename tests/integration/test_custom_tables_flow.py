"""Custom tables: typed rows per person, kept by HR or by the person where allowed."""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee import Employee
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services import custom_tables
from app.services.custom_tables import ColumnIn, TableIn, TableUpdate
from tests.integration.conftest import as_caller, requires_db

pytestmark = requires_db


async def _login(session: AsyncSession, tenant: Tenant, employee: Employee) -> User:
    user = User(
        tenant_id=tenant.id,
        email="anita.ct@acme-demo.com",
        password_hash="x",
        full_name="Anita",
        role=UserRole.EMPLOYEE,
        is_active=True,
        employee_id=employee.id,
    )
    session.add(user)
    await session.commit()
    return await as_caller(session, user)


async def test_rows_are_typed_and_the_person_edits_only_editable_tables(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    certs = await custom_tables.create_table(
        session,
        hr_user,
        TableIn(
            key="certifications",
            label="Certifications",
            employee_editable=True,
            columns=[
                ColumnIn(key="name", label="Certificate", field_type="text", is_required=True),
                ColumnIn(key="expires", label="Expires", field_type="date"),
            ],
        ),
        request=None,
    )
    checks = await custom_tables.create_table(
        session,
        hr_user,
        TableIn(
            key="disciplinary",
            label="Disciplinary notes",
            is_sensitive=True,
            columns=[ColumnIn(key="note", label="Note", field_type="text")],
        ),
        request=None,
    )
    me = await _login(session, tenant, employee)
    certs_id, checks_id = uuid.UUID(certs["id"]), uuid.UUID(checks["id"])

    with pytest.raises(HTTPException) as bad:
        await custom_tables.add_row(
            session, me, employee.id, certs_id, {"expires": "not a date"}, request=None
        )
    assert "Certificate is required" in bad.value.detail
    row = await custom_tables.add_row(
        session,
        me,
        employee.id,
        certs_id,
        {"name": "AWS Solutions Architect", "expires": "2028-01-31"},
        request=None,
    )
    assert row["values"]["expires"] == "2028-01-31"

    with pytest.raises(HTTPException):
        await custom_tables.add_row(
            session, me, employee.id, checks_id, {"note": "Self-written"}, request=None
        )
    await custom_tables.add_row(
        session, hr_user, employee.id, checks_id, {"note": "Verbal warning"}, request=None
    )

    mine = await custom_tables.tables_for(session, me, employee.id)
    assert [t["key"] for t in mine] == ["certifications"]
    assert mine[0]["can_edit"] is True and len(mine[0]["rows"]) == 1
    everything = await custom_tables.tables_for(session, hr_user, employee.id)
    assert {t["key"] for t in everything} == {"certifications", "disciplinary"}

    # With rows saved, a column cannot be dropped or retyped.
    with pytest.raises(HTTPException):
        await custom_tables.update_table(
            session,
            hr_user,
            certs_id,
            TableUpdate(columns=[ColumnIn(key="name", label="Certificate", field_type="number")]),
            request=None,
        )

    await custom_tables.delete_row(session, me, uuid.UUID(row["id"]), request=None)
    assert (await custom_tables.tables_for(session, me, employee.id))[0]["rows"] == []
