"""Clearing a workspace's people before a fresh import.

Everyone but the admin goes, with punches, bank accounts, documents, payroll
runs and payslips — the admin's own punches too. Settings stay; a rule set
approved by a removed admin stays approved with its approver emptied. Another
workspace is untouched, a rolled-back run changes nothing, and a table nobody
classified stops the reset before anything moves.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models import AuditLog, Employee
from app.models.attendance import AttendancePunch, PunchDirection, PunchSource
from app.models.culture_depth import Announcement
from app.models.disbursement import EmployeeBankAccount
from app.models.employee import EmployeeDocument
from app.models.leave import LeaveType
from app.models.payroll import PayrollRuleSet, PayrollRun, Payslip
from app.models.user import User, UserRole
from app.services import workspace_people_reset as reset
from tests.integration.conftest import requires_db
from tests.integration.platform_support import make_person

pytestmark = requires_db


def _punch(tenant, employee_id) -> AttendancePunch:
    return AttendancePunch(
        tenant_id=tenant.id,
        employee_id=employee_id,
        punched_at=datetime(2026, 9, 1, 4, 0, tzinfo=UTC),
        direction=PunchDirection.IN,
        source=PunchSource.WEB,
    )


async def _count(session, model, tenant_id) -> int:
    return await session.scalar(
        select(func.count()).select_from(model).where(model.tenant_id == tenant_id)
    )


async def _workspace(session, tenant, legal_entity):
    admin = await make_person(session, tenant, role=UserRole.ADMIN, name="Deepak Rawat")
    sachin = await make_person(session, tenant, role=UserRole.ADMIN, name="Sachin Rao")
    ravi = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Ravi Kumar")
    photo = f"{tenant.id}/documents/aadhaar.jpg"
    run = PayrollRun(
        tenant_id=tenant.id,
        legal_entity_id=legal_entity.id,
        period_year=2026,
        period_month=9,
        effective_date=date(2026, 9, 30),
    )
    session.add_all(
        [
            _punch(tenant, ravi.employee_id),
            _punch(tenant, admin.employee_id),
            EmployeeBankAccount(
                tenant_id=tenant.id,
                employee_id=ravi.employee_id,
                account_number="123456789012",
                ifsc="HDFC0000001",
                account_holder_name="Ravi Kumar",
            ),
            EmployeeDocument(
                tenant_id=tenant.id,
                employee_id=ravi.employee_id,
                category="identity",
                title="Aadhaar",
                file_url=photo,
            ),
            LeaveType(tenant_id=tenant.id, name="Casual Leave", code="CL"),
            PayrollRuleSet(
                tenant_id=tenant.id,
                rule_type="pf",
                jurisdiction="IN",
                effective_from=date(2014, 9, 1),
                rules={"wage_ceiling": "15000"},
                created_by_user_id=admin.id,
                approved_by_user_id=sachin.id,
                approved_at=datetime(2026, 10, 29, tzinfo=UTC),
            ),
            run,
        ]
    )
    await session.flush()
    session.add(
        Payslip(
            tenant_id=tenant.id,
            run_id=run.id,
            employee_id=ravi.employee_id,
            gross_earnings=Decimal("30000.00"),
        )
    )
    await session.commit()
    return admin, photo


async def test_people_go_settings_and_the_admin_stay(
    session, tenant, other_tenant, legal_entity, location
):
    admin, photo = await _workspace(session, tenant, legal_entity)
    elsewhere = await make_person(session, other_tenant, role=UserRole.EMPLOYEE, name="Sam Okafor")
    session.add(_punch(other_tenant, elsewhere.employee_id))
    await session.commit()
    admin_id, admin_employee_id = admin.id, admin.employee_id
    tenant_id, other_id, admin_email = tenant.id, other_tenant.id, admin.email

    report = await reset.reset_people(session, tenant_id, keep_user_id=admin_id)
    await session.commit()
    session.expire_all()

    assert report.removed["employees"] == 2 and report.removed["users"] == 2
    assert report.removed["attendance_punches"] == 2  # the admin's own punch too
    assert report.removed["payslips"] == 1 and report.removed["payroll_runs"] == 1
    assert report.files == {photo}
    assert report.links_cleared == {"payroll_rule_sets.approved_by_user_id": 1}

    users = (await session.scalars(select(User.id).where(User.tenant_id == tenant_id))).all()
    people = (
        await session.scalars(select(Employee.id).where(Employee.tenant_id == tenant_id))
    ).all()
    assert users == [admin_id] and people == [admin_employee_id]
    for model in (AttendancePunch, EmployeeBankAccount, EmployeeDocument, PayrollRun, Payslip):
        assert await _count(session, model, tenant_id) == 0, model.__tablename__

    assert await _count(session, LeaveType, tenant_id) == 1
    rule = await session.scalar(select(PayrollRuleSet).where(PayrollRuleSet.tenant_id == tenant_id))
    assert rule.is_approved and rule.approved_by_user_id is None
    assert rule.created_by_user_id == admin_id

    assert await _count(session, Employee, other_id) == 1
    assert await _count(session, AttendancePunch, other_id) == 1

    entry = await session.scalar(
        select(AuditLog).where(AuditLog.tenant_id == tenant_id, AuditLog.entity_type == "tenant")
    )
    assert entry.summary.startswith("People data cleared before a fresh import: 2 employee")
    assert entry.changes["kept_login"] == admin_email


async def test_a_rolled_back_run_changes_nothing(session, tenant, legal_entity, location):
    admin, _ = await _workspace(session, tenant, legal_entity)
    tenant_id = tenant.id
    await reset.reset_people(session, tenant_id, keep_user_id=admin.id)
    await session.rollback()

    assert await _count(session, Employee, tenant_id) == 3
    assert await _count(session, Payslip, tenant_id) == 1
    assert await _count(session, AttendancePunch, tenant_id) == 2


async def test_an_unclassified_table_stops_it_before_anything_moves(
    session, tenant, legal_entity, location
):
    admin, _ = await _workspace(session, tenant, legal_entity)
    tenant_id = tenant.id
    session.add(
        Announcement(
            tenant_id=tenant.id,
            title="Diwali",
            body="Office closed",
            publish_at=datetime(2026, 11, 1, tzinfo=UTC),
            content_hash="x",
        )
    )
    await session.commit()

    with pytest.raises(reset.ResetError, match="announcements"):
        await reset.reset_people(session, tenant_id, keep_user_id=admin.id)
    await session.rollback()
    assert await _count(session, Employee, tenant_id) == 3


async def test_only_an_admin_can_be_the_one_kept(session, tenant, legal_entity, location):
    await _workspace(session, tenant, legal_entity)
    ravi = await session.scalar(
        select(User).where(User.tenant_id == tenant.id, User.role == UserRole.EMPLOYEE)
    )
    with pytest.raises(reset.ResetError, match="not an admin"):
        await reset.reset_people(session, tenant.id, keep_user_id=ravi.id)
