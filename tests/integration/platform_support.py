"""Shared helpers for the platform (W8) integration tests."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.db.session import get_session
from app.main import app
from app.models import Employee, EmploymentStatus, Tenant, User, UserRole

PASSWORD = "Correct-horse-9"


async def make_person(
    session: AsyncSession,
    tenant: Tenant,
    *,
    role: UserRole,
    name: str,
    manager: Employee | None = None,
    with_employee: bool = True,
    password: str | None = None,
) -> User:
    """A login, optionally with an employee record and a manager."""
    slug = uuid.uuid4().hex[:8]
    emp = None
    if with_employee:
        first, _, last = name.partition(" ")
        emp = Employee(
            tenant_id=tenant.id,
            employee_code=f"E{slug}",
            first_name=first,
            last_name=last or None,
            work_email=f"{slug}@acme-demo.com",
            status=EmploymentStatus.ACTIVE,
            date_of_joining=date(2024, 1, 1),
            manager_id=manager.id if manager else None,
        )
        session.add(emp)
        await session.flush()
    user = User(
        tenant_id=tenant.id,
        email=f"{name.split()[0].lower()}.{slug}@acme-demo.com",
        password_hash=hash_password(password) if password else "not-a-real-hash",
        full_name=name,
        role=role,
        is_active=True,
        employee_id=emp.id if emp else None,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


def bearer(user: User) -> dict[str, str]:
    token = create_access_token(
        user_id=str(user.id), tenant_id=str(user.tenant_id), role=user.role.value
    )
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def client(session: AsyncSession) -> AsyncGenerator[httpx.AsyncClient, None]:
    async def override() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_session] = override
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            yield c
    finally:
        app.dependency_overrides.clear()


def now() -> datetime:
    return datetime.now(UTC)
