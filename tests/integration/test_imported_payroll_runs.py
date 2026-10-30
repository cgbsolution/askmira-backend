"""Months imported from factoHR as paid: the guards and the wording around them.

An imported run carries the figures the previous payroll paid, locked on import
and never recomputed. Every place that would act on a locked run — the run's
own lifecycle, the bank file, the journal, arrears, Form 16 — must leave it as
it is and say why, and every read must say where its figures came from.

Runs here are inserted directly, the way the importer writes them
(`origin="imported"`, status PAID, published payslips with `import_details`),
so these tests depend on nothing but the guards themselves.
"""

from __future__ import annotations

import calendar
import io
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
from fastapi import HTTPException
from openpyxl import load_workbook
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_secret
from app.models.arrears import ArrearLine, RevisionType
from app.models.attendance import AttendancePeriodLock
from app.models.audit import AuditLog
from app.models.disbursement import (
    BankFileFormat,
    BankProfile,
    DisbursementBatch,
    EmployeeBankAccount,
)
from app.models.employee import Employee, EmploymentStatus, EmploymentType
from app.models.org import LegalEntity
from app.models.payroll import (
    RUN_ORIGIN_IMPORTED,
    CalculationType,
    ComponentType,
    PayrollRuleSet,
    PayrollRun,
    PayrollRunStatus,
    PayrollRunType,
    Payslip,
    SalaryComponent,
    SalaryStructure,
    SalaryStructureComponent,
)
from app.models.tenant import Tenant
from app.models.timesheets import Project
from app.models.user import User, UserRole
from app.schemas.payroll import EmployeeSalaryCreate
from app.services.arrears import service as arrears_service
from app.services.disbursement import service as disbursement_service
from app.services.payroll import journal as journal_service
from app.services.payroll import payslip_pdf
from app.services.payroll import payslips as payslip_service
from app.services.payroll import register_xlsx as register_xlsx_service
from app.services.payroll import run as run_service
from app.services.payroll import service as salary_service
from app.services.payroll.run import other_earnings_of
from app.services.tax import form16
from tests.integration.conftest import as_caller, requires_db
from tests.integration.platform_support import bearer, client  # noqa: F401  (fixture)

pytestmark = requires_db

IMPORTED_AT = "2026-10-30T10:00:00+00:00"
TDS_NOTE = "TDS as deducted on factoHR (imported). No income-tax working was imported."


def _register_lines(file: str, row: int, month: str) -> list[dict]:
    basis = f"As paid in factoHR — {month} register, {file}, row {row}"
    lines = [
        ("BASIC", "BASIC SALARY", "earning", "12682.00", "M"),
        ("HRA", "HOUSE RENT ALLOWANCE", "earning", "5073.00", "N"),
        ("SPECIAL", "SPECIAL ALLOWANCE", "earning", "10634.00", "O"),
        ("BONUS", "BONUS", "earning", "1056.00", "P"),
        ("PF_EMPLOYEE", "Provident fund (employee)", "deduction", "1800.00", "S"),
        ("INSURANCE", "Medical insurance", "deduction", "450.00", "U"),
        ("PT", "Professional tax", "deduction", "200.00", "V"),
        (
            "PF_EMPLOYER",
            "Provident fund (employer, incl. admin & EDLI as in factoHR)",
            "employer_contribution",
            "1950.00",
            "AA",
        ),
        ("GRATUITY_PROVISION", "Gratuity", "employer_contribution", "609.75", "AC"),
    ]
    return [
        {
            "code": code,
            "name": name,
            "type": kind,
            "amount": amount,
            "basis": basis,
            "rule_set_id": None,
            "source": {"column": column, "header": name, "raw": amount},
        }
        for code, name, kind, amount, column in lines
    ]


def _register_inputs(file: str, row: int) -> list[dict]:
    """A NET_DIFF and a negative ADJUSTMENT, as a hand-set HOLD row produces."""
    source = {"type": "factohr_register", "file": file, "sheet": "Sheet1", "rows": [row]}
    lines = [
        ("NET_DIFF", "Net salary in the register differs from gross - deductions", "-1000.00"),
        ("ADJUSTMENT", "Adjustment (bank transfer - net salary, as in the register)", "-495.40"),
        ("PAISA_ROUNDING", "Rounding to the paisa", "0.40"),
    ]
    return [
        {
            "provider": "factohr_register",
            "code": code,
            "name": name,
            "kind": "earning",
            "amount": amount,
            "requested": amount,
            "taxable_amount": "0.00",
            "basis": "As in the factoHR register",
            "source": source,
            "skipped": False,
            "counts_as_wages": False,
        }
        for code, name, amount in lines
    ]


