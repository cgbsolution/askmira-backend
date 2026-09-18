"""An `engine` fixture for the hiring integration suites that survives a busy server.

The shared fixture drops the whole schema in one transaction. That takes a lock
per table, index, constraint and type — well over a thousand — and the lock
table is shared by every database on the server. With several test suites
running at once it runs out ("out of shared memory"), and the test errors in
setup rather than failing on anything it checks.

This one drops the tables in small batches, each in its own transaction, and
retries a step that hit a momentary shortage. Import it into a test module to
shadow the shared fixture:

    from tests.hiring.db_fixture import engine  # noqa: F401
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.models import Base
from tests.integration.conftest import TEST_DATABASE_URL, _guard

_BATCH = 20
_ATTEMPTS = 6


async def _retrying(engine, statement_fn) -> None:
    for attempt in range(_ATTEMPTS):
        try:
            async with engine.begin() as conn:
                await statement_fn(conn)
            return
        except DBAPIError as exc:
            if "out of shared memory" not in str(exc) or attempt == _ATTEMPTS - 1:
                raise
            await asyncio.sleep(1.5 * (attempt + 1))


@pytest_asyncio.fixture(scope="function")
async def engine():
    if not TEST_DATABASE_URL:
        pytest.skip("no TEST_DATABASE_URL")

    engine = create_async_engine(
        _guard(TEST_DATABASE_URL),
        echo=False,
        poolclass=NullPool,
        connect_args={"statement_cache_size": 0},
    )

    async with engine.connect() as conn:
        tables = [
            row[0]
            for row in await conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
        ]
    for start in range(0, len(tables), _BATCH):
        chunk = tables[start : start + _BATCH]
        names = ", ".join(f'"{name}"' for name in chunk)

        async def drop(conn, names=names):
            await conn.execute(text(f"DROP TABLE IF EXISTS {names} CASCADE"))

        await _retrying(engine, drop)

    async def reset(conn):
        await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))

    await _retrying(engine, reset)

    async def create(conn):
        await conn.run_sync(Base.metadata.create_all)

    await _retrying(engine, create)
    try:
        yield engine
    finally:
        await engine.dispose()
