"""Contractor invoices: TDS under 194C / 194J from the rule set, thresholds, 26Q."""

from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contractors import ContractorKind, TdsSection
from app.models.payroll import PayrollRuleSet
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services import contractors
from app.services.contractors import ContractorIn, InvoiceIn
from tests.integration.conftest import requires_db

pytestmark = requires_db

RULES = {
    "no_pan_rate": "20",
    "sections": {
        "194C": {
            "rate_individual": "1",
            "rate_other": "2",
            "single_threshold": "30000",
            "annual_threshold": "100000",
        },
        "194J_professional": {"rate": "10", "annual_threshold": "50000"},
        "194J_technical": {"rate": "2", "annual_threshold": "50000"},
    },
}


async def _rules(session: AsyncSession, tenant: Tenant, hr_user: User) -> None:
    session.add(
        PayrollRuleSet(
            tenant_id=tenant.id,
            rule_type="tds_non_salary",
            jurisdiction="IN",
            effective_from=date(2025, 4, 1),
            rules=RULES,
            source_reference="Test rules",
            approved_at=datetime(2025, 4, 1, tzinfo=UTC),
            approved_by_user_id=hr_user.id,
        )
    )
    await session.commit()


async def _checker(session: AsyncSession, tenant: Tenant) -> User:
    user = User(
        tenant_id=tenant.id,
        email="checker@acme-demo.com",
        password_hash="x",
        full_name="Checker",
        role=UserRole.ADMIN,
        is_active=True,
    )
    session.add(user)
    await session.commit()
    return user