async def _imported_run(
    session: AsyncSession,
    tenant: Tenant,
    legal_entity: LegalEntity,
    admin: User,
    people: list[Employee],
    *,
    year: int,
    month: int,
    account: str = "DXC",
    project_ids: dict | None = None,
) -> tuple[PayrollRun, list[Payslip]]:
    """A month as the importer writes it: PAID, published, figures from the register."""
    file = f"FINAL Salary Register {date(year, month, 1):%b-%y}.xlsx"
    label = f"{date(year, month, 1):%b %Y}"
    now = datetime.now(UTC)
    run = PayrollRun(
        tenant_id=tenant.id,
        legal_entity_id=legal_entity.id,
        period_year=year,
        period_month=month,
        effective_date=date(year, month, calendar.monthrange(year, month)[1]),
        run_type=PayrollRunType.REGULAR,
        status=PayrollRunStatus.PAID,
        origin=RUN_ORIGIN_IMPORTED,
        approved_by_user_id=admin.id,
        approved_at=now,
        published_by_user_id=admin.id,
        published_at=now,
        paid_by_user_id=admin.id,
        paid_at=now,
        reason=f"Imported from factoHR: {file}",
        import_details={
            "system": "factoHR",
            "imported_at": IMPORTED_AT,
            "imported_by_user_id": str(admin.id),
            "files": [{"name": file, "sha256": "ab" * 32, "size": 1234, "sheets": []}],
            "register_totals": {},
            "computed_totals": {},
        },
    )
    session.add(run)
    await session.flush()
    slips = []
    for index, person in enumerate(people):
        row = 4 + index
        slip = Payslip(
            tenant_id=tenant.id,
            run_id=run.id,
            employee_id=person.id,
            project_id=(project_ids or {}).get(person.id),
            working_days=Decimal(30),
            paid_days=Decimal(30),
            lop_days=Decimal(0),
            gross_earnings=Decimal("29445.00"),
            total_deductions=Decimal("2950.00"),
            net_pay=Decimal("25000.00"),
            employer_cost=Decimal("32004.75"),
            pf_wage_base=Decimal("12682.00"),
            pf_employee=Decimal("1800.00"),
            pf_employer=Decimal("1950.00"),
            professional_tax=Decimal("200.00"),
            tds=Decimal("500.00"),
            line_items=_register_lines(file, row, label),
            input_lines=_register_inputs(file, row),
            is_published=True,
            published_at=now,
            import_details={
                "system": "factoHR",
                "file": file,
                "sheet": "Sheet1",
                "rows": [row],
                "kind": "main",
                "status": "HOLD",
                "client": account,
                "project": None,
                "remarks": "Net set by hand",
                "hidden": False,
                "zero_row": False,
                "register": {"net_salary": "24000", "bank_transfer": "23504.6"},
                "cells": {"Net Salary (AE)": "24000", "Bank Tfr (AG)": "23504.6"},
            },
        )
        session.add(slip)
        slips.append(slip)
    run.employee_count = len(slips)
    run.total_gross = Decimal("29445.00") * len(slips)
    run.total_deductions = Decimal("2950.00") * len(slips)
    run.total_net = Decimal("25000.00") * len(slips)
    run.total_employer_cost = Decimal("32004.75") * len(slips)
    await session.commit()
    await session.refresh(run)
    for slip in slips:
        await session.refresh(slip)
    return run, slips


