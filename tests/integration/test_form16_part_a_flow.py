"""TRACES' Form 16 Part A: uploaded by HR, matched by PAN, joined to Part B."""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi import HTTPException
from pypdf import PdfReader, PdfWriter
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee import Employee, EmploymentStatus
from app.models.org import LegalEntity
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.tax import form16_part_a
from tests.integration.conftest import as_caller, requires_db

pytestmark = requires_db


def _pdf(pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


async def test_a_traces_zip_is_matched_by_pan_and_joined_to_part_b(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    employee.pan = "ABCDE1234F"
    twin_a = Employee(
        tenant_id=tenant.id,
        employee_code="T1",
        first_name="Twin",
        work_email="t1@acme-demo.com",
        legal_entity_id=legal_entity.id,
        status=EmploymentStatus.ACTIVE,
        pan="ZZZZZ9999Z",
    )
    twin_b = Employee(
        tenant_id=tenant.id,
        employee_code="T2",
        first_name="Twin",
        work_email="t2@acme-demo.com",
        legal_entity_id=legal_entity.id,
        status=EmploymentStatus.ACTIVE,
        pan="ZZZZZ9999Z",
    )
    session.add_all([twin_a, twin_b])
    await session.commit()

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("ABCDE1234F_2026-27.pdf", _pdf(2))
        archive.writestr("ZZZZZ9999Z_2026-27.pdf", _pdf(2))
        archive.writestr("QQQQQ0000Q_2026-27.pdf", _pdf(2))
        archive.writestr("readme.txt", "not a pdf")
    result = await form16_part_a.upload_zip(
        session, hr_user, financial_year="2025-26", data=buffer.getvalue()
    )
    assert [s["employee_code"] for s in result["stored"]] == [employee.employee_code]
    reasons = {s["file"]: s["reason"] for s in result["skipped"]}
    assert "More than one" in reasons["ZZZZZ9999Z_2026-27.pdf"]
    assert "No one here" in reasons["QQQQQ0000Q_2026-27.pdf"]

    stored = await form16_part_a.part_a_for(session, hr_user, employee.id, "2025-26")
    merged, included = await form16_part_a.merged_pdf(stored, _pdf(3))
    assert included is True
    assert len(PdfReader(io.BytesIO(merged)).pages) == 5

    alone, included = await form16_part_a.merged_pdf(None, _pdf(3))
    assert included is False and len(PdfReader(io.BytesIO(alone)).pages) == 3


async def test_only_hr_uploads_and_only_pdfs(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    with pytest.raises(HTTPException):
        await form16_part_a.upload_one(
            session,
            hr_user,
            employee_id=employee.id,
            financial_year="2025-26",
            data=b"<html>not a pdf</html>",
            filename="x.pdf",
        )
    login = User(
        tenant_id=tenant.id,
        email="anita.f16@acme-demo.com",
        password_hash="x",
        full_name="Anita",
        role=UserRole.EMPLOYEE,
        is_active=True,
        employee_id=employee.id,
    )
    session.add(login)
    await session.commit()
    login = await as_caller(session, login)
    with pytest.raises(HTTPException) as refused:
        await form16_part_a.upload_one(
            session,
            login,
            employee_id=employee.id,
            financial_year="2025-26",
            data=_pdf(1),
            filename="mine.pdf",
        )
    assert refused.value.status_code == 403
    done = await form16_part_a.upload_one(
        session,
        hr_user,
        employee_id=employee.id,
        financial_year="2025-26",
        data=_pdf(1),
        filename="a.pdf",
    )
    assert done["stored"] is True
