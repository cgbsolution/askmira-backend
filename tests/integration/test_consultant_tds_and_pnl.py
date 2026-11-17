"""Consultant TDS in 26Q, challans and statements; consultant cost in Account P&L."""

from __future__ import annotations

import csv
import io
import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.consultant_payouts import ConsultantPayoutLine, ConsultantTdsOpening
from app.models.contractors import ContractorKind, TdsSection
from app.models.filings import TdsChallan
from app.models.payroll import PayrollRun, PayrollRunStatus, PayrollRunType, Payslip
from app.schemas.accounts import AllocationCreate, ClientAccountCreate
from app.services import contractors
from app.services.accounts import service as accounts
from app.services.consultants import payouts
from tests.integration.conftest import as_caller, requires_db
from tests.integration.test_consultant_payout_run import (
    CUT_OFF,
    PAY_DATE,
    _confirmed,
    _consultant,
    _hours,
    _second_hr,
    _setup,
)

pytestmark = requires_db

_serials = {"n": 0}


async def _approved_run(session, tenant, hr_user, entity, *, cut_off=CUT_OFF, pay_date=PAY_DATE):
    """A consultant with 40,000 already paid this year and one week: 8,800 TDS on 88,000."""
    await _setup(session, tenant, hr_user)
    person = await _consultant(session, tenant, entity, "CON1")
    session.add(
        ConsultantTdsOpening(
            tenant_id=tenant.id,
            employee_id=person.id,
            fy_start=date(2026, 4, 1),
            section=TdsSection.S194J_PROFESSIONAL,
            fees_paid=Decimal("40000"),
        )
    )
    await session.commit()
    await _hours(session, tenant, person)
    run = await payouts.create_run(
        session, hr_user, legal_entity_id=None, cut_off=cut_off, pay_date=pay_date
    )
    checker = await _second_hr(session, tenant)
    await payouts.approve_run(session, checker, uuid.UUID(run["id"]), confirm=_confirmed)
    return person, uuid.UUID(run["id"])


async def _challan(session, tenant, entity, amount: str, *, quarter: int = 3, form: str = "26Q"):
    _serials["n"] += 1
    row = TdsChallan(
        tenant_id=tenant.id,
        legal_entity_id=entity.id,
        bsr_code="0510308",
        deposit_date=date(2026, 10, 7),
        challan_serial=f"{_serials['n']:05d}",
        financial_year="2026-2027",
        quarter=quarter,
        tds_amount=Decimal(amount),
        total_amount=Decimal(amount),
        form=form,
    )
    session.add(row)
    await session.commit()
    return row


async def _line(session: AsyncSession, person) -> ConsultantPayoutLine:
    from sqlalchemy import select

    return await session.scalar(
        select(ConsultantPayoutLine).where(ConsultantPayoutLine.employee_id == person.id)
    )


async def test_26q_includes_consultant_tds(session, tenant, hr_user, legal_entity) -> None:
    person, _ = await _approved_run(session, tenant, hr_user, legal_entity)

    text = await contractors.form_26q_csv(session, hr_user, financial_year="2026-27", quarter=3)

    rows = list(csv.reader(io.StringIO(text)))
    assert len(rows) == 2
    row = dict(zip(rows[0], rows[1], strict=True))
    assert row["Deductee PAN"] == "ABCDE1234F"
    assert row["Deductee name"] == person.full_name
    assert row["Deductee type"] == "02"
    assert row["Section code"] == "4JB"
    assert row["Date of credit"] == PAY_DATE.isoformat()
    assert row["Amount credited"] == "48000.00"
    assert row["TDS deducted"] == "8800.00"
    assert row["Remark"] == ""
    # Not in the quarter before.
    earlier = await contractors.form_26q_csv(session, hr_user, financial_year="2026-27", quarter=2)
    assert len(list(csv.reader(io.StringIO(earlier)))) == 1


async def test_26q_flags_a_consultant_without_a_pan(session, tenant, hr_user, legal_entity) -> None:
    person, _ = await _approved_run(session, tenant, hr_user, legal_entity)
    person.pan = None
    await session.commit()

    text = await contractors.form_26q_csv(session, hr_user, financial_year="2026-27", quarter=3)

    row = dict(zip(*list(csv.reader(io.StringIO(text))), strict=True))
    assert row["Deductee PAN"] == "PANNOTAVBL" and row["Remark"] == "C"