async def _computed_run(
    session: AsyncSession,
    tenant: Tenant,
    legal_entity: LegalEntity,
    employee: Employee,
    *,
    year: int,
    month: int,
    run_status: PayrollRunStatus = PayrollRunStatus.PAID,
) -> tuple[PayrollRun, Payslip]:
    run = PayrollRun(
        tenant_id=tenant.id,
        legal_entity_id=legal_entity.id,
        period_year=year,
        period_month=month,
        effective_date=date(year, month, calendar.monthrange(year, month)[1]),
        status=run_status,
    )
    session.add(run)
    await session.flush()
    slip = Payslip(
        tenant_id=tenant.id,
        run_id=run.id,
        employee_id=employee.id,
        working_days=Decimal(30),
        paid_days=Decimal(30),
        gross_earnings=Decimal("100000.00"),
        total_deductions=Decimal("1800.00"),
        net_pay=Decimal("98200.00"),
        employer_cost=Decimal("101800.00"),
        pf_wage_base=Decimal("40000.00"),
        pf_employee=Decimal("1800.00"),
        pf_employer=Decimal("1800.00"),
        line_items=[
            {"code": "BASIC", "name": "Basic", "type": "earning", "amount": "40000.00"},
            {"code": "SPECIAL", "name": "Special", "type": "earning", "amount": "60000.00"},
            {"code": "PF_EMPLOYEE", "name": "PF", "type": "deduction", "amount": "1800.00"},
            {
                "code": "PF_EMPLOYER",
                "name": "PF (employer)",
                "type": "employer_contribution",
                "amount": "1800.00",
            },
        ],
        is_published=True,
        published_at=datetime.now(UTC),
    )
    session.add(slip)
    await session.commit()
    await session.refresh(run)
    await session.refresh(slip)
    return run, slip


async def _employee_login(session: AsyncSession, tenant: Tenant, employee: Employee) -> User:
    login = User(
        tenant_id=tenant.id,
        email="anita.login@acme.test",
        password_hash="x",
        full_name=employee.full_name,
        role=UserRole.EMPLOYEE,
        is_active=True,
        employee_id=employee.id,
    )
    session.add(login)
    await session.commit()
    await session.refresh(login)
    return await as_caller(session, login)


def _fingerprint(slip: Payslip) -> tuple:
    return (
        str(slip.gross_earnings),
        str(slip.total_deductions),
        str(slip.net_pay),
        str(slip.employer_cost),
        str(slip.tds),
        slip.line_items,
        slip.input_lines,
        slip.is_published,
    )


# ═══════════════════════════════════════════════════════════════════════════
# The run's own lifecycle
# ═══════════════════════════════════════════════════════════════════════════


