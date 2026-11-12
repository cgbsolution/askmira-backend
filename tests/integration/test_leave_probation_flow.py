"""Casual Leave from November, and no leave while on probation — against a real database.

The set-up asked for on 2026-11-13 for CGB and Artbox: Casual Leave at 1.25 a
month, starting with November and December (2.5 days for the rest of 2026),
and nobody earns leave in their first six months. Built the way HR would build
it — two policy versions through the service, the probation through the leave
settings — and read back the way people see it: their own cards, HR's team
table and its CSV, the balance history, and a leave request.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.employee import Employee, EmploymentStatus
from app.models.leave import LeavePolicy, LeaveType
from app.models.org import LegalEntity, Location
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.leave import LeavePolicyCreate, LeaveRequestCreate, LeaveSettingsUpdate
from app.services.leave import balance_report, ledger
from app.services.leave import service as leave_service
from tests.integration.conftest import requires_db

pytestmark = requires_db

TODAY = date(2026, 11, 13)


@pytest.fixture(autouse=True)
def pinned_today(monkeypatch: pytest.MonkeyPatch) -> None:
    """The day the user asked for this, so the figures do not drift with the calendar."""
    monkeypatch.setattr(leave_service, "local_today", lambda: TODAY)
    monkeypatch.setattr(leave_service, "_today_in", lambda _zone: TODAY)


async def _person(
    session: AsyncSession,
    tenant: Tenant,
    entity: LegalEntity,
    location: Location,
    *,
    code: str,
    joined: date,
    role: UserRole | None = None,
) -> tuple[Employee, User | None]:
    employee = Employee(
        tenant_id=tenant.id,
        employee_code=code,
        first_name=code.title(),
        work_email=f"{code.lower()}@acme.test",
        legal_entity_id=entity.id,
        location_id=location.id,
        status=EmploymentStatus.ACTIVE,
        date_of_joining=joined,
    )
    session.add(employee)
    await session.flush()
    user = None
    if role is not None:
        user = User(
            tenant_id=tenant.id,
            email=f"{code.lower()}@acme.test",
            password_hash="not-a-real-hash",
            full_name=code.title(),
            role=role,
            is_active=True,
            employee_id=employee.id,
        )
        session.add(user)
    await session.commit()
    return employee, user


@pytest.fixture
async def org(session: AsyncSession, tenant: Tenant, legal_entity: LegalEntity, location: Location):
    people: dict[str, Employee] = {}
    admin_emp, admin = await _person(
        session,
        tenant,
        legal_entity,
        location,
        code="ADMIN",
        joined=date(2020, 1, 1),
        role=UserRole.ADMIN,
    )
    people["admin"] = admin_emp
    hr_emp, hr = await _person(
        session,
        tenant,
        legal_entity,
        location,
        code="SUBHR",
        joined=date(2021, 4, 1),
        role=UserRole.SUB_ADMIN,
    )
    people["subhr"] = hr_emp
    for key, joined in (
        ("veteran", date(2022, 3, 7)),
        ("ends_10_nov", date(2026, 5, 10)),
        ("ends_20_nov", date(2026, 5, 20)),
    ):
        people[key], _ = await _person(
            session, tenant, legal_entity, location, code=key.upper(), joined=joined
        )
    people["newbie"], newbie = await _person(
        session,
        tenant,
        legal_entity,
        location,
        code="NEWBIE",
        joined=date(2026, 10, 26),
        role=UserRole.EMPLOYEE,
    )

    # As both live workspaces were: Casual Leave on a 0-day policy from January.
    casual = LeaveType(tenant_id=tenant.id, name="Casual Leave", code="CL")
    session.add(casual)
    await session.flush()
    session.add(
        LeavePolicy(
            tenant_id=tenant.id,
            leave_type_id=casual.id,
            name="Casual Leave",
            annual_quota_days=Decimal(0),
            accrual_frequency="annual",
            effective_from=date(2026, 1, 1),
        )
    )
    await session.commit()
    return {"admin": admin, "hr": hr, "newbie": newbie, "people": people, "cl": casual}


async def _set_up_casual_leave(session: AsyncSession, org) -> None:
    """The two versions put on live: 2.5 days up front for Nov-Dec, then 1.25 a month."""
    await leave_service.create_policy(
        session,
        org["admin"],
        LeavePolicyCreate(
            leave_type_id=org["cl"].id,
            name="Casual Leave - Nov-Dec 2026 (1.25 a month)",
            annual_quota_days=Decimal(15),
            accrual_frequency="annual",
            effective_from=date(2026, 11, 1),
        ),
    )
    await leave_service.create_policy(
        session,
        org["admin"],
        LeavePolicyCreate(
            leave_type_id=org["cl"].id,
            name="Casual Leave - 1.25 a month",
            annual_quota_days=Decimal(15),
            accrual_frequency="monthly",
            effective_from=date(2027, 1, 1),
        ),
    )


async def _available(session: AsyncSession, org, key: str, year: int = 2026) -> Decimal:
    lines = await leave_service.balances(
        session, org["admin"], employee_id=org["people"][key].id, year=year
    )
    (casual,) = [line for line in lines if line.leave_type_code == "CL"]
    return casual.available


async def test_the_versions_chain_without_gaps_or_overlap(session: AsyncSession, org) -> None:
    await _set_up_casual_leave(session, org)
    versions = (
        await session.scalars(
            select(LeavePolicy)
            .where(LeavePolicy.leave_type_id == org["cl"].id)
            .order_by(LeavePolicy.effective_from)
        )
    ).all()
    assert [(v.effective_from, v.effective_to) for v in versions] == [
        (date(2026, 1, 1), date(2026, 10, 31)),
        (date(2026, 11, 1), date(2026, 12, 31)),
        (date(2027, 1, 1), None),
    ]


async def test_without_probation_everybody_has_two_and_a_half_days(
    session: AsyncSession, org
) -> None:
    before = await _available(session, org, "veteran")
    assert before == Decimal(0)
    await _set_up_casual_leave(session, org)
    for key in ("admin", "veteran", "ends_10_nov", "ends_20_nov", "newbie"):
        assert await _available(session, org, key) == Decimal("2.50"), key
    # January 2027 is the first monthly credit.
    assert await _available(session, org, "veteran", 2027) == Decimal("1.25")


async def test_probation_holds_leave_back_for_six_months(session: AsyncSession, org) -> None:
    await _set_up_casual_leave(session, org)
    settings = await leave_service.update_settings(
        session, org["admin"], LeaveSettingsUpdate(probation_months=6)
    )
    assert settings.probation_months == 6

    assert await _available(session, org, "veteran") == Decimal("2.50")
    assert await _available(session, org, "ends_10_nov") == Decimal("2.50")
    assert await _available(session, org, "ends_20_nov") == Decimal(0)
    assert await _available(session, org, "newbie") == Decimal(0)

    newbie = await leave_service.balances(
        session, org["admin"], employee_id=org["people"]["newbie"].id, year=2026
    )
    assert newbie[0].probation_until == date(2027, 4, 26)
    veteran = await leave_service.balances(
        session, org["admin"], employee_id=org["people"]["veteran"].id, year=2026
    )
    assert veteran[0].probation_until is None

    entry = await session.scalar(select(AuditLog).where(AuditLog.entity_type == "leave_settings"))
    assert entry is not None
    assert entry.changes["after"] == {"leave_probation_months": 6}


async def test_probation_ending_after_the_15th_earns_from_the_day_it_ends(
    session: AsyncSession, org, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _set_up_casual_leave(session, org)
    await leave_service.update_settings(
        session, org["admin"], LeaveSettingsUpdate(probation_months=6)
    )
    later = date(2026, 11, 20)
    monkeypatch.setattr(leave_service, "local_today", lambda: later)
    monkeypatch.setattr(leave_service, "_today_in", lambda _zone: later)
    # Probation ended today, after the 15th: December's 1.25, credited now.
    assert await _available(session, org, "ends_20_nov") == Decimal("1.25")


async def test_only_an_admin_sets_the_probation(session: AsyncSession, org) -> None:
    with pytest.raises(HTTPException) as refused:
        await leave_service.update_settings(
            session, org["hr"], LeaveSettingsUpdate(probation_months=6)
        )
    assert refused.value.status_code == 403
    # Saving the other settings with the same value is not a change.
    same = await leave_service.update_settings(
        session, org["hr"], LeaveSettingsUpdate(probation_months=0, comp_off_expiry_days=30)
    )
    assert (same.probation_months, same.comp_off_expiry_days) == (0, 30)

    for bad in (-1, 25):
        with pytest.raises(ValidationError):
            LeaveSettingsUpdate(probation_months=bad)


async def test_hrs_table_shows_who_is_on_probation(session: AsyncSession, org) -> None:
    await _set_up_casual_leave(session, org)
    nobody_waits = await balance_report.team_balances(session, org["admin"], year=2026)
    assert (nobody_waits.probation_months, nobody_waits.on_probation) == (0, 0)
    empty = await balance_report.team_balances(session, org["admin"], year=2026, probation="only")
    assert empty.total == 0

    await leave_service.update_settings(
        session, org["admin"], LeaveSettingsUpdate(probation_months=6)
    )
    table = await balance_report.team_balances(session, org["admin"], year=2026)
    assert (table.probation_months, table.on_probation, table.total) == (6, 2, 6)
    rows = {row.employee_code: row for row in table.items}
    assert rows["NEWBIE"].probation_until == date(2027, 4, 26)
    assert rows["NEWBIE"].date_of_joining == date(2026, 10, 26)
    assert rows["ENDS_20_NOV"].probation_until == date(2026, 11, 20)
    assert rows["ENDS_10_NOV"].probation_until is None
    assert rows["NEWBIE"].balances[0].available == Decimal(0)
    assert rows["VETERAN"].balances[0].available == Decimal("2.50")

    waiting = await balance_report.team_balances(session, org["admin"], year=2026, probation="only")
    assert sorted(r.employee_code for r in waiting.items) == ["ENDS_20_NOV", "NEWBIE"]
    assert waiting.on_probation == 2
    past = await balance_report.team_balances(session, org["admin"], year=2026, probation="exclude")
    assert sorted(r.employee_code for r in past.items) == [
        "ADMIN",
        "ENDS_10_NOV",
        "SUBHR",
        "VETERAN",
    ]
    # The count follows the search, not the tab.
    searched = await balance_report.team_balances(session, org["admin"], year=2026, q="newbie")
    assert (searched.total, searched.on_probation) == (1, 1)


async def test_the_csv_says_until_when(session: AsyncSession, org) -> None:
    await _set_up_casual_leave(session, org)
    await leave_service.update_settings(
        session, org["admin"], LeaveSettingsUpdate(probation_months=6)
    )
    content = await balance_report.export_csv(session, org["admin"], year=2026, probation="only")
    await session.commit()
    lines = content.strip().splitlines()
    assert lines[0].endswith(",available,on_probation_until")
    assert len(lines) == 3
    newbie = next(line for line in lines if line.startswith("NEWBIE,"))
    assert newbie.endswith(",0.00,2027-04-26")


async def test_someone_on_probation_is_told_when_leave_starts(session: AsyncSession, org) -> None:
    await _set_up_casual_leave(session, org)
    await leave_service.update_settings(
        session, org["admin"], LeaveSettingsUpdate(probation_months=6)
    )
    preview = await leave_service.preview(
        session,
        org["newbie"],
        LeaveRequestCreate(
            leave_type_id=org["cl"].id,
            start_date=date(2026, 11, 23),
            end_date=date(2026, 11, 23),
        ),
    )
    assert preview.allowed is False
    assert any("starts building up on 26 Apr 2027" in p for p in preview.problems)
    with pytest.raises(HTTPException) as refused:
        await leave_service.apply(
            session,
            org["newbie"],
            LeaveRequestCreate(
                leave_type_id=org["cl"].id,
                start_date=date(2026, 11, 23),
                end_date=date(2026, 11, 23),
            ),
        )
    assert refused.value.status_code == 422


async def test_an_hr_credit_still_works_during_probation(session: AsyncSession, org) -> None:
    """Probation stops accrual, not HR: a deliberate credit is honoured."""
    from app.schemas.leave import BalanceAdjustmentCreate

    await _set_up_casual_leave(session, org)
    await leave_service.update_settings(
        session, org["admin"], LeaveSettingsUpdate(probation_months=6)
    )
    await ledger.adjust(
        session,
        org["admin"],
        BalanceAdjustmentCreate(
            employee_id=org["people"]["newbie"].id,
            leave_type_id=org["cl"].id,
            year=2026,
            kind="credit",
            days=Decimal(1),
            reason="Approved by the director",
        ),
    )
    assert await _available(session, org, "newbie") == Decimal(1)


async def test_the_history_adds_up_and_names_the_version(session: AsyncSession, org) -> None:
    await _set_up_casual_leave(session, org)
    await leave_service.update_settings(
        session, org["admin"], LeaveSettingsUpdate(probation_months=6)
    )
    history = await ledger.history(
        session,
        org["admin"],
        employee_id=org["people"]["ends_10_nov"].id,
        leave_type_id=org["cl"].id,
        year=2026,
    )
    assert [(e.on, e.kind, e.days, e.note) for e in history.entries] == [
        (
            date(2026, 11, 10),
            "accrual",
            Decimal("2.50"),
            "Casual Leave - Nov-Dec 2026 (1.25 a month)",
        )
    ]
    assert history.available == Decimal("2.50")
