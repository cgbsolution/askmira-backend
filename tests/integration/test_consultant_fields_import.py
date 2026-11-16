"""Consultant payout fields come in through the employee Excel import.

A consultant row with a TDS section and a customer contact imports with those
fields, and the blanks the template leaves (payee kind, GST rate) get the
consultant defaults. A consultant who needs a customer contact but has none is
refused row by row, as the API would.
"""

from __future__ import annotations

import io
from datetime import date

from openpyxl import Workbook
from sqlalchemy import select

from app.models import Employee, EmploymentType, LegalEntity
from app.models.contractors import ContractorKind, TdsSection
from app.models.employee import ApproverRoute
from app.models.user import UserRole
from tests.integration import platform_support
from tests.integration.conftest import requires_db
from tests.integration.platform_support import bearer, make_person

pytestmark = requires_db

client = platform_support.client

API = "/api/v1/employees/import"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

HEADERS = [
    "Emp Code",
    "First Name",
    "Email",
    "Joining Date",
    "Employment Type",
    "TDS Section",
    "Paid As",
    "GST Applicable",
    "GST Rate (%)",
    "GSTIN",
    "Contract End Date",
    "Timesheet Approver",
    "Customer Contact Name",
    "Customer Contact Email",
]


def _sheet(rows: list[list]) -> dict:
    book = Workbook()
    sheet = book.active
    sheet.append(HEADERS)
    for row in rows:
        sheet.append(row + [None] * (len(HEADERS) - len(row)))
    out = io.BytesIO()
    book.save(out)
    return {"files": {"file": ("employees.xlsx", out.getvalue(), XLSX)}, "data": {}}


async def _hr(session, tenant):
    hr = await make_person(session, tenant, role=UserRole.ADMIN, name="Priya Hr")
    session.add(LegalEntity(tenant_id=tenant.id, name="Example Solutions Pvt Ltd", code="EXS"))
    await session.commit()
    return hr


async def test_a_consultant_row_imports_with_its_payout_fields(client, session, tenant):
    hr = await _hr(session, tenant)
    upload = _sheet(
        [
            [
                "CON-1",
                "Kavya",
                "kavya@example.com",
                date(2026, 4, 1),
                "Consultant",
                "194J technical",
                None,
                "YES",
                None,
                "29ABCDE1234F1Z5",
                date(2027, 3, 31),
                "Customer",
                "Rohan Client",
                "rohan@client.example.com",
            ],
            ["EMP-9", "Asha", "asha@example.com", date(2026, 4, 1), "Permanent"],
        ]
    )

    imported = await client.post(API, headers=bearer(hr), **upload)
    assert imported.status_code == 201, imported.text
    assert imported.json()["created"] == 2

    kavya = await session.scalar(select(Employee).where(Employee.employee_code == "CON-1"))
    assert kavya.employment_type is EmploymentType.CONSULTANT
    assert kavya.tds_section is TdsSection.S194J_TECHNICAL
    assert kavya.payee_kind is ContractorKind.INDIVIDUAL  # blank: the default
    assert kavya.gst_applicable is True
    assert str(kavya.gst_rate) == "18.00"  # blank: the default rate
    assert kavya.gstin == "29ABCDE1234F1Z5"
    assert kavya.contract_end_date == date(2027, 3, 31)
    assert kavya.timesheet_approver_route is ApproverRoute.CUSTOMER
    assert kavya.customer_contact_name == "Rohan Client"
    assert kavya.customer_contact_email == "rohan@client.example.com"

    asha = await session.scalar(select(Employee).where(Employee.employee_code == "EMP-9"))
    assert asha.tds_section is None
    assert asha.timesheet_approver_route is None


async def test_a_consultant_row_without_its_customer_contact_is_refused(client, session, tenant):
    hr = await _hr(session, tenant)
    upload = _sheet(
        [
            [
                "CON-2",
                "Dev",
                "dev@example.com",
                date(2026, 4, 1),
                "Consultant",
                "194C",
                None,
                "NO",
                None,
                None,
                None,
                "Customer",
            ],
        ]
    )

    checked = await client.post(f"{API}/preview", headers=bearer(hr), **upload)
    assert checked.status_code == 200, checked.text
    row = checked.json()["rows"][0]
    assert any("customer contact" in e.lower() for e in row["errors"])