async def test_every_lifecycle_step_refuses_an_imported_run_with_a_reason(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    run, (slip,) = await _imported_run(
        session, tenant, legal_entity, hr_user, [employee], year=2026, month=3
    )
    before = _fingerprint(slip)

    attempts = {
        "recomputed": run_service.compute_run(session, hr_user, run.id),
        "approved again": run_service.approve_run(session, hr_user, run.id),
        "cancelled": run_service.cancel_run(session, hr_user, run.id, reason="Wrong month"),
        "deleted": run_service.delete_run(session, hr_user, run.id),
        "published again": run_service.publish_run(session, hr_user, run.id),
        "marked paid": run_service.mark_paid(session, hr_user, run.id),
        "reviewed": run_service.acknowledge_variance(
            session, hr_user, run.id, employee.id, note="Looked at it"
        ),
    }
    for cannot_be, attempt in attempts.items():
        with pytest.raises(HTTPException) as caught:
            await attempt
        assert caught.value.status_code == 409, cannot_be
        assert caught.value.detail.startswith(
            f"This month was imported from factoHR as paid; it cannot be {cannot_be}."
        ), caught.value.detail

    # The undo is named where it is the way out.
    with pytest.raises(HTTPException) as caught:
        await run_service.delete_run(session, hr_user, run.id)
    assert "undo the import" in caught.value.detail

    # A second regular run for the month is refused with the import named.
    with pytest.raises(HTTPException) as caught:
        await run_service.create_run(
            session, hr_user, legal_entity_id=legal_entity.id, period_year=2026, period_month=3
        )
    assert caught.value.status_code == 409
    assert "imported from factoHR as paid" in caught.value.detail

    # Nothing moved and nothing was written.
    await session.refresh(run)
    await session.refresh(slip)
    assert run.status is PayrollRunStatus.PAID
    assert _fingerprint(slip) == before
    count = await session.scalar(
        select(func.count(AuditLog.id)).where(AuditLog.entity_id == run.id)
    )
    assert count == 0


async def test_computed_runs_keep_their_own_refusals(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    """The imported wording is for imported runs only."""
    run, _ = await _computed_run(session, tenant, legal_entity, employee, year=2026, month=3)
    with pytest.raises(HTTPException) as caught:
        await run_service.delete_run(session, hr_user, run.id)
    assert caught.value.status_code == 409
    assert "factoHR" not in caught.value.detail
    assert "is locked (paid)" in caught.value.detail


# ═══════════════════════════════════════════════════════════════════════════
# Money out: the bank file and the journal
# ═══════════════════════════════════════════════════════════════════════════


async def test_no_bank_file_is_made_for_an_imported_month(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    run, _ = await _imported_run(
        session, tenant, legal_entity, hr_user, [employee], year=2026, month=3
    )
    profile = BankProfile(
        tenant_id=tenant.id,
        legal_entity_id=legal_entity.id,
        bank_name="HDFC Bank",
        account_number=encrypt_secret("00110123456789", tenant.id),
        ifsc="HDFC0000123",
        corporate_id="ACMECORP01",
        file_format=BankFileFormat.HDFC,
        format_config={},
    )
    session.add(profile)
    session.add(
        EmployeeBankAccount(
            tenant_id=tenant.id,
            employee_id=employee.id,
            account_number=encrypt_secret("50100123456789", tenant.id),
            ifsc="HDFC0001234",
            account_holder_name=employee.full_name,
            is_primary=True,
        )
    )
    await session.commit()

    with pytest.raises(HTTPException) as caught:
        await disbursement_service.prepare_batch(
            session,
            hr_user,
            payroll_run_id=run.id,
            bank_profile_id=profile.id,
            value_date=date(2026, 3, 31),
        )
    assert caught.value.status_code == 409
    assert "imported from factoHR as paid" in caught.value.detail
    assert "pay everyone again" in caught.value.detail
    assert await session.scalar(select(func.count(DisbursementBatch.id))) == 0


async def test_a_bank_file_picker_can_leave_imported_months_out(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    imported, _ = await _imported_run(
        session, tenant, legal_entity, hr_user, [employee], year=2026, month=3
    )
    computed, _ = await _computed_run(session, tenant, legal_entity, employee, year=2026, month=4)

    everything, total = await run_service.list_runs(session, hr_user)
    assert {r.id for r in everything} == {imported.id, computed.id} and total == 2

    payable, total = await run_service.list_runs(session, hr_user, origin="computed")
    assert [r.id for r in payable] == [computed.id] and total == 1

    history, total = await run_service.list_runs(session, hr_user, origin="imported")
    assert [r.id for r in history] == [imported.id] and total == 1


async def test_the_journal_of_an_imported_month_is_not_exported_again(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    run, _ = await _imported_run(
        session, tenant, legal_entity, hr_user, [employee], year=2026, month=3
    )
    with pytest.raises(HTTPException) as caught:
        await journal_service.export_journal(session, hr_user, run.id, fmt="csv")
    assert caught.value.status_code == 409
    assert "already posted from factoHR" in caught.value.detail


# ═══════════════════════════════════════════════════════════════════════════
# Arrears
# ═══════════════════════════════════════════════════════════════════════════


async def _structure(session: AsyncSession, tenant: Tenant) -> SalaryStructure:
    basic = SalaryComponent(
        tenant_id=tenant.id,
        code="BASIC",
        name="Basic",
        component_type=ComponentType.EARNING,
        calculation_type=CalculationType.PERCENTAGE_OF,
        base_component_code="CTC",
        value=Decimal(40),
        counts_toward_pf=True,
        counts_toward_esi=True,
        display_order=1,
    )
    special = SalaryComponent(
        tenant_id=tenant.id,
        code="SPECIAL",
        name="Special allowance",
        component_type=ComponentType.EARNING,
        calculation_type=CalculationType.BALANCE,
        counts_toward_esi=True,
        display_order=9,
    )
    session.add_all([basic, special])
    await session.flush()
    structure = SalaryStructure(
        tenant_id=tenant.id,
        name="Standard",
        effective_from=date(2026, 1, 1),
        applicable_grades=[],
        applicable_locations=[],
    )
    for index, item in enumerate((basic, special)):
        structure.components.append(
            SalaryStructureComponent(
                tenant_id=tenant.id, component_id=item.id, component=item, display_order=index
            )
        )
    session.add(structure)
    await session.commit()
    await session.refresh(structure)
    return structure


async def test_arrears_never_recompute_or_diff_an_imported_month(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    session.add(
        PayrollRuleSet(
            tenant_id=tenant.id,
            rule_type="pf",
            jurisdiction="IN",
            effective_from=date(2026, 1, 1),
            rules={
                "wage_ceiling": 15000,
                "employee_rate": 12,
                "employer_rate": 12,
                "apply_wage_ceiling": True,
            },
            approved_at=datetime.now(UTC),
        )
    )
    await session.commit()
    imported, (imported_slip,) = await _imported_run(
        session, tenant, legal_entity, hr_user, [employee], year=2026, month=2
    )
    computed, _ = await _computed_run(session, tenant, legal_entity, employee, year=2026, month=3)
    before = _fingerprint(imported_slip)
    # Attendance is locked from March, AskMira's first month. February was paid on
    # factoHR and has no AskMira attendance at all.
    session.add(
        AttendancePeriodLock(
            tenant_id=tenant.id,
            period_start=date(2026, 3, 1),
            period_end=date(2099, 12, 31),
            locked_at=datetime.now(UTC),
        )
    )
    await session.commit()

    structure = await _structure(session, tenant)
    salary = await salary_service.assign_salary(
        session,
        hr_user,
        employee.id,
        EmployeeSalaryCreate(
            structure_id=structure.id,
            ctc_annual=Decimal("1500000"),
            effective_from=date(2026, 2, 1),
        ),
    )
    await session.commit()

    # The guard no longer asks for February's attendance: nothing there is recomputed.
    problems = await arrears_service.preview_guardrails(
        session,
        hr_user,
        employee_ids=[employee.id],
        effective_from=date(2026, 2, 1),
        legal_entity_id=legal_entity.id,
    )
    assert problems == []

    per_employee, _ = await arrears_service.preview_arrears(
        session,
        hr_user,
        legal_entity_id=legal_entity.id,
        employee_salaries={employee.id: salary},
        effective_from=date(2026, 2, 1),
    )
    periods = [arrear.period for arrear in per_employee[str(employee.id)]]
    assert periods == ["2026-03"], "only AskMira's own month is recomputed"
    assert {a.source_run_id for a in per_employee[str(employee.id)]} == {str(computed.id)}

    skipped = await arrears_service.imported_periods(
        session, hr_user, legal_entity_id=legal_entity.id, from_date=date(2026, 2, 1)
    )
    assert skipped == [
        {
            "period": "2026-02",
            "run_id": str(imported.id),
            "message": "Paid on factoHR, not recomputed.",
        }
    ]

    batch = await arrears_service.revise_salary(
        session,
        hr_user,
        legal_entity_id=legal_entity.id,
        revisions=[{"employee_id": str(employee.id), "new_salary_id": str(salary.id)}],
        effective_from=date(2026, 2, 1),
        revision_type=RevisionType.INCREMENT,
    )
    await session.commit()
    lines = (await session.scalars(select(ArrearLine).where(ArrearLine.batch_id == batch.id))).all()
    assert lines and {line.period for line in lines} == {"2026-03"}
    assert all(line.source_run_id != imported.id for line in lines)
    audit = await session.scalar(
        select(AuditLog).where(
            AuditLog.entity_type == "arrear_batch", AuditLog.entity_id == batch.id
        )
    )
    assert audit is not None and audit.changes["not_recomputed"] == ["2026-02"]

    await session.refresh(imported_slip)
    assert _fingerprint(imported_slip) == before


# ═══════════════════════════════════════════════════════════════════════════
# Reads: origin, where the figures came from, and honest TDS wording
# ═══════════════════════════════════════════════════════════════════════════


async def test_reads_say_the_figures_came_from_factohr(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    run, (slip,) = await _imported_run(
        session, tenant, legal_entity, hr_user, [employee], year=2026, month=3
    )
    computed, computed_slip = await _computed_run(
        session, tenant, legal_entity, employee, year=2026, month=4
    )

    read = await payslip_service.run_read(session, hr_user, run)
    assert read.origin == "imported"
    assert read.import_summary is not None
    assert read.import_summary.system == "factoHR"
    assert read.import_summary.files == ["FINAL Salary Register Mar-26.xlsx"]
    assert read.import_summary.imported_by_name == hr_user.full_name
    assert read.import_summary.imported_at == datetime.fromisoformat(IMPORTED_AT)
    assert read.caller_may_approve is False

    plain = await payslip_service.run_read(session, hr_user, computed)
    assert plain.origin == "computed" and plain.import_summary is None

    hr_view = await payslip_service.hr_payslip(session, hr_user, run.id, slip.id)
    assert hr_view.origin == "imported"
    assert hr_view.tds_note == TDS_NOTE
    assert hr_view.tds_working is None
    summary = hr_view.import_summary
    assert summary is not None
    assert (summary.system, summary.file, summary.sheet, summary.rows) == (
        "factoHR",
        "FINAL Salary Register Mar-26.xlsx",
        "Sheet1",
        [4],
    )
    assert (summary.status, summary.client) == ("HOLD", "DXC")
    assert summary.cells == {"Net Salary (AE)": "24000", "Bank Tfr (AG)": "23504.6"}
    # Signed register lines come through as they are.
    assert [(line.code, line.amount) for line in hr_view.input_lines] == [
        ("NET_DIFF", Decimal("-1000.00")),
        ("ADJUSTMENT", Decimal("-495.40")),
        ("PAISA_ROUNDING", Decimal("0.40")),
    ]
    assert hr_view.other_earnings == Decimal("-1495.00")

    computed_view = await payslip_service.hr_payslip(
        session, hr_user, computed.id, computed_slip.id
    )
    assert computed_view.origin == "computed" and computed_view.import_summary is None
    assert "factoHR" not in computed_view.tds_note

    # The employee sees the same, on their own payslips.
    login = await _employee_login(session, tenant, employee)
    mine = await payslip_service.my_payslips(session, login)
    by_month = {(item.period_year, item.period_month): item for item in mine}
    assert by_month[(2026, 3)].origin == "imported"
    assert by_month[(2026, 3)].import_summary is not None
    assert by_month[(2026, 3)].import_summary.file == "FINAL Salary Register Mar-26.xlsx"
    assert by_month[(2026, 3)].import_summary.cells is None  # list view: no cells
    assert by_month[(2026, 4)].origin == "computed"
    assert by_month[(2026, 4)].import_summary is None

    own = await payslip_service.my_payslip(session, login, slip.id)
    assert own.origin == "imported"
    assert own.tds_note == TDS_NOTE
    assert own.import_summary is not None and own.import_summary.cells is not None


async def test_the_api_exposes_origin_and_filters_by_it(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    legal_entity: LegalEntity,
    employee: Employee,
    client: httpx.AsyncClient,  # noqa: F811
) -> None:
    run, (slip,) = await _imported_run(
        session, tenant, legal_entity, hr_user, [employee], year=2026, month=3
    )
    await _computed_run(session, tenant, legal_entity, employee, year=2026, month=4)

    listed = await client.get("/api/v1/payroll/runs", headers=bearer(hr_user))
    assert listed.status_code == 200, listed.text
    origins = {item["period_month"]: item["origin"] for item in listed.json()["items"]}
    assert origins == {3: "imported", 4: "computed"}
    imported_item = next(i for i in listed.json()["items"] if i["origin"] == "imported")
    assert imported_item["import_summary"]["files"] == ["FINAL Salary Register Mar-26.xlsx"]

    payable = await client.get("/api/v1/payroll/runs?origin=computed", headers=bearer(hr_user))
    assert [item["period_month"] for item in payable.json()["items"]] == [4]
    bad = await client.get("/api/v1/payroll/runs?origin=other", headers=bearer(hr_user))
    assert bad.status_code == 422

    publish = await client.post(f"/api/v1/payroll/runs/{run.id}/publish", headers=bearer(hr_user))
    assert publish.status_code == 409
    assert "imported from factoHR as paid" in publish.json()["detail"]

    login = await _employee_login(session, tenant, employee)
    mine = await client.get("/api/v1/payroll/my-payslips", headers=bearer(login))
    assert mine.status_code == 200, mine.text
    march = next(item for item in mine.json() if item["period_month"] == 3)
    assert march["origin"] == "imported"
    assert march["import_summary"]["system"] == "factoHR"
    one = await client.get(f"/api/v1/payroll/my-payslips/{slip.id}", headers=bearer(login))
    assert one.status_code == 200, one.text
    assert one.json()["tds_note"] == TDS_NOTE
    assert one.json()["import_summary"]["client"] == "DXC"


# ═══════════════════════════════════════════════════════════════════════════
# Project on the register: never today's project for an imported payslip
# ═══════════════════════════════════════════════════════════════════════════


async def test_an_imported_payslip_never_takes_todays_project(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    transition = Project(tenant_id=tenant.id, code="TRN", name="Transition")
    internal = Project(tenant_id=tenant.id, code="INT", name="Internal")
    session.add_all([transition, internal])
    await session.flush()
    employee.project_id = transition.id  # where they are today
    colleague = Employee(
        tenant_id=tenant.id,
        employee_code="ACM002",
        first_name="Ravi",
        last_name="Kumar",
        work_email="ravi@acme.test",
        legal_entity_id=legal_entity.id,
        status=EmploymentStatus.ACTIVE,
        project_id=transition.id,
    )
    session.add(colleague)
    await session.commit()

    run, _ = await _imported_run(
        session,
        tenant,
        legal_entity,
        hr_user,
        [employee, colleague],
        year=2026,
        month=3,
        account="DXC",
        project_ids={colleague.id: internal.id},
    )
    computed, _ = await _computed_run(session, tenant, legal_entity, employee, year=2026, month=4)

    rows = {
        row.employee_code: row
        for row in (await payslip_service.register(session, hr_user, run.id)).rows
    }
    # No project matched on the register row: none shown, and the register's text instead.
    assert rows["ACM001"].project_id is None
    assert rows["ACM001"].project_name is None
    assert rows["ACM001"].register_client == "DXC"
    # A matched project is shown, and needs no register text.
    assert rows["ACM002"].project_id == internal.id
    assert rows["ACM002"].project_name == "Internal"
    assert rows["ACM002"].register_client is None

    # A computed payslip without a project still shows the person's project.
    (computed_row,) = (await payslip_service.register(session, hr_user, computed.id)).rows
    assert computed_row.project_name == "Transition"
    assert computed_row.register_client is None

    _, data = await register_xlsx_service.register_xlsx(session, hr_user, run.id)
    sheet = load_workbook(io.BytesIO(data)).active
    assert str(sheet["A3"].value).startswith("Imported from factoHR as paid there")
    assert "FINAL Salary Register Mar-26.xlsx" in str(sheet["A3"].value)
    headers = [cell.value for cell in sheet[7]]
    project_col = headers.index("Project") + 1
    by_code = {sheet.cell(row=r, column=2).value: r for r in (8, 9)}
    assert sheet.cell(row=by_code["ACM001"], column=project_col).value == "DXC"
    assert sheet.cell(row=by_code["ACM002"], column=project_col).value == "Internal"


# ═══════════════════════════════════════════════════════════════════════════
# The PDF: register lines, including a negative adjustment, add up
# ═══════════════════════════════════════════════════════════════════════════


async def test_the_pdf_renders_with_a_negative_adjustment_and_a_net_difference(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    run, (slip,) = await _imported_run(
        session, tenant, legal_entity, hr_user, [employee], year=2026, month=3
    )
    # The arithmetic the PDF checks, on its own.
    document = payslip_pdf.build_payslip_document(
        company_name="Acme",
        company_address=None,
        period_year=2026,
        period_month=3,
        employee_name=employee.full_name,
        employee_code=employee.employee_code,
        designation=None,
        date_of_joining=None,
        pan=None,
        uan=None,
        payment_date=None,
        working_days=slip.working_days,
        paid_days=slip.paid_days,
        lop_days=slip.lop_days,
        not_employed_days=slip.not_employed_days,
        line_items=list(slip.line_items),
        input_lines=list(slip.input_lines),
        tds=slip.tds,
        gross_earnings=slip.gross_earnings,
        other_earnings=other_earnings_of(slip),
        total_deductions=slip.total_deductions,
        net_pay=slip.net_pay,
    )
    assert document.total_earnings == Decimal("27950.00")
    assert document.total_deductions == Decimal("2950.00")
    assert document.net_pay == Decimal("25000.00")
    assert ("Adjustment (bank transfer - net salary, as in the register)", Decimal("-495.40")) in (
        document.earnings
    )

    data, filename = await payslip_service.hr_payslip_pdf(session, hr_user, run.id, slip.id)
    assert data[:5] == b"%PDF-"
    assert filename == "payslip-ACM001-2026-03.pdf"

    login = await _employee_login(session, tenant, employee)
    own, _ = await payslip_service.my_payslip_pdf(session, login, slip.id)
    assert own[:5] == b"%PDF-"


# ═══════════════════════════════════════════════════════════════════════════
# Form 16
# ═══════════════════════════════════════════════════════════════════════════


async def test_form16_for_a_year_paid_on_factohr_says_it_was_issued_there(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    # An approved TDS rule set in force at the year's end: even so, a year paid
    # wholly on factoHR is not recomputed.
    session.add(
        PayrollRuleSet(
            tenant_id=tenant.id,
            rule_type="tds",
            jurisdiction="IN",
            effective_from=date(2025, 4, 1),
            rules={"financial_year_start_month": 4},
            approved_at=datetime.now(UTC),
        )
    )
    await session.commit()
    for month in (1, 2, 3):
        await _imported_run(
            session, tenant, legal_entity, hr_user, [employee], year=2026, month=month
        )
    await _computed_run(session, tenant, legal_entity, employee, year=2026, month=4)

    years = {
        year["financial_year"]: year
        for year in await form16.list_form16_years(session, hr_user, employee.id)
    }
    factohr = years["2025-26"]
    assert factohr["available"] is False
    assert factohr["status"] == "issued_elsewhere"
    assert factohr["issued_by"] == "factoHR"
    assert factohr["reason"] == "Issued by your previous payroll (factoHR)."
    assert factohr["tds_deducted"] == "1500.00"  # 3 x 500 as deducted on factoHR
    assert factohr["taxable_income"] is None

    # AskMira's own year is unchanged: not ready until it ends.
    own = years["2026-27"]
    assert own["status"] == "not_ready" and own["issued_by"] is None
    assert "Issued by your previous payroll" not in own["reason"]

    with pytest.raises(form16.Form16UnavailableError, match="Issued by your previous payroll"):
        await form16.form16_statement(session, hr_user, employee.id, "2025-26")


async def test_a_year_still_running_or_finished_in_askmira_is_not_issued_elsewhere(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    """factoHR issued Form 16 only for years that ended before AskMira ran payroll.

    A year still running, or one the company finished in AskMira after moving
    mid-year, is AskMira's to issue on the imported months plus its own.
    """
    from app.services.attendance.service import local_today

    today = await local_today(session, hr_user)
    fy_start_year = today.year if today.month >= 4 else today.year - 1
    running = f"{fy_start_year}-{str(fy_start_year + 1)[-2:]}"
    # The running year: only imported months so far.
    await _imported_run(
        session, tenant, legal_entity, hr_user, [employee], year=fy_start_year, month=4
    )
    # The year before: imported for this person, but the company ran its own
    # payroll in it (another person's computed run), so it moved mid-year.
    other = Employee(
        tenant_id=tenant.id,
        employee_code="EMP-MOVED",
        first_name="Moved",
        work_email="moved@example.com",
        status=EmploymentStatus.ACTIVE,
        employment_type=EmploymentType.FULL_TIME,
        date_of_joining=date(2020, 1, 1),
        legal_entity_id=legal_entity.id,
    )
    session.add(other)
    await session.commit()
    previous_start = fy_start_year - 1
    await _imported_run(
        session, tenant, legal_entity, hr_user, [employee], year=previous_start, month=5
    )
    await _computed_run(session, tenant, legal_entity, other, year=previous_start + 1, month=2)

    years = {
        year["financial_year"]: year
        for year in await form16.list_form16_years(session, hr_user, employee.id)
    }
    previous = f"{previous_start}-{str(previous_start + 1)[-2:]}"
    for label in (running, previous):
        assert years[label]["status"] != "issued_elsewhere", label
        assert years[label]["issued_by"] is None, label
        assert "Issued by your previous payroll" not in (years[label]["reason"] or ""), label