async def test_26q_skips_drafts_excluded_lines_and_no_tds(
    session, tenant, hr_user, legal_entity
) -> None:
    await _setup(session, tenant, hr_user)
    person = await _consultant(session, tenant, legal_entity, "CON1")
    await _hours(session, tenant, person)
    # Below the 50,000 threshold: no TDS, so nothing to report.
    run = await payouts.create_run(
        session, hr_user, legal_entity_id=None, cut_off=CUT_OFF, pay_date=PAY_DATE
    )
    text = await contractors.form_26q_csv(session, hr_user, financial_year="2026-27", quarter=3)
    assert len(list(csv.reader(io.StringIO(text)))) == 1
    assert run["status"] == "draft"


async def test_26q_reports_tds_deducted_not_tds_due(session, tenant, hr_user, legal_entity) -> None:
    person, _ = await _approved_run(session, tenant, hr_user, legal_entity)
    line = await _line(session, person)
    line.tds_due = Decimal("9999")  # owed, but 8,800 is what was taken
    line.tds_carried_out = Decimal("1199")
    await session.commit()

    text = await contractors.form_26q_csv(session, hr_user, financial_year="2026-27", quarter=3)
    position = await contractors.challan_position(
        session, hr_user, financial_year="2026-27", quarter=3
    )

    assert "8800.00" in text and "9999" not in text
    assert position["untied_tds"] == "8800.00"


async def test_challan_position_and_link_for_a_payout_line(
    session, tenant, hr_user, legal_entity
) -> None:
    person, _ = await _approved_run(session, tenant, hr_user, legal_entity)
    line = await _line(session, person)
    challan = await _challan(session, tenant, legal_entity, "8800")

    before = await contractors.challan_position(
        session, hr_user, financial_year="2026-27", quarter=3
    )
    assert before["untied_invoices"] == 1 and before["ready"] is False

    linked = await contractors.link_challan(
        session, hr_user, challan_id=challan.id, consultant_payout_line_id=line.id
    )
    assert linked["challan_id"] == str(challan.id)

    after = await contractors.challan_position(
        session, hr_user, financial_year="2026-27", quarter=3
    )
    assert after["ready"] is True
    assert after["challans"][0]["tied"] == "8800.00"
    assert after["challans"][0]["unused"] == "0.00"
    csv_text = await contractors.form_26q_csv(session, hr_user, financial_year="2026-27", quarter=3)
    assert "0510308" in csv_text

    await contractors.link_challan(
        session, hr_user, challan_id=None, consultant_payout_line_id=line.id
    )
    assert (await _line(session, person)).challan_id is None


async def test_link_challan_validations_for_a_payout_line(
    session, tenant, hr_user, legal_entity
) -> None:
    person, _ = await _approved_run(session, tenant, hr_user, legal_entity)
    line = await _line(session, person)

    async def refused(challan_id, **kwargs) -> HTTPException:
        with pytest.raises(HTTPException) as caught:
            await contractors.link_challan(session, hr_user, challan_id=challan_id, **kwargs)
        return caught.value

    salary = await _challan(session, tenant, legal_entity, "8800", form="24Q")
    assert (await refused(salary.id, consultant_payout_line_id=line.id)).status_code == 422
    wrong_quarter = await _challan(session, tenant, legal_entity, "8800", quarter=2)
    assert (await refused(wrong_quarter.id, consultant_payout_line_id=line.id)).status_code == 422
    small = await _challan(session, tenant, legal_entity, "8799")
    too_small = await refused(small.id, consultant_payout_line_id=line.id)
    assert too_small.status_code == 422 and "does not fit" in too_small.detail
    # Exactly one of invoice or line.
    assert (await refused(small.id)).status_code == 422
    with pytest.raises(HTTPException) as both_given:
        await contractors.link_challan(
            session,
            hr_user,
            uuid.uuid4(),
            challan_id=small.id,
            consultant_payout_line_id=line.id,
        )
    assert both_given.value.status_code == 422
    missing = await refused(small.id, consultant_payout_line_id=uuid.uuid4())
    assert missing.status_code == 404


