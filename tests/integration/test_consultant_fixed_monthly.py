"""Fixed-monthly consultants: a monthly fee prorated by calendar days, never their hours."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.models.consultant_fees import ConsultantMonthlyFee
from app.models.consultant_hours import HoursEntryStatus
from app.models.employee import ConsultantPayBasis, Employee, EmploymentType
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.employee import EmployeeCreate, EmployeeUpdate
from app.services import employee as employee_service
from app.services.consultants import fees, payouts
from app.services.consultants.hours import period_label
from tests.integration.conftest import as_caller, requires_db
from tests.integration.platform_support import bearer, client  # noqa: F401  (fixture)
from tests.integration.test_consultant_payout_run import (
    _confirmed,
    _consultant,
    _hours,
    _line,
    _second_hr,
    _setup,
)

pytestmark = requires_db

OCT_1 = date(2026, 10, 1)
OCT_31 = date(2026, 10, 31)


async def _fixed(session, tenant: Tenant, entity, code: str, *fee_rows: tuple[date, str]):
    """A fixed-monthly consultant (an hourly rate on file too, never used) with these fees."""
    person = await _consultant(
        session, tenant, entity, code, pay_basis=ConsultantPayBasis.FIXED_MONTHLY
    )
    for effective_from, amount in fee_rows:
        session.add(
            ConsultantMonthlyFee(
                tenant_id=tenant.id,
                employee_id=person.id,
                amount=Decimal(amount),
                effective_from=effective_from,
            )
        )
    await session.commit()
    return person


async def _run(session, hr_user: User, *, cut_off: date = OCT_31, pay_date=date(2026, 11, 5)):
    return await payouts.create_run(
        session, hr_user, legal_entity_id=None, cut_off=cut_off, pay_date=pay_date
    )


async def test_full_month_pays_the_fee_exactly(session, tenant, hr_user, legal_entity) -> None:
    await _setup(session, tenant, hr_user)
    person = await _fixed(session, tenant, legal_entity, "FIX1", (date(2026, 1, 1), "100000"))
    await _hours(session, tenant, person, monday=OCT_1, period_end=OCT_31, total="160")

    line = _line(await _run(session, hr_user), person)

    assert line["problems"] == [] and line["excluded"] is False
    assert line["gross"] == "100000.00"
    # The hours are kept for the record, never priced.
    assert line["hours"] == "160.00"
    assert line["entries"][0]["breakdown"] == [
        {
            "month": "2026-10",
            "days": 31,
            "days_in_month": 31,
            "fee": "100000.00",
            "fees": [{"fee": "100000.00", "days": 31}],
            "missing_days": 0,
            "amount": "100000.00",
        }
    ]


async def test_a_month_entered_week_by_week_pays_the_fee_exactly(
    session, tenant, hr_user, legal_entity
) -> None:
    await _setup(session, tenant, hr_user)
    person = await _fixed(session, tenant, legal_entity, "FIX1", (date(2026, 1, 1), "100000"))
    for first, last in ((1, 7), (8, 14), (15, 21), (22, 28), (29, 31)):
        await _hours(
            session,
            tenant,
            person,
            monday=date(2026, 10, first),
            period_end=date(2026, 10, last),
            total="40",
        )

    line = _line(await _run(session, hr_user), person)

    # Rounded per entry this would be 4 x 22,580.65 + 9,677.42 = 1,00,000.02.
    assert line["gross"] == "100000.00"
    amounts = [entry["gross"] for entry in line["entries"]]
    # Each week its own share rounded; the last entry of the month takes the remainder.
    assert amounts == ["22580.65", "22580.65", "22580.65", "22580.65", "9677.40"]
    assert [entry["breakdown"][0]["amount"] for entry in line["entries"]] == amounts


async def test_part_month_prorates_by_calendar_days(session, tenant, hr_user, legal_entity) -> None:
    await _setup(session, tenant, hr_user)
    person = await _fixed(session, tenant, legal_entity, "FIX1", (date(2026, 1, 1), "100000"))
    # Given day by day: the hours on each day still do not matter.
    await _hours(
        session,
        tenant,
        person,
        monday=date(2026, 10, 11),
        period_end=OCT_31,
        days=21,
        hours_per_day="3",
    )

    line = _line(await _run(session, hr_user), person)

    # 21 x 1,00,000 / 31
    assert line["gross"] == "67741.94"
    assert line["hours"] == "63.00"


async def test_period_crossing_month_end(session, tenant, hr_user, legal_entity) -> None:
    await _setup(session, tenant, hr_user)
    person = await _fixed(session, tenant, legal_entity, "FIX1", (date(2026, 1, 1), "93000"))
    await _hours(
        session, tenant, person, monday=date(2026, 10, 25), period_end=date(2026, 11, 5), total="70"
    )

    line = _line(await _run(session, hr_user, cut_off=date(2026, 11, 5)), person)

    parts = line["entries"][0]["breakdown"]
    assert [(p["month"], p["days"], p["days_in_month"], p["amount"]) for p in parts] == [
        ("2026-10", 7, 31, "21000.00"),
        ("2026-11", 5, 30, "15500.00"),
    ]
    assert line["gross"] == "36500.00"


async def test_fee_change_mid_month(session, tenant, hr_user, legal_entity) -> None:
    await _setup(session, tenant, hr_user)
    person = await _fixed(
        session,
        tenant,
        legal_entity,
        "FIX1",
        (date(2026, 1, 1), "90000"),
        (date(2026, 10, 15), "120000"),
    )
    await _hours(session, tenant, person, monday=OCT_1, period_end=OCT_31, total="160")

    line = _line(await _run(session, hr_user), person)

    # One month part: (14 x 90,000 + 17 x 1,20,000) / 31 = 33,00,000 / 31
    assert line["gross"] == "106451.61"
    (part,) = line["entries"][0]["breakdown"]
    assert part["fee"] is None
    assert part["fees"] == [{"fee": "90000.00", "days": 14}, {"fee": "120000.00", "days": 17}]


async def test_no_fee_holds_the_line(session, tenant, hr_user, legal_entity) -> None:
    await _setup(session, tenant, hr_user)
    # The fee starts halfway through the period, and the hourly rate is no stand-in.
    person = await _fixed(session, tenant, legal_entity, "FIX1", (date(2026, 10, 15), "100000"))
    await _hours(session, tenant, person, monday=OCT_1, period_end=OCT_31, total="160")

    line = _line(await _run(session, hr_user), person)

    assert line["excluded"] is True
    assert line["problems"] == [f"No monthly fee for {period_label(OCT_1, OCT_31)}"]
    assert line["gross"] == "0.00"
    (part,) = line["entries"][0]["breakdown"]
    # Shown as the days without a fee, never as a fee beside a nil amount.
    assert part["fees"] == [] and part["fee"] is None
    assert part["missing_days"] == 14
    assert part["amount"] == "0.00"


async def test_fixed_monthly_tds_and_net(session, tenant, hr_user, legal_entity) -> None:
    await _setup(session, tenant, hr_user)
    person = await _fixed(session, tenant, legal_entity, "FIX1", (date(2026, 1, 1), "100000"))
    await _hours(session, tenant, person, monday=OCT_1, period_end=OCT_31, total="160")

    line = _line(await _run(session, hr_user), person)

    # 194J at 10%, nothing paid earlier this year: over the threshold on its own.
    assert line["section"] == "194J_professional"
    assert line["tds_base"] == "100000.00"
    assert line["tds_amount"] == "10000.00"
    assert line["net_payable"] == "90000.00"


async def test_fee_added_after_pricing_blocks_approval(
    session, tenant, hr_user, legal_entity
) -> None:
    await _setup(session, tenant, hr_user)
    person = await _fixed(session, tenant, legal_entity, "FIX1", (date(2026, 1, 1), "100000"))
    await _hours(session, tenant, person, monday=OCT_1, period_end=OCT_31, total="160")
    run = await _run(session, hr_user)
    session.add(
        ConsultantMonthlyFee(
            tenant_id=tenant.id,
            employee_id=person.id,
            amount=Decimal("120000"),
            effective_from=date(2026, 10, 15),
        )
    )
    await session.commit()

    checker = await _second_hr(session, tenant)
    with pytest.raises(HTTPException) as stale:
        await payouts.approve_run(session, checker, uuid.UUID(run["id"]), confirm=_confirmed)
    assert stale.value.status_code == 409


async def test_pnl_fixed_monthly_by_work_month(session, tenant, hr_user, legal_entity) -> None:
    await _setup(session, tenant, hr_user)
    person = await _fixed(session, tenant, legal_entity, "FIX1", (date(2026, 1, 1), "93000"))
    await _hours(
        session, tenant, person, monday=date(2026, 10, 25), period_end=date(2026, 11, 5), total="70"
    )
    run = await _run(session, hr_user, cut_off=date(2026, 11, 5), pay_date=date(2026, 11, 15))
    checker = await _second_hr(session, tenant)
    await payouts.approve_run(session, checker, uuid.UUID(run["id"]), confirm=_confirmed)

    costs = await payouts.consultant_cost_by_month(
        session, hr_user, start=date(2026, 10, 1), end=date(2026, 11, 30)
    )
    assert costs == {
        (person.id, 2026, 10): Decimal("21000.00"),
        (person.id, 2026, 11): Decimal("15500.00"),
    }
    november = await payouts.consultant_cost_by_month(
        session, hr_user, start=date(2026, 11, 1), end=date(2026, 11, 30)
    )
    assert november == {(person.id, 2026, 11): Decimal("15500.00")}


# ── Pay basis on the record ─────────────────────────────────────────────────


def _new(code: str, kind: EmploymentType, **extra) -> EmployeeCreate:
    return EmployeeCreate(
        employee_code=code,
        first_name="Pat",
        work_email=f"{code.lower()}-{uuid.uuid4().hex[:6]}@acme.example.com",
        employment_type=kind,
        **extra,
    )


async def test_pay_basis_only_for_consultants_and_defaults_to_hourly(
    session, tenant, hr_user
) -> None:
    with pytest.raises(ValidationError):
        _new("E1", EmploymentType.FULL_TIME, pay_basis=ConsultantPayBasis.HOURLY)
    assert _new("C1", EmploymentType.CONSULTANT).pay_basis is ConsultantPayBasis.HOURLY

    caller = await as_caller(session, hr_user)
    person = await employee_service.create_employee(
        session,
        caller,
        _new("C2", EmploymentType.CONSULTANT, pay_basis=ConsultantPayBasis.FIXED_MONTHLY),
    )
    assert person.pay_basis is ConsultantPayBasis.FIXED_MONTHLY
    # Leaving consultant clears it.
    updated = await employee_service.update_employee(
        session, caller, person.id, EmployeeUpdate(employment_type=EmploymentType.FULL_TIME)
    )
    assert updated.pay_basis is None


async def test_pay_basis_change_refused_with_unpaid_hours(
    session, tenant, hr_user, legal_entity
) -> None:
    await _setup(session, tenant, hr_user)
    person = await _consultant(
        session, tenant, legal_entity, "CON1", pay_basis=ConsultantPayBasis.HOURLY
    )
    entry = await _hours(session, tenant, person, entry_status=HoursEntryStatus.DRAFT)
    caller = await as_caller(session, hr_user)
    change = EmployeeUpdate(pay_basis=ConsultantPayBasis.FIXED_MONTHLY)

    with pytest.raises(HTTPException) as refused:
        await employee_service.update_employee(session, caller, person.id, change)
    assert refused.value.status_code == 409
    assert refused.value.detail == (
        "Their hours not yet paid were entered on the hourly basis. Pay or reject them "
        "before changing how they're paid."
    )

    # Approved and in an approved (not yet paid) run: still not paid.
    entry.status = HoursEntryStatus.APPROVED
    await session.commit()
    run = await _run(session, hr_user, cut_off=date(2026, 9, 30), pay_date=date(2026, 10, 5))
    checker = await _second_hr(session, tenant)
    await payouts.approve_run(session, checker, uuid.UUID(run["id"]), confirm=_confirmed)
    with pytest.raises(HTTPException) as still:
        await employee_service.update_employee(session, caller, person.id, change)
    assert still.value.status_code == 409

    # Paid: the change goes through. A rejected entry never stood in the way.
    await payouts.mark_paid(
        session, checker, uuid.UUID(run["id"]), paid_on=date(2026, 10, 5), reference="UTR1"
    )
    await _hours(
        session, tenant, person, monday=date(2026, 10, 5), entry_status=HoursEntryStatus.REJECTED
    )
    updated = await employee_service.update_employee(session, caller, person.id, change)
    assert updated.pay_basis is ConsultantPayBasis.FIXED_MONTHLY


async def test_unset_pay_basis_counts_as_hourly(session, tenant, hr_user, legal_entity) -> None:
    await _setup(session, tenant, hr_user)
    person = await _consultant(session, tenant, legal_entity, "CON1")
    assert person.pay_basis is None
    await _hours(session, tenant, person, entry_status=HoursEntryStatus.DRAFT)
    caller = await as_caller(session, hr_user)
    # Hourly in all but name: setting it is no change.
    updated = await employee_service.update_employee(
        session, caller, person.id, EmployeeUpdate(pay_basis=ConsultantPayBasis.HOURLY)
    )
    assert updated.pay_basis is ConsultantPayBasis.HOURLY


# ── Monthly fees ────────────────────────────────────────────────────────────


async def test_fees_are_added_listed_and_looked_up(session, tenant, hr_user, legal_entity) -> None:
    person = await _fixed(session, tenant, legal_entity, "FIX1")
    caller = await as_caller(session, hr_user)

    first = await fees.add_fee(
        session,
        caller,
        fees.FeeIn(employee_id=person.id, amount=Decimal("90000"), effective_from=OCT_1),
    )
    assert first["amount"] == "90000.00" and first["effective_from"] == "2026-10-01"
    await fees.add_fee(
        session,
        caller,
        fees.FeeIn(
            employee_id=person.id,
            amount=Decimal("120000"),
            effective_from=date(2026, 10, 15),
            reason="Raise",
        ),
    )
    listed = await fees.list_fees(session, caller, person.id)
    assert [(f["amount"], f["effective_from"], f["reason"]) for f in listed] == [
        ("120000.00", "2026-10-15", "Raise"),
        ("90000.00", "2026-10-01", None),
    ]
    assert await fees.fee_on(session, caller, person.id, date(2026, 9, 30)) is None
    assert await fees.fee_on(session, caller, person.id, date(2026, 10, 14)) == Decimal("90000")
    assert await fees.fee_on(session, caller, person.id, OCT_31) == Decimal("120000")

    with pytest.raises(HTTPException) as taken:
        await fees.add_fee(
            session,
            caller,
            fees.FeeIn(employee_id=person.id, amount=Decimal("1"), effective_from=OCT_1),
        )
    assert taken.value.status_code == 409


async def test_fee_only_for_a_consultant(session, tenant, hr_user, legal_entity) -> None:
    caller = await as_caller(session, hr_user)
    staff = Employee(
        tenant_id=tenant.id,
        employee_code="E1",
        first_name="Pat",
        work_email="e1@acme-demo.com",
        employment_type=EmploymentType.FULL_TIME,
    )
    session.add(staff)
    await session.commit()
    with pytest.raises(HTTPException) as refused:
        await fees.add_fee(
            session,
            caller,
            fees.FeeIn(employee_id=staff.id, amount=Decimal("1000"), effective_from=OCT_1),
        )
    assert refused.value.status_code == 422


async def test_fee_routes_are_hr_only(session, tenant, hr_user, legal_entity, client) -> None:  # noqa: F811
    person = await _fixed(session, tenant, legal_entity, "FIX1")
    added = await client.post(
        "/api/v1/consultant-fees",
        json={"employee_id": str(person.id), "amount": "100000", "effective_from": "2026-10-01"},
        headers=bearer(hr_user),
    )
    assert added.status_code == 201, added.text
    listed = await client.get(f"/api/v1/consultant-fees/{person.id}", headers=bearer(hr_user))
    assert listed.status_code == 200
    assert [f["amount"] for f in listed.json()] == ["100000.00"]

    employee_login = User(
        tenant_id=tenant.id,
        email="someone@acme.test",
        password_hash="not-a-real-hash",
        full_name="Someone",
        role=UserRole.EMPLOYEE,
        is_active=True,
    )
    session.add(employee_login)
    await session.commit()
    await session.refresh(employee_login)
    refused = await client.get(
        f"/api/v1/consultant-fees/{person.id}", headers=bearer(employee_login)
    )
    assert refused.status_code == 403


async def test_attention_says_no_monthly_fee(session, tenant, hr_user, legal_entity) -> None:
    from app.services.consultants import attention

    await _setup(session, tenant, hr_user)
    person = await _fixed(session, tenant, legal_entity, "FIX1")

    listed = await attention.attention_list(session, hr_user)

    reasons = next(row["reasons"] for row in listed if row["employee_id"] == str(person.id))
    assert attention.NO_FEE in reasons
    assert attention.NO_RATE not in reasons
