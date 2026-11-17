"""Consultant hours entries: no overlaps, entered by finance, approved by a second person."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.access import UserAreaRights
from app.models.consultant_hours import ConsultantHoursEntry, HoursEntryStatus
from app.models.employee import Employee, EmploymentType
from app.models.notifications import Channel, Notification
from app.models.payroll import PayrollRun, PayrollRunStatus, PayrollRunType, Payslip
from app.models.user import User, UserRole
from app.services.consultants import hours
from tests.integration.conftest import as_caller, requires_db
from tests.integration.platform_support import client  # noqa: F401  (fixture)

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
    """Who was told in the app; each of them is emailed the same notice as well."""

    async def on(channel: Channel) -> list[uuid.UUID]:
        return list(
            (
                await session.scalars(
                    select(Notification.recipient_user_id).where(
                        Notification.event_key == event_key, Notification.channel == channel
                    )
                )
            ).all()
        )

    in_app = await on(Channel.IN_APP)
    assert sorted(await on(Channel.EMAIL)) == sorted(in_app)
    return in_app


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

    listed = await hours.list_entries(
        session, a, month="2026-10", status_filter=None, employee_id=None
    )
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
        session, b, month=None, status_filter=None, employee_id=None, waiting_for_me=True
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


async def _approved_payroll_run(session, tenant, legal_entity, person, month=10):
    run = PayrollRun(
        tenant_id=tenant.id,
        legal_entity_id=legal_entity.id,
        period_year=2026,
        period_month=month,
        effective_date=date(2026, month, 1),
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


async def test_approver_who_swapped_the_file_cannot_approve(session, tenant, hr_user, employee):
    person = await _consultant(session, employee)
    a = await as_caller(session, hr_user)
    b = await _user(session, tenant, "hr2@acme.test", UserRole.SUB_ADMIN)
    c = await _user(session, tenant, "hr3@acme.test", UserRole.SUB_ADMIN)
    entry = await hours.create_entry(session, a, _body(person))
    entry_id = uuid.UUID(entry["id"])
    first = await hours.add_attachment(session, a, entry_id, name="a.pdf", data=PDF)
    # B replaces the customer's file with their own and sends it for approval.
    await hours.remove_attachment(session, b, entry_id, uuid.UUID(first["attachments"][0]["id"]))
    await hours.add_attachment(session, b, entry_id, name="mine.pdf", data=PDF)
    submitted = await hours.submit_entry(session, a, entry_id)
    assert submitted["can_approve"] is False
    with pytest.raises(HTTPException) as refused:
        await hours.decide_entry(session, b, entry_id, approve=True, note=None)
    assert refused.value.status_code == 403
    done = await hours.decide_entry(session, c, entry_id, approve=True, note=None)
    assert done["status"] == "approved"


async def test_earlier_editor_cannot_approve_after_someone_else_resaves(
    session, tenant, hr_user, employee
):
    person = await _consultant(session, employee)
    a = await as_caller(session, hr_user)
    b = await _user(session, tenant, "hr2@acme.test", UserRole.SUB_ADMIN)
    c = await _user(session, tenant, "hr3@acme.test", UserRole.SUB_ADMIN)
    entry = await hours.create_entry(session, a, _body(person))
    entry_id = uuid.UUID(entry["id"])
    await hours.add_attachment(session, a, entry_id, name="a.pdf", data=PDF)
    await hours.update_entry(
        session, b, entry_id, _body(person, total_billable_hours=Decimal("150"))
    )
    # A re-saves only the customer reference: last_edited is A again.
    await hours.update_entry(
        session,
        a,
        entry_id,
        _body(person, total_billable_hours=Decimal("150"), customer_reference="TS-9"),
    )
    await hours.submit_entry(session, a, entry_id)
    with pytest.raises(HTTPException) as refused:
        await hours.decide_entry(session, b, entry_id, approve=True, note=None)
    assert refused.value.status_code == 403
    done = await hours.decide_entry(session, c, entry_id, approve=True, note=None)
    assert done["status"] == "approved"


async def test_approver_who_submitted_cannot_approve(session, tenant, hr_user, employee):
    person = await _consultant(session, employee)
    a = await as_caller(session, hr_user)
    b = await _user(session, tenant, "hr2@acme.test", UserRole.SUB_ADMIN)
    entry = await hours.create_entry(session, a, _body(person))
    entry_id = uuid.UUID(entry["id"])
    await hours.add_attachment(session, a, entry_id, name="a.pdf", data=PDF)
    await hours.submit_entry(session, b, entry_id)
    with pytest.raises(HTTPException) as refused:
        await hours.decide_entry(session, b, entry_id, approve=True, note=None)
    assert refused.value.status_code == 403
    # A is a maker too, so nobody is left to approve: it waits.
    listed = await hours.list_entries(session, a, month=None, status_filter=None, employee_id=None)
    assert listed[0]["approval_note"] == WAITING


async def test_reopen_keeps_earlier_makers_barred(session, tenant, hr_user, employee):
    person = await _consultant(session, employee)
    a = await as_caller(session, hr_user)
    b = await _user(session, tenant, "hr2@acme.test", UserRole.SUB_ADMIN)
    c = await _user(session, tenant, "hr3@acme.test", UserRole.SUB_ADMIN)
    submitted = await _submitted(session, a, person)
    entry_id = uuid.UUID(submitted["id"])
    await hours.decide_entry(session, b, entry_id, approve=True, note=None)
    await hours.reopen_entry(session, b, entry_id)
    await hours.submit_entry(session, b, entry_id)
    # A entered the figures and B reopened and resubmitted them: neither may approve.
    for maker in (a, b):
        with pytest.raises(HTTPException) as refused:
            await hours.decide_entry(session, maker, entry_id, approve=True, note=None)
        assert refused.value.status_code == 403
    done = await hours.decide_entry(session, c, entry_id, approve=True, note=None)
    assert done["status"] == "approved"


async def test_finance_approver_entry_falls_back_to_hr(session, tenant, hr_user, employee):
    person = await _consultant(session, employee)
    finance = await _user(session, tenant, "finance@acme.test", UserRole.EMPLOYEE)
    person.internal_approver_user_id = finance.id
    await session.commit()
    a = await as_caller(session, hr_user)

    # The finance approver entered the hours themself: HR approves instead.
    submitted = await _submitted(session, finance, person)
    entry_id = uuid.UUID(submitted["id"])
    assert submitted["can_approve"] is False and submitted["approval_note"] is None
    assert await _told(session, "consultant_hours.submitted") == [a.id]
    done = await hours.decide_entry(session, a, entry_id, approve=True, note=None)
    assert done["status"] == "approved"

    # When HR worked on it too, nobody is left; the note says who to ask.
    second = await hours.create_entry(
        session,
        finance,
        _body(person, period_start=date(2026, 11, 1), period_end=date(2026, 11, 30)),
    )
    second_id = uuid.UUID(second["id"])
    await hours.add_attachment(session, a, second_id, name="sheet.pdf", data=PDF)
    waiting = await hours.submit_entry(session, finance, second_id)
    assert waiting["approval_note"] == (
        "Needs another person to approve — ask HR, or set a different finance approver "
        "for this consultant."
    )


async def test_payroll_run_approved_after_entry_blocks_approval(
    session, tenant, hr_user, employee, legal_entity
):
    person = await _consultant(session, employee)
    a = await as_caller(session, hr_user)
    b = await _user(session, tenant, "hr2@acme.test", UserRole.SUB_ADMIN)
    submitted = await _submitted(session, a, person)
    entry_id = uuid.UUID(submitted["id"])
    await _approved_payroll_run(session, tenant, legal_entity, person)
    with pytest.raises(HTTPException) as refused:
        await hours.decide_entry(session, b, entry_id, approve=True, note=None)
    assert refused.value.status_code == 409
    assert refused.value.detail == "That period was already paid through payroll."
    # Rejecting is still possible, and resubmitting is refused the same way.
    rejected = await hours.decide_entry(session, b, entry_id, approve=False, note="Paid already")
    assert rejected["status"] == "rejected"
    with pytest.raises(HTTPException) as again:
        await hours.submit_entry(session, a, entry_id)
    assert again.value.status_code == 409


async def test_a_narrowed_sub_admin_is_not_an_approver(session, tenant, hr_user, employee):
    person = await _consultant(session, employee)
    a = await as_caller(session, hr_user)
    b = await _user(session, tenant, "hr2@acme.test", UserRole.SUB_ADMIN)
    session.add(
        UserAreaRights(tenant_id=tenant.id, user_id=b.id, area="payroll", can_approve=False)
    )
    await session.commit()
    await _submitted(session, a, person)
    listed = await hours.list_entries(session, a, month=None, status_filter=None, employee_id=None)
    assert listed[0]["approval_note"] == WAITING
    assert await _told(session, "consultant_hours.submitted") == []


async def test_a_finance_approver_who_is_the_consultant_falls_back_to_hr(
    session, tenant, hr_user, employee
):
    person = await _consultant(session, employee)
    own_login = await _user(session, tenant, "anita@acme.test", UserRole.EMPLOYEE)
    own_login.employee_id = person.id
    person.internal_approver_user_id = own_login.id
    await session.commit()
    a = await as_caller(session, hr_user)
    b = await _user(session, tenant, "hr2@acme.test", UserRole.SUB_ADMIN)
    await _submitted(session, a, person)
    assert await _told(session, "consultant_hours.submitted") == [b.id]


async def test_people_without_a_right_to_the_hours_see_nothing(session, tenant, hr_user, employee):
    person = await _consultant(session, employee)
    a = await as_caller(session, hr_user)
    submitted = await _submitted(session, a, person)
    entry_id = uuid.UUID(submitted["id"])
    stranger = await _user(session, tenant, "someone@acme.test", UserRole.EMPLOYEE)
    own_login = await _user(session, tenant, "anita@acme.test", UserRole.EMPLOYEE)
    own_login.employee_id = person.id
    await session.commit()
    for who in (stranger, own_login):
        assert (
            await hours.list_entries(session, who, month=None, status_filter=None, employee_id=None)
            == []
        )
        with pytest.raises(HTTPException) as hidden:
            await hours.get_entry(session, who, entry_id)
        assert hidden.value.status_code == 404
        with pytest.raises(HTTPException) as nope:
            await hours.create_entry(session, who, _body(person))
        assert nope.value.status_code == 403
        assert person.full_name not in str(nope.value.detail)
        with pytest.raises(HTTPException) as no_decide:
            await hours.decide_entry(session, who, entry_id, approve=True, note=None)
        assert no_decide.value.status_code == 404


async def test_consultants_the_caller_may_act_on(
    client,  # noqa: F811
    session,
    tenant,
    hr_user,
    employee,
    legal_entity,
):
    from tests.integration.platform_support import bearer

    person = await _consultant(session, employee)
    staff = Employee(
        tenant_id=tenant.id,
        employee_code="ACM002",
        first_name="Ravi",
        work_email="ravi@acme.test",
        legal_entity_id=legal_entity.id,
    )
    other = Employee(
        tenant_id=tenant.id,
        employee_code="CON9",
        first_name="Zoya",
        work_email="zoya@acme.test",
        legal_entity_id=legal_entity.id,
        employment_type=EmploymentType.CONSULTANT,
    )
    session.add_all([staff, other])
    finance = await _user(session, tenant, "finance@acme.test", UserRole.EMPLOYEE)
    stranger = await _user(session, tenant, "someone@acme.test", UserRole.EMPLOYEE)
    person.internal_approver_user_id = finance.id
    await session.commit()
    a = await as_caller(session, hr_user)

    everyone = await hours.list_consultants(session, a)
    assert [row["code"] for row in everyone] == ["ACM001", "CON9"]
    assert everyone[0] == {
        "employee_id": str(person.id),
        "code": "ACM001",
        "name": "Anita Sharma",
        "project": None,
        "pay_basis": "hourly",
    }
    assert [row["code"] for row in await hours.list_consultants(session, finance)] == ["ACM001"]
    assert await hours.list_consultants(session, stranger) == []

    over_http = await client.get("/api/v1/consultant-hours/consultants", headers=bearer(finance))
    assert over_http.status_code == 200
    assert [row["code"] for row in over_http.json()] == ["ACM001"]


async def test_submit_notification_links_to_the_hours_screen(session, tenant, hr_user, employee):
    person = await _consultant(session, employee)
    a = await as_caller(session, hr_user)
    await _user(session, tenant, "hr2@acme.test", UserRole.SUB_ADMIN)
    await _submitted(session, a, person)
    payloads = (
        await session.scalars(
            select(Notification.payload).where(
                Notification.event_key == "consultant_hours.submitted"
            )
        )
    ).all()
    assert payloads and all(p["link"] == "/payroll/consultant-hours" for p in payloads)


# ── Status grid, the missing-month check, the customer route retired ────────


async def test_a_status_grid_with_a_typed_total_is_a_total_only_entry(session, hr_user, employee):
    person = await _consultant(session, employee)
    a = await as_caller(session, hr_user)
    days = [
        {
            "date": date(2026, 10, 1) + timedelta(days=i),
            "hours": "0",
            "status": "week_off"
            if (date(2026, 10, 1) + timedelta(days=i)).weekday() >= 5
            else "present",
        }
        for i in range(31)
    ]
    made = await hours.create_entry(session, a, _body(person, days=days))
    assert made["total_billable_hours"] == "170.00"
    assert len(made["days"]) == 31


async def test_a_status_grid_total_over_24_hours_a_day_is_refused(session, hr_user, employee):
    person = await _consultant(session, employee)
    a = await as_caller(session, hr_user)
    days = [
        {"date": date(2026, 10, 1) + timedelta(days=i), "hours": "0", "status": "present"}
        for i in range(31)
    ]
    with pytest.raises(HTTPException) as refused:
        await hours.create_entry(
            session, a, _body(person, days=days, total_billable_hours=Decimal("745"))
        )
    assert refused.value.status_code == 422
    assert "more than 24 hours a day" in refused.value.detail


async def test_editing_without_a_project_keeps_the_entry_project(
    session, tenant, hr_user, employee
):
    from app.models.timesheets import Project

    first = Project(tenant_id=tenant.id, code="ALPHA", name="Alpha")
    later = Project(tenant_id=tenant.id, code="BETA", name="Beta")
    session.add_all([first, later])
    await session.flush()
    person = await _consultant(session, employee)
    person.project_id = first.id
    await session.commit()
    a = await as_caller(session, hr_user)
    made = await hours.create_entry(session, a, _body(person))
    assert made["project_id"] == str(first.id)

    person.project_id = later.id
    await session.commit()
    edited = await hours.update_entry(
        session, a, uuid.UUID(made["id"]), _body(person, total_billable_hours=Decimal("160"))
    )
    assert edited["project_id"] == str(first.id)


async def test_attention_flags_missing_last_month(session, tenant, hr_user, legal_entity):
    from app.models.employee import ApproverRoute, EmploymentStatus
    from app.services.attendance.service import local_today
    from app.services.consultants import attention

    today = await local_today(session, hr_user)
    first_this_month = today.replace(day=1)
    last_month_end = first_this_month - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)
    expected = f"No hours entered for {last_month_start:%b %Y}"

    def person(code: str, **extra) -> Employee:
        return Employee(
            tenant_id=tenant.id,
            employee_code=code,
            first_name=code.title(),
            work_email=f"{code.lower()}@acme-demo.com",
            legal_entity_id=legal_entity.id,
            status=EmploymentStatus.ACTIVE,
            employment_type=EmploymentType.CONSULTANT,
            timesheet_approver_route=ApproverRoute.INTERNAL,
            internal_approver_user_id=hr_user.id,
            **extra,
        )

    nothing = person("CONA")
    rejected = person("CONB")
    covered = person("CONC")
    joined_now = person("COND", date_of_joining=first_this_month)
    left_before = person("CONE", date_of_exit=last_month_start - timedelta(days=1))
    session.add_all([nothing, rejected, covered, joined_now, left_before])
    await session.flush()
    session.add(
        _entry(tenant, rejected, last_month_start, last_month_end, status=HoursEntryStatus.REJECTED)
    )
    # A draft touching just the last day of the month counts as entered.
    session.add(_entry(tenant, covered, last_month_end, last_month_end + timedelta(days=3)))
    await session.commit()

    listed = {
        row["code"]: row["reasons"] for row in await attention.attention_list(session, hr_user)
    }
    assert expected in listed["CONA"]
    assert expected in listed["CONB"]
    assert expected not in listed.get("CONC", [])
    assert expected not in listed.get("COND", [])
    assert expected not in listed.get("CONE", [])


async def test_customer_route_refused_on_record(session, tenant, hr_user, employee):
    from pydantic import ValidationError

    from app.models.employee import ApproverRoute
    from app.schemas.employee import EmployeeCreate, EmployeeUpdate

    message = "Customer approval by email is no longer used."
    with pytest.raises(ValidationError) as on_create:
        EmployeeCreate(
            employee_code="CONX",
            first_name="Pat",
            work_email="pat.conx@acme.example.com",
            employment_type=EmploymentType.CONSULTANT,
            timesheet_approver_route=ApproverRoute.CUSTOMER,
            customer_contact_name="Asha",
            customer_contact_email="asha@client.example.com",
        )
    assert message in str(on_create.value)
    with pytest.raises(ValidationError) as on_update:
        EmployeeUpdate(timesheet_approver_route=ApproverRoute.CUSTOMER)
    assert message in str(on_update.value)
    # The internal route is still fine.
    assert EmployeeUpdate(timesheet_approver_route=ApproverRoute.INTERNAL)


async def test_customer_route_refused_over_http(client, session, tenant, hr_user, employee):  # noqa: F811
    from tests.integration.platform_support import bearer

    await _consultant(session, employee)
    refused = await client.patch(
        f"/api/v1/employees/{employee.id}",
        json={"timesheet_approver_route": "customer"},
        headers=bearer(hr_user),
    )
    assert refused.status_code == 422
    assert "Customer approval by email is no longer used." in refused.text


async def test_public_timesheet_routes_are_gone(client):  # noqa: F811
    from app.main import app

    paths = {getattr(route, "path", "") for route in app.routes}
    assert not any(path.startswith("/api/v1/public/timesheets") for path in paths)
    assert (await client.get("/api/v1/public/timesheets/x")).status_code == 404
    gone = await client.post("/api/v1/public/timesheets/x/decision", json={"approve": True})
    assert gone.status_code == 404
