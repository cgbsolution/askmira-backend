"""Ask Mira's HR and founder answers, end to end against a real database.

Asked the way a person types them, with no AI configured (the rule-based
planner): who joined and left, a month's payroll, the salary bill, birthdays
and probation ends, what is waiting on my approval, and HR helpdesk tickets.
Each checks the figures against the records, who may ask, and that another
workspace sees none of it.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.access import UserAreaRights
from app.models.attendance import AttendanceRegularisation, AttendanceStatus
from app.models.employee import Employee, EmploymentStatus
from app.models.org import LegalEntity
from app.models.payroll import (
    EmployeeSalary,
    PayrollRun,
    PayrollRunStatus,
    PayrollRunType,
    Payslip,
    SalaryStructure,
)
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.ai import client as ai_client
from app.services.companion import service as companion
from app.services.helpdesk import categories as helpdesk_categories
from app.services.helpdesk import service as helpdesk
from tests.integration.conftest import as_caller, requires_db
from tests.integration.platform_support import make_person

pytestmark = requires_db


@pytest.fixture(autouse=True)
def _no_ai(monkeypatch: pytest.MonkeyPatch) -> None:
    """No AI provider: every turn is planned by rule, here."""
    monkeypatch.setattr(ai_client.settings, "ANTHROPIC_API_KEY", "")


TODAY = companion._today()


def _month_back(day: date) -> date:
    return (day.replace(day=1) - timedelta(days=1)).replace(day=1)


async def _ask(session: AsyncSession, user: User, text: str) -> dict[str, Any]:
    result = await companion.send(session, user, companion.MessageIn(text=text), request=None)
    return result["messages"][1]


def _figures(card: dict[str, Any]) -> dict[str, str]:
    return {f["label"]: f["value"] for f in card["figures"]}


def _names(card: dict[str, Any]) -> set[str]:
    rows = card["table"]["rows"] if card["type"] == "record" else card["rows"]
    return {row[0] for row in rows}


async def _person(
    session: AsyncSession, tenant: Tenant, code: str, name: str, **fields: Any
) -> Employee:
    first, _, last = name.partition(" ")
    person = Employee(
        tenant_id=tenant.id,
        employee_code=code,
        first_name=first,
        last_name=last or None,
        work_email=f"{code.lower()}@acme.test",
        status=fields.pop("status", EmploymentStatus.ACTIVE),
        date_of_joining=fields.pop("date_of_joining", date(2024, 1, 1)),
        **fields,
    )
    session.add(person)
    await session.commit()
    return person


@pytest.fixture
async def employee_user(session: AsyncSession, tenant: Tenant, employee: Employee) -> User:
    user = User(
        tenant_id=tenant.id,
        email="anita@acme.test",
        password_hash="x",
        full_name="Anita Sharma",
        role=UserRole.EMPLOYEE,
        is_active=True,
        employee_id=employee.id,
    )
    session.add(user)
    await session.commit()
    return await as_caller(session, user)


# ── Joiners, exits and notice ────────────────────────────────────────────────


async def test_hr_sees_who_joined_who_left_and_who_is_on_notice(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    employee: Employee,
    employee_user: User,
    other_hr_user: User,
) -> None:
    first_of_month = TODAY.replace(day=1)
    await _person(session, tenant, "ACM100", "Neha Joshi", date_of_joining=first_of_month)
    await _person(
        session,
        tenant,
        "ACM101",
        "Kabir Das",
        status=EmploymentStatus.EXITED,
        date_of_joining=date(2023, 5, 1),
        date_of_exit=first_of_month,
    )
    await _person(
        session,
        tenant,
        "ACM102",
        "Meera Nair",
        status=EmploymentStatus.ON_NOTICE,
        date_of_joining=date(2022, 2, 1),
        date_of_exit=TODAY + timedelta(days=40),
    )
    hr = await as_caller(session, hr_user)

    reply = await _ask(session, hr, "who joined this month?")
    assert reply["tool"] == "people_changes"
    summary, *tables = reply["cards"]
    figures = _figures(summary)
    assert figures["Joined"] == "1" and figures["Left"] == "1"
    assert figures["On notice now"] == "1"
    by_title = {t["title"]: t for t in tables}
    assert _names(by_title["Joiners"]) == {"Neha Joshi"}
    assert _names(by_title["Exits"]) == {"Kabir Das"}
    assert _names(by_title["On notice"]) == {"Meera Nair"}
    # Attrition is the leavers over the average headcount, and says so.
    assert figures["Attrition"].endswith("%")
    assert "average of the headcount" in summary["note"]

    notice = await _ask(session, hr, "who is serving notice")
    assert notice["tool"] == "people_changes"
    assert "Meera Nair" in json.dumps(notice["cards"])

    # An employee is refused before anything is read; another workspace sees none of it.
    refused = await _ask(session, employee_user, "who joined this month")
    assert refused["tool"] is None and refused["cards"] == []
    assert "only answer about your own record" in refused["text"]
    elsewhere = await _ask(
        session, await as_caller(session, other_hr_user), "who joined this month"
    )
    assert "Neha Joshi" not in json.dumps(elsewhere["cards"])
    assert _figures(elsewhere["cards"][0])["Joined"] == "0"


# ── Payroll and the salary bill ──────────────────────────────────────────────


async def _run(
    session: AsyncSession,
    tenant: Tenant,
    entity: LegalEntity,
    month: date,
    slips: list[tuple[Employee, str, str, str, str]],
    *,
    status=PayrollRunStatus.PAID,
) -> PayrollRun:
    """A run for `month`; each slip is (person, gross, net, pf employee, tds)."""
    run = PayrollRun(
        tenant_id=tenant.id,
        legal_entity_id=entity.id,
        period_year=month.year,
        period_month=month.month,
        effective_date=month,
        run_type=PayrollRunType.REGULAR,
        status=status,
    )
    session.add(run)
    await session.flush()
    for person, gross, net, pf, tds in slips:
        session.add(
            Payslip(
                tenant_id=tenant.id,
                run_id=run.id,
                employee_id=person.id,
                working_days=Decimal(30),
                paid_days=Decimal(30),
                gross_earnings=Decimal(gross),
                total_deductions=Decimal(gross) - Decimal(net),
                net_pay=Decimal(net),
                employer_cost=Decimal(gross) + Decimal(pf),
                pf_employee=Decimal(pf),
                pf_employer=Decimal(pf),
                tds=Decimal(tds),
            )
        )
    await session.commit()
    return run


async def test_a_months_payroll_adds_up_its_payslips(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    employee: Employee,
    legal_entity: LegalEntity,
    employee_user: User,
    other_hr_user: User,
) -> None:
    rohan = await _person(session, tenant, "ACM200", "Rohan Verma")
    last_month = _month_back(TODAY)
    await _run(
        session,
        tenant,
        legal_entity,
        last_month,
        [(employee, "50000", "44000", "1800", "2000"), (rohan, "100000", "82000", "1800", "9000")],
    )
    hr = await as_caller(session, hr_user)

    reply = await _ask(session, hr, "payroll cost last month")
    assert reply["tool"] == "payroll_summary"
    card = reply["cards"][0]
    figures = _figures(card)
    assert figures["People paid"] == "2"
    assert figures["Gross pay"] == "₹1,50,000.00"
    assert figures["Net pay"] == "₹1,26,000.00"
    assert figures["Deductions"] == "₹24,000.00"
    assert figures["PF (employee + employer)"] == "₹7,200.00"
    assert figures["TDS"] == "₹11,000.00"
    assert card["table"]["rows"][0][2] == "Paid"

    # Nothing named: the latest month that has a run.
    latest = await _ask(session, hr, "payroll summary")
    assert _figures(latest["cards"][0])["Gross pay"] == "₹1,50,000.00"
    # A month with no run says so instead of showing zeros.
    none = await _ask(session, hr, "payroll for next month")
    assert none["cards"][0]["type"] == "notice"
    assert "No payroll has been run" in none["cards"][0]["text"]

    refused = await _ask(session, employee_user, "payroll cost last month")
    assert refused["tool"] is None and refused["cards"] == []
    elsewhere = await _ask(session, await as_caller(session, other_hr_user), "payroll summary")
    assert "1,50,000" not in json.dumps(elsewhere["cards"])


async def test_a_sub_admin_who_may_not_view_payroll_gets_no_figures(
    session: AsyncSession,
    tenant: Tenant,
    employee: Employee,
    legal_entity: LegalEntity,
) -> None:
    await _run(
        session,
        tenant,
        legal_entity,
        _month_back(TODAY),
        [(employee, "50000", "44000", "1800", "2000")],
    )
    sub = User(
        tenant_id=tenant.id,
        email="ops@acme.test",
        password_hash="x",
        full_name="Ops Person",
        role=UserRole.SUB_ADMIN,
        is_active=True,
    )
    session.add(sub)
    await session.flush()
    session.add(
        UserAreaRights(
            tenant_id=tenant.id,
            user_id=sub.id,
            area="payroll",
            can_view=False,
            can_create=False,
            can_edit=False,
            can_delete=False,
            can_approve=False,
            can_export=False,
        )
    )
    await session.commit()
    sub = await as_caller(session, sub)
    for question in ("payroll cost last month", "total salary bill"):
        reply = await _ask(session, sub, question)
        assert reply["cards"][0]["type"] == "notice", question
        assert "50,000" not in json.dumps(reply["cards"])


async def test_the_salary_bill_adds_up_salaries_in_force(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    employee: Employee,
) -> None:
    rohan = await _person(session, tenant, "ACM300", "Rohan Verma")
    structure = SalaryStructure(
        tenant_id=tenant.id, name="Standard", effective_from=date(2024, 1, 1)
    )
    session.add(structure)
    await session.flush()
    for person, ctc in ((employee, "600000"), (rohan, "1200000")):
        session.add(
            EmployeeSalary(
                tenant_id=tenant.id,
                employee_id=person.id,
                structure_id=structure.id,
                ctc_annual=Decimal(ctc),
                effective_from=date(2024, 4, 1),
            )
        )
    # A revision saved for later is not in force today.
    session.add(
        EmployeeSalary(
            tenant_id=tenant.id,
            employee_id=rohan.id,
            structure_id=structure.id,
            ctc_annual=Decimal("9900000"),
            effective_from=TODAY + timedelta(days=60),
        )
    )
    await session.commit()
    hr = await as_caller(session, hr_user)

    reply = await _ask(session, hr, "what is our total salary bill")
    assert reply["tool"] == "salary_totals"
    figures = _figures(reply["cards"][0])
    assert figures["People with a salary"] == "2"
    assert figures["Annual CTC"] == "₹18,00,000.00"
    assert figures["Monthly CTC"] == "₹1,50,000.00"
    assert figures["Average annual CTC"] == "₹9,00,000.00"
    assert "₹99,00,000" not in json.dumps(reply["cards"], ensure_ascii=False)


# ── Dates coming up ──────────────────────────────────────────────────────────


async def test_birthdays_anniversaries_and_probation_ends(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    employee_user: User,
) -> None:
    await _person(
        session,
        tenant,
        "ACM400",
        "Isha Rao",
        date_of_birth=TODAY.replace(year=1992)
        if not (TODAY.month == 2 and TODAY.day == 29)
        else date(1992, 2, 29),
    )
    await _person(
        session,
        tenant,
        "ACM401",
        "Dev Kapoor",
        date_of_joining=TODAY.replace(year=TODAY.year - 3)
        if not (TODAY.month == 2 and TODAY.day == 29)
        else date(2023, 2, 28),
    )
    await _person(
        session,
        tenant,
        "ACM402",
        "Tara Sen",
        date_of_joining=TODAY - timedelta(days=80),
        probation_end_date=TODAY + timedelta(days=10),
    )
    hr = await as_caller(session, hr_user)

    birthdays = await _ask(session, hr, "whose birthday is today")
    assert birthdays["tool"] == "people_dates"
    assert _names(birthdays["cards"][0]) == {"Isha Rao"}
    # The birthday shows the day, never the year (an age is not HR's to broadcast).
    assert "1992" not in json.dumps(birthdays["cards"])

    anniversaries = await _ask(session, hr, "work anniversaries this week")
    assert "Dev Kapoor" in json.dumps(anniversaries["cards"])
    assert anniversaries["cards"][0]["rows"][0][-1] == 3

    probation = await _ask(session, hr, "probation ending in the next 30 days")
    assert _names(probation["cards"][0]) == {"Tara Sen"}

    everything = await _ask(session, hr, "upcoming birthdays")
    assert everything["tool"] == "people_dates"

    refused = await _ask(session, employee_user, "birthdays this week")
    assert refused["tool"] is None and refused["cards"] == []


# ── What is waiting on me ────────────────────────────────────────────────────


async def test_a_manager_asks_what_is_waiting_for_their_approval(
    session: AsyncSession,
    tenant: Tenant,
) -> None:
    boss = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Kavya Boss")
    boss_emp = await session.get(Employee, boss.employee_id)
    report = await make_person(
        session, tenant, role=UserRole.EMPLOYEE, name="Ravi Report", manager=boss_emp
    )
    stranger = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Sita Other")
    session.add(
        AttendanceRegularisation(
            tenant_id=tenant.id,
            employee_id=report.employee_id,
            attendance_date=TODAY - timedelta(days=2),
            requested_status=AttendanceStatus.PRESENT,
            reason="Forgot to punch out",
        )
    )
    await session.commit()
    boss = await as_caller(session, boss)
    stranger = await as_caller(session, stranger)

    reply = await _ask(session, boss, "what is pending for my approval?")
    assert reply["tool"] == "my_approvals"
    card = reply["cards"][0]
    assert card["title"] == "Waiting on you"
    assert _figures(card) == {"Attendance corrections": "1"}
    assert card["table"]["rows"][0][1] == "Ravi Report"
    assert card["open"] == "/approvals"

    narrowed = await _ask(session, boss, "pending leave requests")
    assert narrowed["tool"] == "my_approvals"
    assert "No leave approvals are waiting on you" in narrowed["cards"][0]["text"]

    # Nobody sees what they may not decide, nor their own request.
    for person in (stranger, await as_caller(session, report)):
        empty = await _ask(session, person, "anything to approve")
        assert empty["cards"][0]["text"] == "Nothing is waiting on you right now."


# ── HR helpdesk ──────────────────────────────────────────────────────────────


async def test_hr_helpdesk_tickets_mine_and_the_queue(
    session: AsyncSession,
    tenant: Tenant,
) -> None:
    admin = await as_caller(
        session, await make_person(session, tenant, role=UserRole.ADMIN, name="Asha Admin")
    )
    anita = await as_caller(
        session, await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Anita Rao")
    )
    bala = await as_caller(
        session, await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Bala Iyer")
    )
    added = await helpdesk_categories.add_standard_categories(session, admin)
    payroll = next(c for c in added.created if c.name == "Payroll & salary query")
    await helpdesk.create_ticket(
        session,
        anita,
        category_id=payroll.id,
        subject="Salary short in September",
        description="My September salary is 4,000 short.",
    )
    await helpdesk.create_ticket(
        session,
        bala,
        category_id=payroll.id,
        subject="Address proof letter",
        description="Need a letter for my bank.",
    )

    mine = await _ask(session, anita, "my hr tickets")
    assert mine["tool"] == "hr_tickets"
    card = mine["cards"][0]
    assert card["title"] == "Your HR tickets"
    assert [row[1] for row in card["table"]["rows"]] == ["Salary short in September"]
    assert "Address proof" not in json.dumps(mine["cards"])

    # An employee asking for the queue is told it is for those who work it.
    queue = await _ask(session, anita, "all hr tickets")
    assert queue["cards"][0]["type"] == "notice"
    assert "Address proof" not in json.dumps(queue["cards"])

    # HR sees the whole queue, unassigned first.
    everyone = await _ask(session, admin, "open hr tickets")
    assert everyone["tool"] == "hr_tickets"
    subjects = {row[1] for row in everyone["cards"][0]["table"]["rows"]}
    assert subjects == {"Salary short in September", "Address proof letter"}
    unassigned = await _ask(session, admin, "unassigned hr tickets")
    assert len(unassigned["cards"][0]["table"]["rows"]) == 2


async def test_a_new_tool_group_switched_off_is_never_used(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    employee: Employee,
    legal_entity: LegalEntity,
) -> None:
    from app.services.companion import config as companion_config

    await _run(
        session,
        tenant,
        legal_entity,
        _month_back(TODAY),
        [(employee, "50000", "44000", "1800", "2000")],
    )
    hr = await as_caller(session, hr_user)
    await companion_config.set_config(
        session,
        hr,
        companion_config.CompanionConfig(tools={"payroll_figures": False, "approvals": False}),
        request=None,
    )
    payroll = await _ask(session, hr, "payroll cost last month")
    assert payroll["tool"] is None and payroll["cards"] == []
    assert "not switched on" in payroll["text"]
    approvals = await _ask(session, hr, "pending approvals")
    assert approvals["tool"] is None and "not switched on" in approvals["text"]
    status = await companion.status_for(session, hr)
    assert not {"payroll_summary", "salary_totals", "my_approvals"} & {
        t["key"] for t in status["tools"]
    }
    assert uuid.UUID(str(hr.tenant_id))
