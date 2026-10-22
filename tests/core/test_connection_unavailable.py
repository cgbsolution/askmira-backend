"""No free database connection is a 503 the portal can explain, not a hang.

Before, a full Supabase pooler held each request for up to a minute per
connect attempt; the portal's proxy gave up first and showed a bare 502.
"""

from __future__ import annotations

import asyncio
import json

import asyncpg
import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from starlette.requests import Request

from app.main import database_handler, unavailable_handler


def _request() -> Request:
    return Request(
        {"type": "http", "method": "POST", "path": "/api/v1/auth/identify", "headers": []}
    )


def _detail(response) -> str:
    return json.loads(response.body)["detail"]


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError(),
        ConnectionRefusedError(),
        asyncpg.InternalServerError(
            "(EMAXCONNSESSION) max clients reached in session mode - max clients are "
            "limited to pool_size: 15"
        ),
    ],
)
def test_a_connection_that_cannot_be_opened_is_busy(exc: Exception) -> None:
    response = asyncio.run(unavailable_handler(_request(), exc))

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert "busy" in _detail(response)


def test_an_exhausted_pool_is_busy() -> None:
    response = asyncio.run(database_handler(_request(), PoolTimeoutError("QueuePool limit")))

    assert response.status_code == 503
    assert "busy" in _detail(response)


def test_a_failing_query_is_still_a_500_without_detail() -> None:
    exc = OperationalError("SELECT secret FROM users", {}, Exception("boom"))

    response = asyncio.run(database_handler(_request(), exc))

    assert response.status_code == 500
    assert "secret" not in _detail(response)