async def test_a_challan_counts_invoices_and_payout_lines_together(
    session, tenant, hr_user, legal_entity
) -> None:
    from app.services.contractors import ContractorIn, InvoiceIn

    person, _ = await _approved_run(session, tenant, hr_user, legal_entity)
    line = await _line(session, person)
    vendor = await contractors.create_contractor(
        session,
        hr_user,
        ContractorIn(
            name="Ravi Electricals",
            kind=ContractorKind.INDIVIDUAL,
            pan="ABCPR1234K",
            default_section=TdsSection.S194J_PROFESSIONAL,
        ),
        request=None,
    )
    invoice = await contractors.create_invoice(
        session,
        hr_user,
        uuid.UUID(vendor["id"]),
        InvoiceIn(
            invoice_number="INV-1",
            invoice_date=date(2026, 10, 10),
            amount=Decimal("60000"),
            section=TdsSection.S194J_PROFESSIONAL,
        ),
        request=None,
    )
    await contractors.approve_invoice(
        session, await _other_hr(session, tenant), uuid.UUID(invoice["id"]), request=None
    )
    challan = await _challan(session, tenant, legal_entity, "10000")
    await contractors.link_challan(
        session, hr_user, challan_id=challan.id, consultant_payout_line_id=line.id
    )

    # 8,800 already tied from the payout; the invoice's 6,000 TDS no longer fits in 10,000.
    with pytest.raises(HTTPException) as over:
        await contractors.link_challan(
            session, hr_user, uuid.UUID(invoice["id"]), challan_id=challan.id
        )
    assert over.value.status_code == 422 and "does not fit" in over.value.detail


async def test_scoped_admin_sees_whole_deductor_for_26q_and_challans(
    session, tenant, hr_user, legal_entity
) -> None:
    from sqlalchemy import select

    from app.core import hr_scope

    await _setup(session, tenant, hr_user)
    inside = await _consultant(session, tenant, legal_entity, "CON1")
    outside = await _consultant(session, tenant, legal_entity, "CON2")
    for person in (inside, outside):
        session.add(
            ConsultantTdsOpening(
                tenant_id=tenant.id,
                employee_id=person.id,
                fy_start=date(2026, 4, 1),
                section=TdsSection.S194J_PROFESSIONAL,
                fees_paid=Decimal("40000"),
            )
        )
        await session.commit()
        await _hours(session, tenant, person)
    run = await payouts.create_run(
        session, hr_user, legal_entity_id=None, cut_off=CUT_OFF, pay_date=PAY_DATE
    )
    checker = await _second_hr(session, tenant)
    await payouts.approve_run(session, checker, uuid.UUID(run["id"]), confirm=_confirmed)
    lines = {
        line.employee_id: line
        for line in (await session.scalars(select(ConsultantPayoutLine))).all()
    }
    # Room for one person's 8,800, not both. The out-of-scope consultant's is already tied.
    challan = await _challan(session, tenant, legal_entity, "10000")
    await contractors.link_challan(
        session,
        hr_user,
        challan_id=challan.id,
        consultant_payout_line_id=lines[outside.id].id,
    )

    hr_scope.set_scope(hr_scope.HrScope(employee_ids=(inside.id,)))
    try:
        with pytest.raises(HTTPException) as over:
            await contractors.link_challan(
                session,
                hr_user,
                challan_id=challan.id,
                consultant_payout_line_id=lines[inside.id].id,
            )
        assert over.value.status_code == 422 and "does not fit" in over.value.detail
        text = await contractors.form_26q_csv(session, hr_user, financial_year="2026-27", quarter=3)
        position = await contractors.challan_position(
            session, hr_user, financial_year="2026-27", quarter=3
        )
    finally:
        hr_scope.set_scope(None)
    names = {row["Deductee name"] for row in csv.DictReader(io.StringIO(text))}
    assert names == {inside.full_name, outside.full_name}
    assert position["challans"][0]["tied"] == "8800.00"
    assert position["untied_invoices"] == 1


