"""PF and ESI challan files from a locked run, with HR's filters.

What only a database settles: the lock gate, that the figures are the Filings
page's own, who is left out and why, the filters, zero-wage members, and that
the download is audited and tenant-isolated.
"""

from __future__ import annotations

import calendar
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
import xlrd
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditAction, AuditLog
from app.models.employee import Employee, EmploymentStatus
from app.models.org import Department, LegalEntity, Location
from app.models.payroll import PayrollRuleSet, PayrollRun, PayrollRunStatus, Payslip
from app.models.tenant import Tenant
from app.schemas.challan_files import ChallanFilters
from app.services.filings import service as filing_service
from app.services.payroll import challan_files
from tests.integration.conftest import requires_db

pytestmark = requires_db

PF_RULES = {
    "wage_ceiling": 15_000,
    "eps_wage_ceiling": 15_000,
    "edli_wage_ceiling": 15_000,
    "employee_rate": 12,
    "employer_rate": 12,
    "eps_rate": "8.33",
}
ALL = ChallanFilters()


async def _pf_rules(session: AsyncSession, tenant: Tenant) -> None:
    session.add(
        PayrollRuleSet(
            tenant_id=tenant.id,
            rule_type="pf",
            jurisdiction="IN",
            effective_from=date(2026, 4, 1),
            rules=PF_RULES,
            approved_at=datetime.now(UTC),
        )
    )
    await session.commit()


