"""Consultant hours entries: no overlaps, entered by finance, approved by a second person."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.consultant_hours import ConsultantHoursEntry, HoursEntryStatus
from app.models.employee import Employee, EmploymentType
from app.models.notifications import Notification
from app.models.payroll import PayrollRun, PayrollRunStatus, PayrollRunType, Payslip
from app.models.user import User, UserRole
from app.services.consultants import hours
from tests.integration.conftest import as_caller, requires_db

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


async def _refused(session, entry):
    session.add(entry)
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_period_and_total_checks(session, tenant, employee):
    tenant_id, employee_id = tenant.id, employee.id

    # 30 days apart (31 days counted inclusively) is the longest allowed period.
    session.add(_entry_for(tenant_id, employee_id, date(2026, 10, 1), date(2026, 10, 31)))
    await session.commit()

    # 31 days apart is refused.
    await _refused(
        session, _entry_for(tenant_id, employee_id, date(2026, 11, 1), date(2026, 12, 2))
    )
    # End before start is refused.
    await _refused(session, _entry_for(tenant_id, employee_id, date(2027, 2, 10), date(2027, 2, 9)))
    # No billable hours is refused.
    zero = _entry_for(tenant_id, employee_id, date(2027, 3, 1), date(2027, 3, 31))
    zero.total_billable_hours = Decimal("0")
    await _refused(session, zero)


# ── Entering, submitting and approving ──────────────────────────────────────

PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
WAITING = "Needs another person to approve — set a finance approver for this consultant."


async def _user(session, tenant, email, role=UserRole.ADMIN) -> User:
    user = User(
        tenant_id=tenant.id,
        email=email,
        password_hash="not-a-real-hash",
        full_name=email.split("@")[0].title(),
        role=role,
        is_active=True,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return await as_caller(session, user)


async def _consultant(session, employee) -> Employee:
    employee.employment_type = EmploymentType.CONSULTANT
    await session.commit()
    return employee


def _body(employee, **extra):
    base = {
        "employee_id": employee.id,
        "period_start": date(2026, 10, 1),
        "period_end": date(2026, 10, 31),
        "total_billable_hours": Decimal("170"),
    }
    base.update(extra)
    return hours.HoursIn(**base)


async def _submitted(session, enterer, employee) -> dict:
    entry = await hours.create_entry(session, enterer, _body(employee))
    entry_id = uuid.UUID(entry["id"])
    await hours.add_attachment(session, enterer, entry_id, name="sheet.pdf", data=PDF)
    return await hours.submit_entry(session, enterer, entry_id)


async def _told(session, event_key) -> list[uuid.UUID]:
    return list(
        (
            await session.scalars(
                select(Notification.recipient_user_id).where(Notification.event_key == event_key)
            )
        ).all()
    )


async def test_finance_enters_submits_and_another_person_approves(
    session, tenant, hr_user, employee
):
    person = await _consultant(session, employee)
    a = await as_caller(session, hr_user)
    b = await _user(session, tenant, "finance@acme.test", UserRole.EMPLOYEE)
    person.internal_approver_user_id = b.id
    await session.commit()

    submitted = await _submitted(session, a, person)
    assert submitted["status"] == "submitted"
    assert await _told(session, "consultant_hours.submitted") == [b.id]

    with pytest.raises(HTTPException) as refused:
        await hours.decide_entry(session, a, uuid.UUID(submitted["id"]), approve=True, note=None)
    assert refused.value.status_code == 403

    decided = await hours.decide_entry(
        session, b, uuid.UUID(submitted["id"]), approve=True, note=None
    )
    assert decided["status"] == "approved"
    assert decided["decided_by_user_id"] == str(b.id)
    assert await _told(session, "consultant_hours.decided") == [a.id]


async def test_single_hr_user_entry_waits(session, tenant, hr_user, employee):
    person = await _consultant(session, employee)
    a = await as_caller(session, hr_user)
    submitted = await _submitted(session, a, person)

    listed = await hours.list_entries(session, a, month="2026-10", status=None, employee_id=None)
    assert [item["can_approve"] for item in listed] == [False]
    assert listed[0]["approval_note"] == WAITING
    with pytest.raises(HTTPException) as refused:
        await hours.decide_entry(session, a, uuid.UUID(submitted["id"]), approve=True, note=None)
    assert refused.value.status_code == 403
    assert await _told(session, "consultant_hours.submitted") == []


async def test_a_second_hr_user_can_approve(session, tenant, hr_user, employee):
    person = await _consultant(session, employee)
    a = await as_caller(session, hr_user)
    b = await _user(session, tenant, "hr2@acme.test", UserRole.SUB_ADMIN)
    submitted = await _submitted(session, a, person)
    assert await _told(session, "consultant_hours.submitted") == [b.id]
    waiting = await hours.list_entries(
        session, b, month=None, status=None, employee_id=None, waiting_for_me=True
    )
    assert [item["can_approve"] for item in waiting] == [True]
    assert waiting[0]["approval_note"] is None
    done = await hours.decide_entry(session, b, uuid.UUID(submitted["id"]), approve=True, note=None)
    assert done["status"] == "approved"


async def test_submit_needs_an_attachment(session, hr_user, employee):
    person = await _consultant(session, employee)
    a = await as_caller(session, hr_user)
    entry = await hours.create_entry(session, a, _body(person))
    with pytest.raises(HTTPException) as refused:
        await hours.submit_entry(session, a, uuid.UUID(entry["id"]))
    assert refused.value.status_code == 422


async def test_reject_needs_reason_and_reopens_for_edit(session, tenant, hr_user, employee):
    person = await _consultant(session, employee)
    a = await as_caller(session, hr_user)
    b = await _user(session, tenant, "hr2@acme.test", UserRole.SUB_ADMIN)
    submitted = await _submitted(session, a, person)
    entry_id = uuid.UUID(submitted["id"])

    with pytest.raises(HTTPException) as refused:
        await hours.decide_entry(session, b, entry_id, approve=False, note="  ")
    assert refused.value.status_code == 422
    rejected = await hours.decide_entry(session, b, entry_id, approve=False, note="Wrong hours")
    assert rejected["status"] == "rejected"
    assert rejected["decision_note"] == "Wrong hours"

    fixed = await hours.update_entry(
        session, a, entry_id, _body(person, total_billable_hours=Decimal("160"))
    )
    assert fixed["status"] == "draft" and fixed["total_billable_hours"] == "160.00"
    resent = await hours.submit_entry(session, a, entry_id)
    assert resent["status"] == "submitted" and resent["decision_note"] is None
    approved = await hours.decide_entry(session, b, entry_id, approve=True, note=None)
    assert approved["status"] == "approved"

    # Edits stop once approved; only HR reopens.
    with pytest.raises(HTTPException) as locked:
        await hours.update_entry(session, a, entry_id, _body(person))
    assert locked.value.status_code == 409
    reopened = await hours.reopen_entry(session, a, entry_id)
    assert reopened["status"] == "draft" and reopened["decided_at"] is None


async def test_period_paid_through_payroll_is_refused(
    session, tenant, hr_user, employee, legal_entity
):
    person = await _consultant(session, employee)
    run = PayrollRun(
        tenant_id=tenant.id,
        legal_entity_id=legal_entity.id,
        period_year=2026,
        period_month=10,
        effective_date=date(2026, 10, 1),
        run_type=PayrollRunType.REGULAR,
        status=PayrollRunStatus.APPROVED,
    )
    session.add(run)
    await session.flush()
    session.add(
        Payslip(
            tenant_id=tenant.id,
            run_id=run.id,
            employee_id=person.id,
            working_days=Decimal(31),
            paid_days=Decimal(31),
            gross_earnings=Decimal(1),
            total_deductions=Decimal(0),
            net_pay=Decimal(1),
            employer_cost=Decimal(1),
        )
    )
    await session.commit()
    a = await as_caller(session, hr_user)
    with pytest.raises(HTTPException) as refused:
        await hours.create_entry(session, a, _body(person))
    assert refused.value.status_code == 409
    assert refused.value.detail == "That period was already paid through payroll."
    # A period touching October only on its last day is refused too; November is fine.
    with pytest.raises(HTTPException):
        await hours.create_entry(
            session,
            a,
            _body(person, period_start=date(2026, 10, 31), period_end=date(2026, 11, 10)),
        )
    ok = await hours.create_entry(
        session, a, _body(person, period_start=date(2026, 11, 1), period_end=date(2026, 11, 30))
    )
    assert ok["status"] == "draft"


async def test_overlap_is_a_plain_409(session, hr_user, employee):
    person = await _consultant(session, employee)
    a = await as_caller(session, hr_user)
    await hours.create_entry(
        session, a, _body(person, period_start=date(2026, 10, 1), period_end=date(2026, 10, 15))
    )
    with pytest.raises(HTTPException) as refused:
        await hours.create_entry(
            session,
            a,
            _body(person, period_start=date(2026, 10, 15), period_end=date(2026, 10, 30)),
        )
    assert refused.value.status_code == 409
    assert refused.value.detail.startswith("Hours for Anita Sharma already cover part of ")


async def test_day_grid_total_must_match(session, hr_user, employee):
    person = await _consultant(session, employee)
    a = await as_caller(session, hr_user)
    days = [{"date": date(2026, 10, 1) + timedelta(days=i), "hours": "8"} for i in range(22)]
    with pytest.raises(HTTPException) as refused:
        await hours.create_entry(session, a, _body(person, days=days))  # 22 x 8 = 176, total 170
    assert refused.value.status_code == 422
    made = await hours.create_entry(session, a, _body(person, days=days, total_billable_hours=None))
    assert made["total_billable_hours"] == "176.00"
    assert len(made["days"]) == 22


async def test_non_consultant_refused(session, hr_user, employee):
    a = await as_caller(session, hr_user)
    with pytest.raises(HTTPException) as refused:
        await hours.create_entry(session, a, _body(employee))
    assert refused.value.status_code == 422


async def test_attachments_sheet_types_and_download(session, hr_user, employee):
    person = await _consultant(session, employee)
    a = await as_caller(session, hr_user)
    entry = await hours.create_entry(session, a, _body(person))
    entry_id = uuid.UUID(entry["id"])
    csv = await hours.add_attachment(
        session, a, entry_id, name="hours.csv", data=b"date,hours\n2026-10-01,8\n"
    )
    assert csv["attachments"][0]["content_type"] == "text/csv"
    with pytest.raises(HTTPException) as refused:
        await hours.add_attachment(session, a, entry_id, name="x.pdf", data=b"<html>hi\x00</html>")
    assert refused.value.status_code == 422
    attachment_id = uuid.UUID(csv["attachments"][0]["id"])
    got = await hours.attachment_file(session, a, entry_id, attachment_id)
    assert got == ("hours.csv", "text/csv", b"date,hours\n2026-10-01,8\n")
    left = await hours.remove_attachment(session, a, entry_id, attachment_id)
    assert left["attachments"] == []