async def _other_hr(session: AsyncSession, tenant):
    from app.models.user import User, UserRole

    user = User(
        tenant_id=tenant.id,
        email="other@acme.test",
        password_hash="x",
        full_name="Other HR",
        role=UserRole.ADMIN,
        is_active=True,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def test_consultant_tds_statement(session, tenant, hr_user, legal_entity) -> None:
    person, _ = await _approved_run(session, tenant, hr_user, legal_entity)

    data, filename = await contractors.consultant_tds_statement_pdf(
        session, hr_user, person.id, financial_year="2026-27", quarter=3
    )

    assert data.startswith(b"%PDF") and filename.endswith("2026-27-Q3.pdf")
    with pytest.raises(HTTPException) as none:
        await contractors.consultant_tds_statement_pdf(
            session, hr_user, person.id, financial_year="2026-27", quarter=2
        )
    assert none.value.status_code == 404


# ── Account P&L ─────────────────────────────────────────────────────────────


async def test_pnl_cost_in_work_month(session, tenant, hr_user, legal_entity) -> None:
    # 28 Sep (Mon) to 2 Oct (Fri), 8 hours a day at 1,200: three days in September,
    # two in October; paid on 15 October.
    await _setup(session, tenant, hr_user)
    person = await _consultant(session, tenant, legal_entity, "CON1")
    await _hours(session, tenant, person, monday=date(2026, 9, 28))
    run = await payouts.create_run(
        session,
        hr_user,
        legal_entity_id=None,
        cut_off=date(2026, 10, 4),
        pay_date=date(2026, 10, 15),
    )
    run_id = uuid.UUID(run["id"])

    # A draft is not a cost yet.
    assert (
        await payouts.consultant_cost_by_month(
            session, hr_user, start=date(2026, 9, 1), end=date(2026, 10, 31)
        )
        == {}
    )
    checker = await _second_hr(session, tenant)
    await payouts.approve_run(session, checker, run_id, confirm=_confirmed)

    costs = await payouts.consultant_cost_by_month(
        session, hr_user, start=date(2026, 9, 1), end=date(2026, 10, 31)
    )
    assert costs == {
        (person.id, 2026, 9): Decimal("28800.00"),
        (person.id, 2026, 10): Decimal("19200.00"),
    }
    # Only the months asked for.
    october = await payouts.consultant_cost_by_month(
        session, hr_user, start=date(2026, 10, 1), end=date(2026, 10, 31)
    )
    assert october == {(person.id, 2026, 10): Decimal("19200.00")}

    hr = await as_caller(session, hr_user)
    account = await accounts.create_account(
        session, hr, ClientAccountCreate(code="alpha", name="Alpha Bank"), request=None
    )
    await accounts.create_allocation(
        session,
        hr,
        AllocationCreate(
            employee_id=person.id,
            account_id=account.id,
            percent=Decimal(100),
            effective_from=date(2026, 9, 1),
        ),
        request=None,
    )
    detail = await accounts.pnl_detail(session, hr, account.id, start="2026-09", end="2026-10")
    by_month = {m.month: m.people_cost for m in detail.months}
    assert by_month == {"2026-09": Decimal("28800.00"), "2026-10": Decimal("19200.00")}
    assert detail.summary.people_cost == Decimal("48000.00")
    result = await accounts.pnl(session, hr, start="2026-09", end="2026-10")
    assert result.payroll_cost == Decimal("48000.00")
    assert not any("not approved yet" in n for n in result.notes)

    # Nothing in October from September hours: a September-only view is September's days only.
    september = await accounts.pnl_detail(session, hr, account.id, start="2026-09", end="2026-09")
    assert september.summary.people_cost == Decimal("28800.00")


async def test_pnl_leaves_out_excluded_lines_and_adds_payslip_and_fee(
    session, tenant, hr_user, legal_entity
) -> None:
    person, run_id = await _approved_run(session, tenant, hr_user, legal_entity)
    sep = await payouts.consultant_cost_by_month(
        session, hr_user, start=date(2026, 9, 1), end=date(2026, 9, 30)
    )
    assert sep == {(person.id, 2026, 9): Decimal("48000.00")}

    # The same person and month also costed from a payslip (say, the month they
    # moved to payouts): different money, so both count.
    payroll = PayrollRun(
        tenant_id=tenant.id,
        legal_entity_id=legal_entity.id,
        period_year=2026,
        period_month=9,
        effective_date=date(2026, 9, 30),
        status=PayrollRunStatus.APPROVED,
        run_type=PayrollRunType.REGULAR,
    )
    session.add(payroll)
    await session.flush()
    session.add(
        Payslip(
            tenant_id=tenant.id,
            run_id=payroll.id,
            employee_id=person.id,
            gross_earnings=Decimal(1000),
            total_deductions=Decimal(0),
            net_pay=Decimal(1000),
            employer_cost=Decimal(1000),
        )
    )
    await session.commit()
    costs, _ = await accounts._payroll_costs(session, hr_user, [(2026, 9)])
    assert costs[(person.id, 2026, 9)] == Decimal("49000.00")

    line = await _line(session, person)
    line.excluded = True
    await session.commit()
    assert (
        await payouts.consultant_cost_by_month(
            session, hr_user, start=date(2026, 9, 1), end=date(2026, 9, 30)
        )
        == {}
    )
