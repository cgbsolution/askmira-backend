"""Protections the database itself enforces, not only the code.

What this holds: an UPDATE or DELETE on audit_logs is refused by Postgres
itself, whoever issues it; removing a user still unlinks their rows (the
foreign key's SET NULL), and changes nothing else on them. And PAN and bank
account numbers sit in the table as ciphertext while the app reads them as written.
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import DBAPIError

from app.models.audit import AuditAction, AuditLog
from app.models.user import User, UserRole
from app.services import audit
from tests.integration.conftest import requires_db

pytestmark = requires_db


async def _entry(session, tenant, actor) -> AuditLog:
    await audit.record(
        session,
        tenant_id=tenant.id,
        actor=actor,
        action=AuditAction.UPDATE,
        entity_type="employee",
        entity_id=None,
        summary="Salary revised",
    )
    await session.commit()
    return await session.scalar(select(AuditLog).where(AuditLog.summary == "Salary revised"))


async def test_an_update_is_refused_by_the_database(session, tenant, hr_user) -> None:
    row = await _entry(session, tenant, hr_user)
    with pytest.raises(DBAPIError, match="insert-only"):
        await session.execute(
            update(AuditLog).where(AuditLog.id == row.id).values(summary="Nothing happened")
        )
    await session.rollback()


async def test_a_delete_is_refused_by_the_database(session, tenant, hr_user) -> None:
    row = await _entry(session, tenant, hr_user)
    with pytest.raises(DBAPIError, match="insert-only"):
        await session.execute(delete(AuditLog).where(AuditLog.id == row.id))
    await session.rollback()
    with pytest.raises(DBAPIError, match="insert-only"):
        await session.execute(text("DELETE FROM audit_logs"))
    await session.rollback()


async def test_removing_a_user_only_unlinks_their_rows(session, tenant) -> None:
    leaver = User(
        tenant_id=tenant.id,
        email="leaver@acme.test",
        password_hash="x",
        full_name="Leaver",
        role=UserRole.SUB_ADMIN,
        is_active=True,
    )
    session.add(leaver)
    await session.commit()
    row = await _entry(session, tenant, leaver)
    assert row.actor_user_id == leaver.id

    await session.execute(delete(User).where(User.id == leaver.id))
    await session.commit()
    kept = (
        await session.execute(
            select(AuditLog.actor_user_id, AuditLog.summary).where(AuditLog.id == row.id)
        )
    ).one()
    assert kept.actor_user_id is None
    assert kept.summary == "Salary revised"


async def test_pan_and_bank_account_are_ciphertext_in_the_table(
    session, tenant, legal_entity, location
) -> None:
    """Encrypted at rest by the column type; the application reads them as written."""
    from app.models.employee import Employee, EmploymentStatus

    person = Employee(
        tenant_id=tenant.id,
        employee_code="ENC1",
        first_name="Asha",
        last_name="Rao",
        work_email="asha@acme-demo.com",
        legal_entity_id=legal_entity.id,
        location_id=location.id,
        status=EmploymentStatus.ACTIVE,
        pan="ABCDE1234F",
        bank_account_number="50100012345678",
    )
    session.add(person)
    await session.commit()

    raw = (
        await session.execute(
            text("SELECT pan, bank_account_number FROM employees WHERE id = :id"),
            {"id": person.id},
        )
    ).one()
    assert raw.pan.startswith("encf:v1:") and "ABCDE1234F" not in raw.pan
    assert raw.bank_account_number.startswith("encf:v1:")

    read = (
        await session.execute(
            select(Employee.pan, Employee.bank_account_number).where(Employee.id == person.id)
        )
    ).one()
    assert (read.pan, read.bank_account_number) == ("ABCDE1234F", "50100012345678")
