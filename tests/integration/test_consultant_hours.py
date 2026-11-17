"""Consultant hours entries: one consultant's periods never overlap."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.consultant_hours import ConsultantHoursEntry, HoursEntryStatus
from app.models.employee import Employee
from tests.integration.conftest import requires_db

pytestmark = requires_db


def _entry_for(tenant_id, employee_id, start, end, status=HoursEntryStatus.DRAFT):
    return ConsultantHoursEntry(
        tenant_id=tenant_id,
        employee_id=employee_id,
        period_start=start,
        period_end=end,
        total_billable_hours=Decimal("120.00"),
        status=status,
    )


def _entry(tenant, employee, start, end, status=HoursEntryStatus.DRAFT):
    return _entry_for(tenant.id, employee.id, start, end, status)


async def test_overlapping_periods_are_refused(session, tenant, employee):
    # A rollback expires every loaded object, so keep the pair the entries are built from.
    tenant_id, employee_id = tenant.id, employee.id
    session.add(_entry(tenant, employee, date(2026, 10, 1), date(2026, 10, 15)))
    await session.commit()

    # Touching on one shared day (the 15th) is an overlap: periods are inclusive.
    session.add(_entry(tenant, employee, date(2026, 10, 15), date(2026, 10, 30)))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()

    session.add(_entry_for(tenant_id, employee_id, date(2026, 10, 16), date(2026, 10, 31)))
    await session.commit()


async def test_rejected_entry_frees_its_period(session, tenant, employee):
    first = _entry(tenant, employee, date(2026, 10, 1), date(2026, 10, 31))
    session.add(first)
    await session.commit()
    first.status = HoursEntryStatus.REJECTED
    await session.commit()

    session.add(_entry(tenant, employee, date(2026, 10, 1), date(2026, 10, 31)))
    await session.commit()


async def test_other_consultant_same_period_ok(session, tenant, employee):
    other = Employee(
        tenant_id=tenant.id,
        employee_code="ACM002",
        first_name="Ravi",
        last_name="Iyer",
        work_email="ravi@acme.test",
        legal_entity_id=employee.legal_entity_id,
        location_id=employee.location_id,
        status=employee.status,
        date_of_joining=employee.date_of_joining,
    )
    session.add(other)
    await session.commit()

    session.add(_entry(tenant, employee, date(2026, 10, 1), date(2026, 10, 31)))
    session.add(_entry(tenant, other, date(2026, 10, 1), date(2026, 10, 31)))
    await session.commit()
