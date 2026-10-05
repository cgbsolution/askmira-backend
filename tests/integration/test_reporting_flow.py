"""Slice 22 gates that need a real database.

The compiler is proved against generated SQL in `tests/reporting/`. What only a
database settles: that the queries actually execute against the real schema, that
a manager's reporting tree resolves to the right people, that the row cap and the
timeout produce errors rather than hangs, and that restricted-field runs and
scheduled sends leave audit rows.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditAction, AuditLog
from app.models.employee import Employee, EmploymentStatus
from app.models.org import Department, LegalEntity
from app.models.reporting import (
    Cadence,
    ChartType,
    EmptyBehaviour,
    ExportFormat,
    Report,
    ReportDelivery,
    ReportRun,
    ReportSchedule,
    ReportVisibility,
    RunStatus,
)
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.reporting import service as report_service
from app.services.reporting.compiler import (
    Definition,
    Filter,
    Operator,
    Selection,
)
from app.services.reporting.library import STARTERS
from tests.integration.conftest import as_caller, requires_db

pytestmark = requires_db

TODAY = date(2026, 9, 7)


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════


async def _user(
    session: AsyncSession, tenant: Tenant, email: str, role: UserRole,
    employee_id: uuid.UUID | None = None,
) -> User:
    person = User(
        tenant_id=tenant.id, email=email, password_hash="not-a-real-hash",
        full_name=email.split("@")[0], role=role, is_active=True,
        employee_id=employee_id,
    )
    session.add(person)
    await session.commit()
    await session.refresh(person)
    return person


async def _employee(
    session: AsyncSession, tenant: Tenant, legal_entity: LegalEntity,
    code: str, *, manager_id=None, department_id=None, location_id=None,
    grade="L3", status=EmploymentStatus.ACTIVE, exit_date=None,
) -> Employee:
    person = Employee(
        tenant_id=tenant.id, employee_code=code, first_name=code,
        last_name="Person", display_name=f"{code} Person",
        work_email=f"{code.lower()}@acme.test", legal_entity_id=legal_entity.id,
        manager_id=manager_id, department_id=department_id, location_id=location_id,
        grade=grade, status=status, date_of_joining=date(2025, 4, 1),
        date_of_exit=exit_date, pan=f"ABCDE{code[-4:]}F",
    )
    session.add(person)
    await session.commit()
    await session.refresh(person)
    return person


async def _org(session: AsyncSession, tenant: Tenant) -> tuple[Department, Department]:
    engineering = Department(tenant_id=tenant.id, name="Engineering", code="ENG")
    sales = Department(tenant_id=tenant.id, name="Sales", code="SLS")
    session.add_all([engineering, sales])
    await session.commit()
    return engineering, sales


# ═══════════════════════════════════════════════════════════════════════════
# Every starter actually runs against the real schema
# ═══════════════════════════════════════════════════════════════════════════


async def test_every_starter_report_executes_against_the_real_schema(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    """The checklist gate: 20 starter reports ship **and run**.

    Compiling is not running. A join to a column that was renamed compiles fine
    and fails on execution, which is exactly the failure a client would find
    first.
    """
    failures: list[str] = []
    for starter in STARTERS:
        try:
            result = await report_service.run_definition(
                session, hr_user, starter.definition, today=TODAY
            )
            assert isinstance(result["rows"], list)
        except Exception as exc:
            failures.append(f"{starter.key}: {exc}")
        await session.commit()

    assert not failures, "Starters that do not run:\n" + "\n".join(failures)


# ═══════════════════════════════════════════════════════════════════════════
# Row-level scope, resolved against a real org chart
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_managers_reporting_tree_reaches_their_whole_subtree(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """Two levels down, not one. A manager's report that stopped at direct
    reports would silently exclude everybody below them."""
    boss = await _employee(session, tenant, legal_entity, "BOSS")
    lead = await _employee(session, tenant, legal_entity, "LEAD", manager_id=boss.id)
    junior = await _employee(session, tenant, legal_entity, "JUNR", manager_id=lead.id)
    await _employee(session, tenant, legal_entity, "OTHR")

    tree = await report_service.reporting_tree(session, hr_user, boss.id)
    assert set(tree) == {boss.id, lead.id, junior.id}


async def test_a_cycle_in_the_manager_chain_does_not_loop_forever(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """Somebody eventually sets two people as each other's manager. A naive
    recursive CTE loops until the query times out."""
    first = await _employee(session, tenant, legal_entity, "CYC1")
    second = await _employee(session, tenant, legal_entity, "CYC2", manager_id=first.id)
    first.manager_id = second.id
    await session.commit()

    tree = await report_service.reporting_tree(session, hr_user, first.id)
    assert set(tree) == {first.id, second.id}


async def test_a_manager_only_sees_their_own_tree_in_a_real_query(
    session: AsyncSession, tenant: Tenant, legal_entity: LegalEntity
) -> None:
    """The checklist gate, executed rather than inspected."""
    boss = await _employee(session, tenant, legal_entity, "MGR1")
    await _employee(session, tenant, legal_entity, "REP1", manager_id=boss.id)
    await _employee(session, tenant, legal_entity, "NOTM")

    manager_user = await as_caller(
        session,
        await _user(session, tenant, "mgr@acme.test", UserRole.EMPLOYEE, employee_id=boss.id),
    )

    result = await report_service.run_definition(
        session,
        manager_user,
        Definition(
            entity_key="employee",
            selections=(Selection("employee_code"),),
            limit=100,
        ),
        today=TODAY,
    )
    await session.commit()

    codes = {row[0] for row in result["rows"]}
    assert codes == {"MGR1", "REP1"}
    assert "NOTM" not in codes
    assert result["row_scope"] == "reporting_tree"


async def test_an_employee_sees_only_their_own_row(
    session: AsyncSession, tenant: Tenant, legal_entity: LegalEntity
) -> None:
    me = await _employee(session, tenant, legal_entity, "SELF")
    await _employee(session, tenant, legal_entity, "NOTU")
    employee_user = await _user(
        session, tenant, "self@acme.test", UserRole.EMPLOYEE, employee_id=me.id
    )

    result = await report_service.run_definition(
        session,
        employee_user,
        Definition(entity_key="employee", selections=(Selection("employee_code"),)),
        today=TODAY,
    )
    await session.commit()
    assert {row[0] for row in result["rows"]} == {"SELF"}


async def test_hr_sees_the_whole_tenant_and_nothing_beyond_it(
    session: AsyncSession, tenant: Tenant, other_tenant: Tenant, hr_user: User,
    legal_entity: LegalEntity,
) -> None:
    """The tenant predicate, executed."""
    await _employee(session, tenant, legal_entity, "OURS")
    other_entity = LegalEntity(
        tenant_id=other_tenant.id, name="Other Ltd", code="OTHER",
        registered_address="x",
    )
    session.add(other_entity)
    await session.flush()
    session.add(
        Employee(
            tenant_id=other_tenant.id, employee_code="THRS", first_name="Their",
            last_name="Person", display_name="Their Person",
            work_email="theirs@other.test", legal_entity_id=other_entity.id,
        )
    )
    await session.commit()

    result = await report_service.run_definition(
        session,
        hr_user,
        Definition(entity_key="employee", selections=(Selection("employee_code"),)),
        today=TODAY,
    )
    await session.commit()
    codes = {row[0] for row in result["rows"]}
    assert "OURS" in codes
    assert "THRS" not in codes


# ═══════════════════════════════════════════════════════════════════════════
# Grouping and aggregation, executed
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_grouped_headcount_returns_the_right_numbers(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    engineering, sales = await _org(session, tenant)
    for code in ("ENG1", "ENG2", "ENG3"):
        await _employee(
            session, tenant, legal_entity, code, department_id=engineering.id
        )
    await _employee(session, tenant, legal_entity, "SLS1", department_id=sales.id)

    result = await report_service.run_definition(
        session,
        hr_user,
        Definition(
            entity_key="employee",
            selections=(Selection("department"), Selection("headcount", "count")),
            filters=(Filter("status", Operator.EQUALS, "active"),),
            group_by=("department",),
        ),
        today=TODAY,
    )
    await session.commit()

    counts = {row[0]: row[1] for row in result["rows"]}
    assert counts["Engineering"] == 3
    assert counts["Sales"] == 1


async def test_a_grouped_reports_total_counts_groups_not_rows(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """Otherwise the pagination on a grouped report is wrong by the size of the
    largest group."""
    engineering, sales = await _org(session, tenant)
    for code in ("GRP1", "GRP2", "GRP3"):
        await _employee(
            session, tenant, legal_entity, code, department_id=engineering.id
        )
    await _employee(session, tenant, legal_entity, "GRP4", department_id=sales.id)

    result = await report_service.run_definition(
        session,
        hr_user,
        Definition(
            entity_key="employee",
            selections=(Selection("department"), Selection("headcount", "count")),
            group_by=("department",),
        ),
        today=TODAY,
    )
    await session.commit()
    assert result["total_rows"] == len(result["rows"])
    assert result["total_rows"] == 2


async def test_a_left_join_keeps_employees_with_no_department(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """An employee with no department must still appear in a headcount, with a
    blank department, rather than vanishing from the count."""
    engineering, _ = await _org(session, tenant)
    await _employee(session, tenant, legal_entity, "HASD", department_id=engineering.id)
    await _employee(session, tenant, legal_entity, "NODP")

    result = await report_service.run_definition(
        session,
        hr_user,
        Definition(
            entity_key="employee",
            selections=(Selection("department"), Selection("headcount", "count")),
            group_by=("department",),
        ),
        today=TODAY,
    )
    await session.commit()
    total = sum(row[1] for row in result["rows"])
    assert total >= 2
    assert any(row[0] is None for row in result["rows"])


async def test_a_relative_financial_year_filter_executes_and_bounds_correctly(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """The checklist gate, against real rows."""
    await _employee(
        session, tenant, legal_entity, "INFY", status=EmploymentStatus.EXITED,
        exit_date=date(2026, 6, 15),
    )
    await _employee(
        session, tenant, legal_entity, "PRFY", status=EmploymentStatus.EXITED,
        exit_date=date(2026, 3, 15),
    )

    result = await report_service.run_definition(
        session,
        hr_user,
        Definition(
            entity_key="employee",
            selections=(Selection("employee_code"),),
            filters=(
                Filter("date_of_exit", Operator.RELATIVE_RANGE, "current_financial_year"),
            ),
        ),
        today=TODAY,
    )
    await session.commit()

    codes = {row[0] for row in result["rows"]}
    assert "INFY" in codes, "15 June 2026 is inside FY 2026-27"
    assert "PRFY" not in codes, "15 March 2026 is in the previous financial year"


async def test_a_decimal_comes_back_as_a_string_not_a_float(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    """A salary register that renders 1234.4999999 is worse than one that
    fails."""
    result = await report_service.run_definition(
        session,
        hr_user,
        Definition(
            entity_key="attendance_day",
            selections=(Selection("payable_fraction", "sum"),),
        ),
        today=TODAY,
    )
    await session.commit()
    value = result["rows"][0][0]
    assert value is None or isinstance(value, str)


# ═══════════════════════════════════════════════════════════════════════════
# Limits
# ═══════════════════════════════════════════════════════════════════════════


async def test_the_row_cap_produces_a_clear_error_rather_than_a_hang(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity,
    monkeypatch,
) -> None:
    """The checklist gate. Checked before the rows are fetched, so the memory is
    never spent."""
    for code in ("CAP1", "CAP2", "CAP3"):
        await _employee(session, tenant, legal_entity, code)

    monkeypatch.setattr(report_service, "MAX_ROWS", 1)
    with pytest.raises(HTTPException) as exc:
        await report_service.run_definition(
            session,
            hr_user,
            Definition(entity_key="employee", selections=(Selection("employee_code"),)),
            today=TODAY,
        )
    await session.commit()

    assert exc.value.status_code == 413
    assert "Narrow it" in exc.value.detail
    assert "truncated register" in exc.value.detail

    run = await session.scalar(
        select(ReportRun).where(ReportRun.status == RunStatus.TOO_LARGE)
    )
    assert run is not None, "the refusal is recorded, not just raised"


async def test_a_statement_timeout_is_set_for_every_run(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """A report builder lets any user write an expensive query. Without this one
    of them takes the database down at month end."""
    await report_service.run_definition(
        session,
        hr_user,
        Definition(entity_key="employee", selections=(Selection("employee_code"),)),
        today=TODAY,
    )
    value = await session.scalar(select(func.current_setting("statement_timeout")))
    await session.commit()
    assert value not in (None, "0", "")


# ═══════════════════════════════════════════════════════════════════════════
# Auditing — the checklist gate
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_run_returning_restricted_data_is_audited(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """The checklist gate.

    "Who has been pulling salary data, and how often" has to be one query.
    """
    await report_service.run_definition(
        session,
        hr_user,
        Definition(entity_key="employee", selections=(Selection("pan"),)),
        today=TODAY,
    )
    await session.commit()

    entry = await session.scalar(
        select(AuditLog).where(
            AuditLog.tenant_id == tenant.id,
            AuditLog.action == AuditAction.SENSITIVE_READ,
        )
    )
    assert entry is not None
    assert "payroll_only" in entry.summary

    run = await session.scalar(select(ReportRun).where(ReportRun.tenant_id == tenant.id))
    assert run.max_sensitivity == "payroll_only"
    assert run.run_as_role == "admin"


async def test_a_run_returning_only_public_data_is_not_audited(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """Auditing everything is auditing nothing — a log where every row is a
    headcount query is a log nobody reads."""
    await report_service.run_definition(
        session,
        hr_user,
        Definition(entity_key="employee", selections=(Selection("employee_code"),)),
        today=TODAY,
    )
    await session.commit()

    count = await session.scalar(
        select(func.count()).select_from(AuditLog).where(
            AuditLog.tenant_id == tenant.id,
            AuditLog.action.in_([AuditAction.SENSITIVE_READ, AuditAction.EXPORT]),
        )
    )
    assert count == 0


async def test_an_export_of_restricted_fields_is_audited_as_an_export(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """The checklist gate. Distinct from a read: taking a copy and looking at it
    are different acts."""
    result = await report_service.export_report(
        session,
        hr_user,
        definition=Definition(
            entity_key="employee",
            selections=(Selection("employee_code"), Selection("pan")),
        ),
        export_format=ExportFormat.CSV,
        today=TODAY,
    )
    await session.commit()

    assert "employee_code" not in result["content"].splitlines()[0]
    entry = await session.scalar(
        select(AuditLog).where(
            AuditLog.tenant_id == tenant.id, AuditLog.action == AuditAction.EXPORT
        )
    )
    assert entry is not None
    assert "exported as csv" in entry.summary


async def test_every_run_is_recorded_even_when_it_fails(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    with pytest.raises(HTTPException):
        await report_service.run_definition(
            session,
            hr_user,
            Definition(entity_key="employee", selections=(Selection("nonsense"),)),
            today=TODAY,
        )
    await session.commit()

    run = await session.scalar(
        select(ReportRun).where(ReportRun.status == RunStatus.FAILED)
    )
    assert run is not None
    assert "not a field" in run.error


# ═══════════════════════════════════════════════════════════════════════════
# Saved reports and sharing
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_shared_report_runs_as_the_caller_not_its_author(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """A shared report is a shared *question*. Running it as its author would
    turn every shared salary report into a privilege-escalation route."""
    boss = await _employee(session, tenant, legal_entity, "SHRM")
    manager_user = await _user(
        session, tenant, "shared-mgr@acme.test", UserRole.EMPLOYEE, employee_id=boss.id
    )

    report = Report(
        tenant_id=tenant.id,
        name="Everyone with their PAN",
        entity_key="employee",
        definition=report_service.definition_to_json(
            Definition(
                entity_key="employee",
                selections=(Selection("employee_code"), Selection("pan")),
            )
        ),
        chart_type=ChartType.TABLE,
        visibility=ReportVisibility.TENANT,
        owner_user_id=hr_user.id,
    )
    session.add(report)
    await session.commit()

    # The author gets the PAN column.
    as_author = await report_service.run_saved_report(
        session, hr_user, report_id=report.id, today=TODAY
    )
    await session.commit()
    assert any(c["key"] == "pan" for c in as_author["columns"])

    # The manager gets the same report without it.
    as_manager = await report_service.run_saved_report(
        session, manager_user, report_id=report.id, today=TODAY
    )
    await session.commit()
    assert not any(c["key"] == "pan" for c in as_manager["columns"])
    assert "pan" in as_manager["denied_fields"]
    assert as_manager["notes"]


async def test_a_private_report_is_invisible_to_everybody_else(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    other = await _user(session, tenant, "other-hr@acme.test", UserRole.ADMIN)
    report = Report(
        tenant_id=tenant.id, name="Mine", entity_key="employee",
        definition=report_service.definition_to_json(
            Definition(entity_key="employee", selections=(Selection("employee_code"),))
        ),
        visibility=ReportVisibility.PRIVATE, owner_user_id=hr_user.id,
    )
    session.add(report)
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await report_service.run_saved_report(
            session, other, report_id=report.id, today=TODAY
        )
    assert exc.value.status_code == 404


async def test_cloning_a_starter_produces_an_editable_owned_report(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    report = await report_service.clone_starter(
        session, hr_user, starter_key="headcount_by_department"
    )
    await session.commit()

    assert report.created_from_starter == "headcount_by_department"
    assert report.owner_user_id == hr_user.id
    assert report.visibility is ReportVisibility.PRIVATE

    result = await report_service.run_saved_report(
        session, hr_user, report_id=report.id, today=TODAY
    )
    await session.commit()
    assert result["columns"]


async def test_a_manager_cannot_clone_a_starter_they_could_not_run(
    session: AsyncSession, tenant: Tenant, legal_entity: LegalEntity
) -> None:
    """Cloning it would produce a report they own and cannot open."""
    boss = await _employee(session, tenant, legal_entity, "CLNM")
    manager_user = await _user(
        session, tenant, "clone-mgr@acme.test", UserRole.EMPLOYEE, employee_id=boss.id
    )
    with pytest.raises(HTTPException) as exc:
        await report_service.clone_starter(
            session, manager_user, starter_key="salary_register"
        )
    assert exc.value.status_code == 403


# ═══════════════════════════════════════════════════════════════════════════
# Schedules — the exfiltration path
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_scheduled_send_records_every_recipient(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """The checklist gate.

    Somebody scheduling the salary register weekly to a personal address is
    invisible unless the sends are recorded with their recipients.
    """
    recipient = await _user(session, tenant, "recip@acme.test", UserRole.ADMIN)
    report = Report(
        tenant_id=tenant.id, name="PANs", entity_key="employee",
        definition=report_service.definition_to_json(
            Definition(
                entity_key="employee",
                selections=(Selection("employee_code"), Selection("pan")),
            )
        ),
        visibility=ReportVisibility.TENANT, owner_user_id=hr_user.id,
    )
    session.add(report)
    await session.flush()

    schedule = ReportSchedule(
        tenant_id=tenant.id, report_id=report.id, cadence=Cadence.WEEKLY,
        hour_of_day=9, day_of_week=0, timezone_name="Asia/Kolkata",
        recipient_user_ids=[str(recipient.id), str(hr_user.id)],
        export_format=ExportFormat.CSV,
    )
    session.add(schedule)
    await session.commit()

    result = await report_service.run_schedule(
        session, hr_user, schedule_id=schedule.id,
        as_of=datetime(2026, 9, 7, 6, 0, tzinfo=UTC),
    )
    await session.commit()

    assert len(result["delivered"]) == 2
    deliveries = (
        await session.scalars(
            select(ReportDelivery).where(ReportDelivery.schedule_id == schedule.id)
        )
    ).all()
    assert {d.recipient_email for d in deliveries} == {
        "recip@acme.test", "hr@acme.test"
    }
    assert all(d.max_sensitivity == "payroll_only" for d in deliveries)

    entry = await session.scalar(
        select(AuditLog).where(
            AuditLog.tenant_id == tenant.id,
            AuditLog.entity_type == "report_schedule",
        )
    )
    assert entry is not None
    assert "recip@acme.test" in str(entry.changes)


async def test_each_recipients_copy_is_compiled_as_that_recipient(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """A schedule that compiled once as its creator and mailed the same file to
    everybody would send a manager the whole tenant's PANs because an HR admin
    set it up."""
    boss = await _employee(session, tenant, legal_entity, "SCHM")
    # A manager because somebody reports to them; the schedule works that out
    # for each recipient.
    await _employee(session, tenant, legal_entity, "SCHR", manager_id=boss.id)
    manager_user = await _user(
        session, tenant, "sched-mgr@acme.test", UserRole.EMPLOYEE, employee_id=boss.id
    )

    report = Report(
        tenant_id=tenant.id, name="Codes and PANs", entity_key="employee",
        definition=report_service.definition_to_json(
            Definition(
                entity_key="employee",
                selections=(Selection("employee_code"), Selection("pan")),
            )
        ),
        visibility=ReportVisibility.TENANT, owner_user_id=hr_user.id,
    )
    session.add(report)
    await session.flush()
    schedule = ReportSchedule(
        tenant_id=tenant.id, report_id=report.id, cadence=Cadence.DAILY,
        recipient_user_ids=[str(manager_user.id)], export_format=ExportFormat.CSV,
    )
    session.add(schedule)
    await session.commit()

    await report_service.run_schedule(
        session, hr_user, schedule_id=schedule.id,
        as_of=datetime(2026, 9, 7, 6, 0, tzinfo=UTC),
    )
    await session.commit()

    delivery = await session.scalar(
        select(ReportDelivery).where(ReportDelivery.schedule_id == schedule.id)
    )
    assert delivery is not None
    assert delivery.max_sensitivity == "public", (
        "the manager's copy contains no PAN, so its sensitivity is public"
    )

    run = await session.scalar(
        select(ReportRun).where(ReportRun.id == delivery.run_id)
    )
    assert run.run_as_role == "manager"
    assert run.row_scope == "reporting_tree"


async def test_an_empty_scheduled_report_can_be_configured_to_skip(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """"Pending leave requests" arriving empty every Monday trains people to stop
    opening it. "Employees on notice" arriving empty is news somebody wants."""
    report = Report(
        tenant_id=tenant.id, name="Nobody", entity_key="employee",
        definition=report_service.definition_to_json(
            Definition(
                entity_key="employee",
                selections=(Selection("employee_code"),),
                filters=(Filter("employee_code", Operator.EQUALS, "NOSUCHCODE"),),
            )
        ),
        visibility=ReportVisibility.TENANT, owner_user_id=hr_user.id,
    )
    session.add(report)
    await session.flush()
    schedule = ReportSchedule(
        tenant_id=tenant.id, report_id=report.id, cadence=Cadence.DAILY,
        recipient_user_ids=[str(hr_user.id)], on_empty=EmptyBehaviour.SKIP,
    )
    session.add(schedule)
    await session.commit()

    result = await report_service.run_schedule(
        session, hr_user, schedule_id=schedule.id,
        as_of=datetime(2026, 9, 7, 6, 0, tzinfo=UTC),
    )
    await session.commit()

    assert result["delivered"] == []
    assert any("skips empties" in s for s in result["skipped"])


async def test_a_schedule_advances_its_next_run_after_firing(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    report = Report(
        tenant_id=tenant.id, name="Daily", entity_key="employee",
        definition=report_service.definition_to_json(
            Definition(entity_key="employee", selections=(Selection("employee_code"),))
        ),
        visibility=ReportVisibility.TENANT, owner_user_id=hr_user.id,
    )
    session.add(report)
    await session.flush()
    schedule = ReportSchedule(
        tenant_id=tenant.id, report_id=report.id, cadence=Cadence.DAILY,
        recipient_user_ids=[str(hr_user.id)],
    )
    session.add(schedule)
    await session.commit()

    moment = datetime(2026, 9, 7, 6, 0, tzinfo=UTC)
    await report_service.run_schedule(
        session, hr_user, schedule_id=schedule.id, as_of=moment
    )
    await session.commit()
    await session.refresh(schedule)

    assert schedule.last_run_at == moment
    assert schedule.next_run_at > moment


async def test_a_recipient_from_another_tenant_is_refused_not_sent_to(
    session: AsyncSession, tenant: Tenant, other_tenant: Tenant, hr_user: User,
    other_hr_user: User, employee: Employee,
) -> None:
    """Should be impossible. Checked because a scheduled send to another tenant
    is the single worst outcome available here."""
    report = Report(
        tenant_id=tenant.id, name="Cross", entity_key="employee",
        definition=report_service.definition_to_json(
            Definition(entity_key="employee", selections=(Selection("employee_code"),))
        ),
        visibility=ReportVisibility.TENANT, owner_user_id=hr_user.id,
    )
    session.add(report)
    await session.flush()
    schedule = ReportSchedule(
        tenant_id=tenant.id, report_id=report.id, cadence=Cadence.DAILY,
        recipient_user_ids=[str(other_hr_user.id)],
    )
    session.add(schedule)
    await session.commit()

    result = await report_service.run_schedule(
        session, hr_user, schedule_id=schedule.id,
        as_of=datetime(2026, 9, 7, 6, 0, tzinfo=UTC),
    )
    await session.commit()

    assert result["delivered"] == []
    assert any("different tenant" in s for s in result["skipped"])
    count = await session.scalar(select(func.count()).select_from(ReportDelivery))
    assert count == 0


async def test_reports_are_tenant_isolated(
    session: AsyncSession, tenant: Tenant, hr_user: User, other_hr_user: User
) -> None:
    report = Report(
        tenant_id=tenant.id, name="Ours", entity_key="employee",
        definition=report_service.definition_to_json(
            Definition(entity_key="employee", selections=(Selection("employee_code"),))
        ),
        visibility=ReportVisibility.TENANT, owner_user_id=hr_user.id,
    )
    session.add(report)
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await report_service.run_saved_report(
            session, other_hr_user, report_id=report.id, today=TODAY
        )
    assert exc.value.status_code == 404
