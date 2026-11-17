"""A consultant's week decided by hand, with the customer's approval attached as proof.

Customer approval by email is no longer used; a consultant's week is tracking only
and is decided here by their approver or HR.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee import ApproverRoute, Employee, EmploymentStatus, EmploymentType
from app.models.org import LegalEntity
from app.models.tenant import Tenant
from app.models.timesheets import Timesheet, TimesheetStatus
from app.models.user import User, UserRole
from app.services import timesheets
from app.services.timesheets import EntryIn, RateIn
from tests.integration.conftest import as_caller, requires_db
from tests.integration.platform_support import client  # noqa: F401  (fixture)

pytestmark = requires_db

MONDAY = date(2026, 9, 7)
PDF = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"


async def _login(
    session: AsyncSession, tenant: Tenant, entity: LegalEntity, code: str, **extra
) -> tuple[Employee, User]:
    person = Employee(
        tenant_id=tenant.id,
        employee_code=code,
        first_name=code.title(),
        work_email=f"{code.lower()}@acme-demo.com",
        legal_entity_id=entity.id,
        status=EmploymentStatus.ACTIVE,
        **extra,
    )
    session.add(person)
    await session.flush()
    login = User(
        tenant_id=tenant.id,
        email=f"{code.lower()}.cta@acme-demo.com",
        password_hash="x",
        full_name=code.title(),
        role=UserRole.EMPLOYEE,
        is_active=True,
        employee_id=person.id,
    )
    session.add(login)
    await session.commit()
    return person, await as_caller(session, login)


async def _week_consultant(session, tenant, entity, code, *, approver=None):
    return await _login(
        session,
        tenant,
        entity,
        code,
        employment_type=EmploymentType.CONSULTANT,
        timesheet_approver_route=ApproverRoute.INTERNAL,
        internal_approver_user_id=approver.id if approver else None,
    )


async def _fill(session, login, *days: tuple[int, int]) -> None:
    await timesheets.save_week(
        session,
        login,
        MONDAY,
        [EntryIn(work_date=date(2026, 9, d), hours=Decimal(h)) for d, h in days],
    )


async def _submitted(session, tenant, hr_user, entity, *, approver=None):
    """A consultant with 32 hours at 1200 an hour, submitted."""
    consultant, login = await _week_consultant(session, tenant, entity, "CON1", approver=approver)
    await timesheets.add_rate(
        session,
        hr_user,
        RateIn(employee_id=consultant.id, rate=Decimal("1200"), effective_from=date(2026, 1, 1)),
        request=None,
    )
    await _fill(session, login, (7, 8), (8, 8), (9, 8), (10, 8))
    submitted = await timesheets.submit_week(session, login, MONDAY)
    return consultant, login, uuid.UUID(submitted["id"])


async def _sheet(session, sheet_id) -> Timesheet:
    await session.rollback()
    sheet = await session.scalar(select(Timesheet).where(Timesheet.id == sheet_id))
    assert sheet is not None
    await session.refresh(sheet)
    return sheet


async def test_manual_approval_with_proof(session, tenant, hr_user, legal_entity) -> None:
    _, approver = await _login(session, tenant, legal_entity, "APPR")
    _, _, sheet_id = await _submitted(session, tenant, hr_user, legal_entity, approver=approver)
    approver_id = str(approver.id)
    await session.refresh(approver)
    await timesheets.decide(
        session,
        approver,
        sheet_id,
        approve=True,
        note=None,
        proof=("approval.pdf", PDF),
    )
    sheet = await _sheet(session, sheet_id)
    assert sheet.status is TimesheetStatus.APPROVED
    proof = sheet.approval_proof or {}
    assert proof["name"] == "approval.pdf"
    assert proof["uploaded_by"] == approver_id
    assert proof["key"] and proof["uploaded_at"]


async def test_proof_must_be_a_real_document_on_a_consultant_week(
    session, tenant, hr_user, legal_entity
) -> None:
    _, _, sheet_id = await _submitted(session, tenant, hr_user, legal_entity)
    with pytest.raises(HTTPException) as refused:
        await timesheets.decide(
            session, hr_user, sheet_id, approve=True, note=None, proof=("x.pdf", b"not a pdf")
        )
    assert refused.value.status_code == 422
    assert (await _sheet(session, sheet_id)).status is TimesheetStatus.SUBMITTED

    await session.refresh(tenant)
    await session.refresh(legal_entity)
    await session.refresh(hr_user)
    staff, staff_login = await _login(session, tenant, legal_entity, "STAF")
    await _fill(session, staff_login, (7, 8))
    week = await timesheets.submit_week(session, staff_login, MONDAY)
    with pytest.raises(HTTPException) as other:
        await timesheets.decide(
            session, hr_user, uuid.UUID(week["id"]), approve=True, note=None, proof=("a.pdf", PDF)
        )
    assert other.value.status_code == 422


async def test_consultant_cannot_approve_own_week(session, tenant, hr_user, legal_entity) -> None:
    _, login, sheet_id = await _submitted(session, tenant, hr_user, legal_entity)
    with pytest.raises(HTTPException) as refused:
        await timesheets.decide(session, login, sheet_id, approve=True, note=None)
    assert refused.value.status_code == 403
    assert (await _sheet(session, sheet_id)).status is TimesheetStatus.SUBMITTED


async def test_manual_decide_route_takes_a_file(
    client,  # noqa: F811
    session,
    tenant,
    hr_user,
    legal_entity,
) -> None:
    from app.core.deps import get_current_user
    from app.main import app

    _, _, sheet_id = await _submitted(session, tenant, hr_user, legal_entity)
    app.dependency_overrides[get_current_user] = lambda: hr_user
    try:
        bad = await client.post(
            f"/api/v1/timesheets/{sheet_id}/decide/manual",
            data={"approve": "false"},
        )
        assert bad.status_code == 422
        done = await client.post(
            f"/api/v1/timesheets/{sheet_id}/decide/manual",
            data={"approve": "true"},
            files={"file": ("approval.pdf", PDF, "application/pdf")},
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert done.status_code == 200, done.text
    assert (await _sheet(session, sheet_id)).approval_proof["name"] == "approval.pdf"