async def test_194c_thresholds_and_catch_up(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    await _rules(session, tenant, hr_user)
    vendor = await contractors.create_contractor(
        session,
        hr_user,
        ContractorIn(
            name="Ravi Electricals",
            kind=ContractorKind.INDIVIDUAL,
            pan="ABCPR1234K",
            default_section=TdsSection.S194C,
        ),
        request=None,
    )
    vendor_id = uuid.UUID(vendor["id"])

    small = await contractors.create_invoice(
        session,
        hr_user,
        vendor_id,
        InvoiceIn(
            invoice_number="R-1",
            invoice_date=date(2026, 5, 2),
            amount=Decimal("25000"),
            gst_amount=Decimal("4500"),
        ),
        request=None,
    )
    assert small["tds_amount"] == "0.00" and "below" in small["tds_note"]
    assert small["net_payable"] == "29500.00"

    big = await contractors.create_invoice(
        session,
        hr_user,
        vendor_id,
        InvoiceIn(invoice_number="R-2", invoice_date=date(2026, 6, 2), amount=Decimal("40000")),
        request=None,
    )
    # Over the single-bill limit: 1% on this bill alone (the year is still under 1 lakh).
    assert big["tds_amount"] == "400.00" and big["tds_base"] == "40000.00"

    under = await contractors.create_invoice(
        session,
        hr_user,
        vendor_id,
        InvoiceIn(invoice_number="R-3", invoice_date=date(2026, 7, 2), amount=Decimal("29000")),
        request=None,
    )
    # 94,000 for the year so far, and this bill is under 30,000: still nothing.
    assert under["tds_amount"] == "0.00"

    crossing = await contractors.create_invoice(
        session,
        hr_user,
        vendor_id,
        InvoiceIn(invoice_number="R-4", invoice_date=date(2026, 8, 2), amount=Decimal("10000")),
        request=None,
    )
    # 1,04,000 crosses 1 lakh: R-1 and R-3 (54,000 untaxed) are caught up with R-4.
    assert crossing["tds_base"] == "64000.00"
    assert crossing["tds_amount"] == "640.00"

    with pytest.raises(HTTPException):
        await contractors.create_invoice(
            session,
            hr_user,
            vendor_id,
            InvoiceIn(invoice_number="R-3", invoice_date=date(2026, 7, 3), amount=Decimal("100")),
            request=None,
        )


async def test_no_pan_rate_maker_checker_and_26q(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    await _rules(session, tenant, hr_user)
    consultant = await contractors.create_contractor(
        session,
        hr_user,
        ContractorIn(
            name="No PAN Consulting",
            kind=ContractorKind.INDIVIDUAL,
            default_section=TdsSection.S194J_PROFESSIONAL,
        ),
        request=None,
    )
    invoice = await contractors.create_invoice(
        session,
        hr_user,
        uuid.UUID(consultant["id"]),
        InvoiceIn(invoice_number="NP-1", invoice_date=date(2026, 8, 10), amount=Decimal("60000")),
        request=None,
    )
    assert invoice["tds_rate"] == "20.000" and invoice["tds_amount"] == "12000.00"
    assert "No PAN" in invoice["tds_note"]

    with pytest.raises(HTTPException) as own:
        await contractors.approve_invoice(session, hr_user, uuid.UUID(invoice["id"]), request=None)
    assert own.value.status_code == 403
    checker = await _checker(session, tenant)
    await contractors.approve_invoice(session, checker, uuid.UUID(invoice["id"]), request=None)

    content = await contractors.form_26q_csv(session, hr_user, financial_year="2026-27", quarter=2)
    rows = list(csv.DictReader(io.StringIO(content)))
    assert len(rows) == 1
    assert rows[0]["Deductee PAN"] == "PANNOTAVBL" and rows[0]["Section code"] == "94J"
    assert rows[0]["TDS deducted"] == "12000.00"


async def test_no_rule_set_refuses_rather_than_guessing(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    vendor = await contractors.create_contractor(
        session,
        hr_user,
        ContractorIn(
            name="Acme Pvt Ltd",
            kind=ContractorKind.COMPANY,
            pan="AABCA1234B",
            default_section=TdsSection.S194C,
        ),
        request=None,
    )
    with pytest.raises(HTTPException) as refused:
        await contractors.create_invoice(
            session,
            hr_user,
            uuid.UUID(vendor["id"]),
            InvoiceIn(invoice_number="A-1", invoice_date=date(2026, 5, 1), amount=Decimal("50000")),
            request=None,
        )
    assert refused.value.status_code == 409 and "tds_non_salary" in refused.value.detail


async def test_26q_tds_is_tied_to_challans_and_stated_per_contractor(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity
) -> None:
    from app.models.filings import TdsChallan

    await _rules(session, tenant, hr_user)
    checker = await _checker(session, tenant)
    firm = await contractors.create_contractor(
        session,
        hr_user,
        ContractorIn(
            name="Design Studio",
            kind=ContractorKind.INDIVIDUAL,
            pan="ABCPD1234K",
            default_section=TdsSection.S194J_PROFESSIONAL,
        ),
        request=None,
    )
    invoice = await contractors.create_invoice(
        session,
        hr_user,
        uuid.UUID(firm["id"]),
        InvoiceIn(invoice_number="DS-1", invoice_date=date(2026, 8, 10), amount=Decimal("60000")),
        request=None,
    )
    invoice_id = uuid.UUID(invoice["id"])
    await contractors.approve_invoice(session, checker, invoice_id, request=None)
    assert invoice["tds_amount"] == "6000.00"

    def challan(form: str, quarter: int, amount: str, serial: str) -> TdsChallan:
        return TdsChallan(
            tenant_id=tenant.id,
            legal_entity_id=legal_entity.id,
            bsr_code="0510308",
            deposit_date=date(2026, 9, 7),
            challan_serial=serial,
            financial_year="2026-2027",
            quarter=quarter,
            tds_amount=Decimal(amount),
            total_amount=Decimal(amount),
            form=form,
        )

    salary = challan("24Q", 2, "6000", "00001")
    wrong_quarter = challan("26Q", 1, "6000", "00002")
    small = challan("26Q", 2, "5000", "00003")
    right = challan("26Q", 2, "6000", "00004")
    session.add_all([salary, wrong_quarter, small, right])
    await session.commit()

    position = await contractors.challan_position(
        session, hr_user, financial_year="2026-27", quarter=2
    )
    assert position["untied_invoices"] == 1 and position["ready"] is False

    for bad, why in ((salary, "24Q"), (wrong_quarter, "Q2"), (small, "does not fit")):
        with pytest.raises(HTTPException) as refused:
            await contractors.link_challan(session, hr_user, invoice_id, bad.id)
        assert why in refused.value.detail

    linked = await contractors.link_challan(session, hr_user, invoice_id, right.id)
    assert linked["challan_id"] == str(right.id)
    position = await contractors.challan_position(
        session, hr_user, financial_year="2026-27", quarter=2
    )
    assert position["ready"] is True
    assert {c["challan_serial"]: c["unused"] for c in position["challans"]}["00004"] == "0.00"

    content = await contractors.form_26q_csv(session, hr_user, financial_year="2026-27", quarter=2)
    [row] = list(csv.DictReader(io.StringIO(content)))
    assert row["Challan BSR code"] == "0510308" and row["Challan serial"] == "00004"

    data, filename = await contractors.tds_statement_pdf(
        session, hr_user, uuid.UUID(firm["id"]), financial_year="2026-27", quarter=2
    )
    assert data.startswith(b"%PDF") and filename.endswith("Q2.pdf")
