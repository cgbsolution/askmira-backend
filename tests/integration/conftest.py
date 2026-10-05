"""Integration test harness.

These tests need a real PostgreSQL — the schema uses JSONB, ARRAY, INET and
native enums, none of which SQLite emulates. Rather than compromise the schema
to make it testable in-process, the integration suite skips cleanly when no
database is configured and runs in full when one is.

Point it at a throwaway database:

    export TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/askmira_test
    pytest tests/integration

The schema is created from the models and dropped between modules, so the
target database is emptied. Never point this at anything you care about — the
fixture refuses a URL that does not look like a test database.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.deps import has_direct_reports
from app.models import (
    Base,
    Employee,
    EmploymentStatus,
    LegalEntity,
    Location,
    Tenant,
    User,
    UserRole,
)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Set TEST_DATABASE_URL to a throwaway Postgres to run integration tests",
)


def _guard(url: str) -> str:
    """Refuse to drop tables in anything that is not obviously a test database.

    The fixture calls `drop_all`. A typo in an environment variable should not
    be able to empty a real workspace.
    """
    if "test" not in url.rsplit("/", 1)[-1].lower():
        raise RuntimeError(
            f"TEST_DATABASE_URL must point at a database whose name contains 'test'. "
            f"Got: ...{url.rsplit('/', 1)[-1]}"
        )
    return url


@pytest_asyncio.fixture(scope="function")
async def engine():
    if not TEST_DATABASE_URL:
        pytest.skip("no TEST_DATABASE_URL")

    engine = create_async_engine(
        _guard(TEST_DATABASE_URL),
        echo=False,
        # NullPool, not the default queue pool. Every test builds and disposes
        # its own engine, and a pooled connection that outlives the dispose
        # shows up as "Could not refresh instance" on a fixture two tests later —
        # a failure that vanishes when the test is run on its own, which is the
        # worst kind to chase.
        poolclass=NullPool,
        # asyncpg caches a prepared statement plan per connection, and this
        # fixture drops and recreates the whole schema on every test. A pooled
        # connection carrying a plan for the previous schema fails with
        # InvalidCachedStatementError the moment the model layer changes shape —
        # which happens every time a slice adds a table, so it surfaces as a
        # test that passed yesterday failing today for no visible reason.
        connect_args={"statement_cache_size": 0},
    )

    # Drop the schema rather than the tables. `metadata.drop_all` cannot order
    # the deferred `use_alter` constraint on a database where the table is
    # already gone, and it leaves native enum types behind, which then collide
    # on the next create. Dropping the schema clears tables, constraints and
    # types in one statement — safe here because `_guard` has already refused
    # anything that is not a test database.
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def session(engine) -> AsyncGenerator[AsyncSession, None]:
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest_asyncio.fixture
async def tenant(session: AsyncSession) -> Tenant:
    tenant = Tenant(
        name="Acme Technologies",
        slug=f"acme-{uuid.uuid4().hex[:8]}",
        enabled_modules={"core_hr": True, "payroll": True, "leave": True},
        settings={"currency": "INR"},
    )
    session.add(tenant)
    await session.commit()
    await session.refresh(tenant)
    return tenant


@pytest_asyncio.fixture
async def other_tenant(session: AsyncSession) -> Tenant:
    """A second tenant, so isolation can be asserted rather than assumed."""
    tenant = Tenant(
        name="Globex",
        slug=f"globex-{uuid.uuid4().hex[:8]}",
        enabled_modules={"core_hr": True, "payroll": True},
    )
    session.add(tenant)
    await session.commit()
    await session.refresh(tenant)
    return tenant


@pytest_asyncio.fixture
async def hr_user(session: AsyncSession, tenant: Tenant) -> User:
    user = User(
        tenant_id=tenant.id,
        email="hr@acme.test",
        password_hash="not-a-real-hash",
        full_name="Priya Ramesh",
        role=UserRole.ADMIN,
        is_active=True,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


@pytest_asyncio.fixture
async def other_hr_user(session: AsyncSession, other_tenant: Tenant) -> User:
    user = User(
        tenant_id=other_tenant.id,
        email="hr@globex.test",
        password_hash="not-a-real-hash",
        full_name="Sam Okafor",
        role=UserRole.ADMIN,
        is_active=True,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


@pytest_asyncio.fixture
async def legal_entity(session: AsyncSession, tenant: Tenant) -> LegalEntity:
    entity = LegalEntity(tenant_id=tenant.id, name="Acme Technologies Pvt Ltd", code="ACME")
    session.add(entity)
    await session.commit()
    await session.refresh(entity)
    return entity


@pytest_asyncio.fixture
async def location(session: AsyncSession, tenant: Tenant, legal_entity: LegalEntity) -> Location:
    loc = Location(
        tenant_id=tenant.id,
        legal_entity_id=legal_entity.id,
        name="Bengaluru",
        code="BLR",
        city="Bengaluru",
        state_code="KA",
    )
    session.add(loc)
    await session.commit()
    await session.refresh(loc)
    return loc


@pytest_asyncio.fixture
async def employee(
    session: AsyncSession, tenant: Tenant, legal_entity: LegalEntity, location: Location
) -> Employee:
    person = Employee(
        tenant_id=tenant.id,
        employee_code="ACM001",
        first_name="Anita",
        last_name="Sharma",
        work_email="anita@acme.test",
        legal_entity_id=legal_entity.id,
        location_id=location.id,
        status=EmploymentStatus.ACTIVE,
        date_of_joining=datetime.now(UTC).date().replace(month=1, day=1),
    )
    session.add(person)
    await session.commit()
    await session.refresh(person)
    return person


async def as_caller(session: AsyncSession, user: User) -> User:
    """Ready a test-built login the way `get_current_user` readies a caller.

    A test that hands a User straight to a service skips the request pipeline,
    so whether anybody reports to them — what makes them a manager — has to be
    worked out here.
    """
    user.has_reports = await has_direct_reports(session, user)
    return user
