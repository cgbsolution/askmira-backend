"""Shared fixtures for the W3 payroll-operations integration tests."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee import Employee, EmploymentStatus
from app.models.org import LegalEntity, Location
from app.models.payroll import PayrollRuleSet
from app.models.tenant import Tenant
from app.models.user import User, UserRole

TDS_RULES = {
    "regimes": {
        "old": {
            "slabs": [
                {"up_to": 250000, "rate": 0},
                {"up_to": 500000, "rate": 5},
                {"up_to": 1000000, "rate": 20},
                {"up_to": None, "rate": 30},
            ],
            "standard_deduction": 50000,
            "deductions_allowed": ["80C", "80D", "24B", "HRA", "PT", "LTA"],
        },
        "new": {
            "slabs": [
                {"up_to": 300000, "rate": 0},
                {"up_to": 700000, "rate": 5},
                {"up_to": 1000000, "rate": 10},
                {"up_to": None, "rate": 20},
            ],
            "standard_deduction": 75000,
            "deductions_allowed": ["80CCD2"],
        },
    },
    "section_limits": {"80C": {"ceiling": 150000}},
    "hra": {"metro_percent": 50, "non_metro_percent": 40, "rent_less_basic_percent": 10},
    "landlord_pan_threshold_annual": 100000,
    "cess_percent": 4,
    "regime_switches_per_year": 1,
    "financial_year_start_month": 4,
}


async def tds_rule_set(
    session: AsyncSession, tenant: Tenant, *, effective: date, rules: dict | None = None
) -> PayrollRuleSet:
    rule_set = PayrollRuleSet(
        tenant_id=tenant.id,
        rule_type="tds",
        jurisdiction="IN",
        version=1,
        effective_from=effective,
        approved_at=datetime.now(UTC),
        rules=rules or TDS_RULES,
    )
    session.add(rule_set)
    await session.commit()
    return rule_set


async def person(
    session: AsyncSession,
    tenant: Tenant,
    entity: LegalEntity,
    location: Location,
    *,
    code: str,
    role: UserRole,
    manager: Employee | None = None,
    pan: str | None = "ABCDE1234F",
) -> tuple[Employee, User]:
    employee = Employee(
        tenant_id=tenant.id,
        employee_code=code,
        first_name=code.title(),
        last_name="Test",
        work_email=f"{code.lower()}@acme.test",
        legal_entity_id=entity.id,
        location_id=location.id,
        manager_id=manager.id if manager else None,
        status=EmploymentStatus.ACTIVE,
        date_of_joining=date(2018, 1, 1),
        pan=pan,
        designation="Engineer",
    )
    session.add(employee)
    await session.flush()
    user = User(
        tenant_id=tenant.id,
        email=f"{code.lower()}@acme.test",
        password_hash="not-a-real-hash",
        full_name=f"{code.title()} Test",
        role=role,
        is_active=True,
        employee_id=employee.id,
    )
    session.add(user)
    await session.commit()
    await session.refresh(employee)
    await session.refresh(user)
    return employee, user


async def plain_user(session: AsyncSession, tenant: Tenant, *, email: str, role: UserRole) -> User:
    user = User(
        tenant_id=tenant.id,
        email=email,
        password_hash="not-a-real-hash",
        full_name=email.split("@")[0].title(),
        role=role,
        is_active=True,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


@pytest.fixture
def storage_root(tmp_path, monkeypatch):
    """Point the file vault at a temporary directory for the test."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "FILE_STORAGE_ROOT", str(tmp_path), raising=False)
    return tmp_path


#: A minimal valid PNG header followed by filler: the vault reads the type
#: from these bytes.
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