async def _run(
    session: AsyncSession,
    tenant: Tenant,
    entity: LegalEntity,
    status: PayrollRunStatus = PayrollRunStatus.APPROVED,
) -> PayrollRun:
    run = PayrollRun(
        tenant_id=tenant.id,
        legal_entity_id=entity.id,
        period_year=2026,
        period_month=10,
        effective_date=date(2026, 10, calendar.monthrange(2026, 10)[1]),
        status=status,
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run


async def _person(
    session: AsyncSession,
    tenant: Tenant,
    entity: LegalEntity,
    location: Location,
    code: str,
    first: str,
    **fields,
) -> Employee:
    person = Employee(
        tenant_id=tenant.id,
        employee_code=code,
        first_name=first,
        last_name=fields.pop("last_name", None),
        work_email=f"{code.lower()}@acme.test",
        legal_entity_id=entity.id,
        location_id=location.id,
        status=fields.pop("status", EmploymentStatus.ACTIVE),
        date_of_joining=fields.pop("date_of_joining", date(2025, 1, 1)),
        **fields,
    )
    session.add(person)
    await session.commit()
    await session.refresh(person)
    return person


async def _payslip(
    session: AsyncSession, tenant: Tenant, run: PayrollRun, employee: Employee, **overrides
) -> None:
    base = {
        "working_days": Decimal(31),
        "paid_days": Decimal(31),
        "lop_days": Decimal(0),
        "gross_earnings": Decimal("20000.00"),
        "net_pay": Decimal("18000.00"),
        "pf_wage_base": Decimal("12000.00"),
        "pf_employee": Decimal("1440.00"),
        "pf_employer": Decimal("1440.00"),
        "esi_wage_base": Decimal("20000.00"),
        "esi_employee": Decimal("150.00"),
        "esi_employer": Decimal("650.00"),
    }
    base.update(overrides)
    session.add(Payslip(tenant_id=tenant.id, run_id=run.id, employee_id=employee.id, **base))
    await session.commit()


@pytest.fixture
async def month(session, tenant, legal_entity, location):
    """A locked October with four people in two departments."""
    await _pf_rules(session, tenant)
    sales = Department(tenant_id=tenant.id, name="Sales", code="SAL")
    ops = Department(tenant_id=tenant.id, name="Operations", code="OPS")
    session.add_all([sales, ops])
    await session.commit()
    run = await _run(session, tenant, legal_entity)
    roja = await _person(
        session,
        tenant,
        legal_entity,
        location,
        "E001",
        "Roja",
        uan="101838392339",
        esi_number="5042408205",
        department_id=sales.id,
        designation="Executive",
    )
    vidya = await _person(
        session,
        tenant,
        legal_entity,
        location,
        "E002",
        "Vidya",
        last_name="Bhat",
        uan="101838392341",
        esi_number="5350522846",
        department_id=ops.id,
        designation="Manager",
        date_of_joining=date(2026, 10, 6),
    )
    # Owed PF and ESI, but HR never recorded the numbers.
    nouan = await _person(
        session,
        tenant,
        legal_entity,
        location,
        "E003",
        "Mohit",
        department_id=ops.id,
    )
    # Above the ESI ceiling: PF only.
    rahul = await _person(
        session,
        tenant,
        legal_entity,
        location,
        "E004",
        "Rahul",
        uan="101759095312",
        department_id=sales.id,
    )
    await _payslip(session, tenant, run, roja)
    await _payslip(session, tenant, run, vidya, paid_days=Decimal("25.5"), lop_days=Decimal("5.5"))
    await _payslip(session, tenant, run, nouan)
    await _payslip(
        session,
        tenant,
        run,
        rahul,
        gross_earnings=Decimal("90000"),
        pf_wage_base=Decimal("15000"),
        pf_employee=Decimal("1800"),
        pf_employer=Decimal("1800"),
        esi_wage_base=Decimal("90000"),
        esi_employee=Decimal(0),
        esi_employer=Decimal(0),
    )
    return {
        "run": run,
        "sales": sales,
        "ops": ops,
        "roja": roja,
        "vidya": vidya,
        "nouan": nouan,
        "rahul": rahul,
    }


async def test_challan_files_wait_for_the_run_to_be_locked(
    session, tenant, hr_user, legal_entity
) -> None:
    await _pf_rules(session, tenant)
    run = await _run(session, tenant, legal_entity, status=PayrollRunStatus.PENDING_REVIEW)
    for call in (
        challan_files.options(session, hr_user, run.id),
        challan_files.preview(session, hr_user, run.id, "esic_mc", ALL),
        challan_files.download(session, hr_user, run.id, "pf_ecr_csv", ALL),
    ):
        with pytest.raises(HTTPException) as exc:
            await call
        assert exc.value.status_code == 409
        assert "Approve & lock" in exc.value.detail


async def test_the_ecr_upload_matches_the_filings_ecr_and_leaves_out_a_blank_uan(
    session, hr_user, legal_entity, month
) -> None:
    run = month["run"]
    preview = await challan_files.preview(session, hr_user, run.id, "pf_ecr_csv", ALL)
    assert preview.matched == 4 and preview.rows == 3
    assert [(i.employee_code, "UAN" in i.message) for i in preview.left_out] == [("E003", True)]
    assert preview.file_name == "ECR_Upload_ACME_102026.csv"

    name, content, media = await challan_files.download(session, hr_user, run.id, "pf_ecr_csv", ALL)
    assert media.startswith("text/csv")
    rows = content.decode().split("\r\n")
    assert rows[0].startswith("UAN,MEMBER_NAME,GROSS_WAGES")
    assert rows[1] == "101838392339,Roja,20000,12000,12000,12000,1440,1000,440,0,0"
    assert rows[2].split(",")[-2] == "6", "5.5 LOP days round up to 6 NCP days"

    # The text file holds exactly what the Filings page's ECR holds for the same people.
    _, text, _ = await challan_files.download(
        session,
        hr_user,
        run.id,
        "pf_ecr_txt",
        ChallanFilters(exclude_employee_ids=[month["nouan"].id]),
    )
    month["nouan"].uan = "102000000003"
    await session.commit()
    filed = await filing_service.generate_ecr(
        session, hr_user, legal_entity_id=legal_entity.id, year=2026, month=10
    )
    filed_lines = [line for line in filed.content.split("\r\n") if not line.startswith("102000")]
    assert text.decode().split("\r\n") == filed_lines


async def test_the_mc_template_lists_esi_members_only_and_names_who_is_left_out(
    session, hr_user, month
) -> None:
    run = month["run"]
    preview = await challan_files.preview(session, hr_user, run.id, "esic_mc", ALL)
    assert preview.rows == 2, "Roja and Vidya; Rahul is above the ceiling"
    assert preview.not_covered == 1
    assert [i.employee_code for i in preview.left_out] == ["E003"]
    totals = {t.label: t.amount for t in preview.totals}
    assert totals["Total monthly wages"] == Decimal(40000)
    assert totals["Total ESI"] == Decimal(1600)

    name, content, media = await challan_files.download(session, hr_user, run.id, "esic_mc", ALL)
    assert name == "MC_Template_ACME_Oct-2026.xls"
    assert media == "application/vnd.ms-excel"
    sheet = xlrd.open_workbook(file_contents=content).sheet_by_index(0)
    assert sheet.nrows == 3
    assert sheet.row_values(1)[:4] == ["5042408205", "Roja", 31.0, 20000.0]
    assert sheet.row_values(2)[:3] == ["5350522846", "Vidya Bhat", 26.0]


async def test_filters_narrow_the_file(session, hr_user, month) -> None:
    run = month["run"]
    by_department = await challan_files.preview(
        session,
        hr_user,
        run.id,
        "pf_ecr_csv",
        ChallanFilters(department_ids=[month["sales"].id]),
    )
    assert by_department.matched == 2 and by_department.rows == 2 and not by_department.left_out

    joiners = await challan_files.preview(
        session, hr_user, run.id, "esic_mc", ChallanFilters(movement="joined")
    )
    assert joiners.matched == 1 and joiners.rows == 1

    designation = await challan_files.preview(
        session, hr_user, run.id, "pf_ecr_txt", ChallanFilters(designations=["Manager"])
    )
    assert designation.rows == 1

    picked = await challan_files.preview(
        session,
        hr_user,
        run.id,
        "pf_ecr_csv",
        ChallanFilters(employee_ids=[month["roja"].id, month["rahul"].id]),
    )
    assert picked.rows == 2

    nobody = ChallanFilters(department_ids=[month["ops"].id], movement="left")
    with pytest.raises(HTTPException) as exc:
        await challan_files.download(session, hr_user, run.id, "esic_mc", nobody)
    assert exc.value.status_code == 422


async def test_options_count_the_runs_people(session, hr_user, month) -> None:
    options = await challan_files.options(session, hr_user, month["run"].id)
    assert options.people == 4 and options.joined == 1 and options.left == 0
    assert {(c.label, c.count) for c in options.departments} == {("Sales", 2), ("Operations", 2)}
    assert {c.label for c in options.designations} == {"Executive", "Manager"}
    assert options.period_label == "October 2026"


async def test_a_zero_wage_leaver_gets_reason_2_and_a_zero_member_stays_on_the_ecr(
    session, tenant, hr_user, legal_entity, location, month
) -> None:
    gone = await _person(
        session,
        tenant,
        legal_entity,
        location,
        "E005",
        "Gopal",
        uan="100556186810",
        esi_number="5350970922",
        status=EmploymentStatus.EXITED,
        date_of_exit=date(2026, 9, 30),
    )
    await _payslip(
        session,
        tenant,
        month["run"],
        gone,
        paid_days=Decimal(0),
        lop_days=Decimal(31),
        gross_earnings=Decimal(0),
        net_pay=Decimal(0),
        pf_wage_base=Decimal(0),
        pf_employee=Decimal(0),
        pf_employer=Decimal(0),
        esi_wage_base=Decimal(0),
        esi_employee=Decimal(0),
        esi_employer=Decimal(0),
    )
    filters = ChallanFilters(employee_ids=[gone.id])
    _, content, _ = await challan_files.download(
        session, hr_user, month["run"].id, "esic_mc", filters
    )
    row = xlrd.open_workbook(file_contents=content).sheet_by_index(0).row_values(1)
    assert row == ["5350970922", "Gopal", 0.0, 0.0, 2.0, "30/09/2026"]

    _, ecr, _ = await challan_files.download(
        session, hr_user, month["run"].id, "pf_ecr_csv", filters
    )
    assert ecr.decode().split("\r\n")[1] == "100556186810,Gopal,0,0,0,0,0,0,0,31,0"


async def test_the_download_is_audited_with_its_filters(session, hr_user, month) -> None:
    await challan_files.download(
        session,
        hr_user,
        month["run"].id,
        "esic_mc",
        ChallanFilters(department_ids=[month["sales"].id]),
    )
    entry = await session.scalar(
        select(AuditLog).where(
            AuditLog.action == AuditAction.EXPORT, AuditLog.entity_id == month["run"].id
        )
    )
    assert entry is not None
    assert entry.changes["export"] == "esic_mc"
    assert entry.changes["rows"] == 1
    assert entry.changes["filters"] == {"department_ids": [str(month["sales"].id)]}


async def test_another_tenant_cannot_download_the_run(session, other_hr_user, month) -> None:
    with pytest.raises(HTTPException) as exc:
        await challan_files.download(session, other_hr_user, month["run"].id, "esic_mc", ALL)
    assert exc.value.status_code == 404


async def test_esi_wages_that_do_not_explain_the_esi_deducted_are_flagged(
    session, tenant, hr_user, legal_entity, location, month
) -> None:
    """An imported month carrying gross as ESI wages: the portal would charge on gross."""
    session.add(
        PayrollRuleSet(
            tenant_id=tenant.id,
            rule_type="esi",
            jurisdiction="IN",
            effective_from=date(2026, 4, 1),
            rules={"employee_rate": "0.75", "employer_rate": "3.25", "wage_threshold": 21000},
            approved_at=datetime.now(UTC),
        )
    )
    await session.commit()
    imported = await _person(
        session,
        tenant,
        legal_entity,
        location,
        "E006",
        "Roja",
        last_name="Poojari",
        uan="101838392300",
        esi_number="5042408299",
    )
    await _payslip(
        session,
        tenant,
        month["run"],
        imported,
        gross_earnings=Decimal("29403"),
        esi_wage_base=Decimal("29403"),
        esi_employee=Decimal("123"),
        esi_employer=Decimal("531"),
    )
    preview = await challan_files.preview(session, hr_user, month["run"].id, "esic_mc", ALL)
    assert preview.wage_mismatches == 1, "only the imported line; 20,000 x 0.75% = 150 matches"
    note = next(w for w in preview.warnings if w.employee_code == "E006")
    assert "about 16,400" in note.message and "29,403" in note.message
    # Nothing is changed: the file carries the payslip's figure for HR to correct.
    _, content, _ = await challan_files.download(
        session, hr_user, month["run"].id, "esic_mc", ChallanFilters(employee_ids=[imported.id])
    )
    assert xlrd.open_workbook(file_contents=content).sheet_by_index(0).row_values(1)[3] == 29403.0
