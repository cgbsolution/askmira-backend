"""Four login roles, and managing people as a place in the org chart.

Super admin, admin, sub-admin and employee are the only roles. Whoever has a
direct report acts for their reporting line whatever their role; nobody is
given a "manager" role, so nobody has to remember to take it away.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from app.core import permissions
from app.core.deps import require_roles
from app.models.user import User, UserRole
from app.schemas.auth import UserRead
from app.services.grounding.retrieval import ScopeKind as GroundingScope
from app.services.grounding.retrieval import scope_for
from app.services.reporting.compiler import ScopeKind as ReportScope
from app.services.reporting.compiler import resolve_scope
from app.services.reporting.semantic import Sensitivity, may_see


def _user(role: UserRole, *, reports: bool = False) -> User:
    user = User(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        email=f"{role.value}@acme.test",
        password_hash="!",
        full_name=role.value,
        phone=None,
        role=role,
        is_active=True,
        is_email_verified=True,
        must_change_password=False,
        employee_id=uuid.uuid4(),
        last_login_at=None,
        created_at=datetime.now(UTC),
    )
    user.has_reports = reports
    return user


def test_there_are_four_roles() -> None:
    assert {role.value for role in UserRole} == {"super_admin", "admin", "sub_admin", "employee"}


def test_a_user_nobody_prepared_is_not_a_manager() -> None:
    """Loaded outside a request, a login fails closed on manager-only actions."""
    assert User(role=UserRole.EMPLOYEE).has_reports is False


# ── The route gate ──────────────────────────────────────────────────────────


async def _passes(gate, user: User) -> bool:
    try:
        await gate(user)
    except HTTPException as exc:
        assert exc.status_code == 403
        return False
    return True


async def test_an_admin_gate_admits_admins_and_the_super_admin_only() -> None:
    gate = require_roles(UserRole.ADMIN)
    assert await _passes(gate, _user(UserRole.ADMIN))
    assert await _passes(gate, _user(UserRole.SUPER_ADMIN))
    assert not await _passes(gate, _user(UserRole.SUB_ADMIN))
    # Having reports does not open an admin gate.
    assert not await _passes(gate, _user(UserRole.EMPLOYEE, reports=True))


async def test_a_line_manager_gate_admits_anybody_with_reports() -> None:
    gate = require_roles(UserRole.ADMIN, UserRole.SUB_ADMIN, line_managers=True)
    assert await _passes(gate, _user(UserRole.EMPLOYEE, reports=True))
    assert not await _passes(gate, _user(UserRole.EMPLOYEE))
    assert await _passes(gate, _user(UserRole.SUB_ADMIN))


def test_the_service_gate_matches_the_route_gate() -> None:
    manager = _user(UserRole.EMPLOYEE, reports=True)
    permissions.require_role(manager, permissions.HR_ROLES, detail="x", line_managers=True)
    with pytest.raises(HTTPException):
        permissions.require_role(manager, permissions.HR_ROLES, detail="x")
    with pytest.raises(HTTPException):
        permissions.require_role(
            _user(UserRole.EMPLOYEE), permissions.HR_ROLES, detail="x", line_managers=True
        )


def test_hr_is_admin_and_sub_admin() -> None:
    assert permissions.is_hr(_user(UserRole.ADMIN))
    assert permissions.is_hr(_user(UserRole.SUB_ADMIN))
    assert permissions.is_hr(_user(UserRole.SUPER_ADMIN))
    assert not permissions.is_hr(_user(UserRole.EMPLOYEE, reports=True))
    assert permissions.is_manager_or_hr(_user(UserRole.EMPLOYEE, reports=True))
    assert not permissions.is_manager_or_hr(_user(UserRole.EMPLOYEE))


# ── Reach in reports, BI and grounding ─────────────────────────────────────


def test_an_employee_with_reports_reaches_their_reporting_tree() -> None:
    manager = _user(UserRole.EMPLOYEE, reports=True)
    assert permissions.scope_role(manager) == "manager"
    assert resolve_scope(permissions.scope_role(manager)) is ReportScope.REPORTING_TREE


def test_an_employee_without_reports_reaches_only_themselves() -> None:
    assert resolve_scope(permissions.scope_role(_user(UserRole.EMPLOYEE))) is ReportScope.SELF


def test_hr_with_reports_keeps_the_wider_hr_reach() -> None:
    sub_admin = _user(UserRole.SUB_ADMIN, reports=True)
    assert permissions.scope_role(sub_admin) == "sub_admin"
    assert resolve_scope("sub_admin") is ReportScope.TENANT


def test_payroll_fields_are_for_admins_and_hr_fields_for_sub_admins() -> None:
    assert may_see("admin", Sensitivity.PAYROLL_ONLY)
    assert not may_see("sub_admin", Sensitivity.PAYROLL_ONLY)
    assert may_see("sub_admin", Sensitivity.HR_ONLY)
    assert not may_see("manager", Sensitivity.HR_ONLY)


def test_grounding_reach_follows_the_same_roles() -> None:
    def kind(role: str) -> GroundingScope:
        return scope_for(role=role, tenant_id="t1", requester_employee_id="e1").kind

    assert kind("admin") is GroundingScope.TENANT
    assert kind("sub_admin") is GroundingScope.TENANT
    assert kind("manager") is GroundingScope.TEAM
    assert kind("employee") is GroundingScope.SELF


# ── What the portal is told ────────────────────────────────────────────────


def test_the_session_says_whether_somebody_reports_to_them() -> None:
    assert UserRead.model_validate(_user(UserRole.EMPLOYEE, reports=True)).is_manager is True
    read = UserRead.model_validate(_user(UserRole.EMPLOYEE))
    assert read.is_manager is False
    assert read.model_dump()["is_manager"] is False
