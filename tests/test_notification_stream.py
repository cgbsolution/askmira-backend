"""The in-app notification stream must not hold a database connection.

It used to read with the request's session. FastAPI closes that before a
StreamingResponse starts, and a closed session used again quietly checks out a
fresh pool connection that is never returned: every open app held one for as
long as it stayed open, and fifteen of them emptied the pool for everyone.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from app.api.v1.routers import notifications as router
from app.core.deps import DbSession


class _Session:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.open = False

    async def __aenter__(self) -> _Session:
        self.open = True
        self.log.append("open")
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.open = False
        self.log.append("close")


class _Request:
    """Connected for the first `polls` checks, then gone."""

    def __init__(self, polls: int) -> None:
        self.checks = 0
        self.polls = polls

    async def is_disconnected(self) -> bool:
        self.checks += 1
        return self.checks > self.polls


def test_the_stream_does_not_take_the_request_session() -> None:
    params = inspect.signature(router.stream).parameters
    assert all(p.annotation not in (DbSession, "DbSession") for p in params.values()), (
        "The stream depends on the request's session again; it is closed before streaming "
        "starts, and using it leaks a pool connection per open stream."
    )


async def test_each_poll_gives_its_connection_back_before_waiting(monkeypatch) -> None:
    log: list[str] = []
    sessions: list[_Session] = []

    def factory() -> _Session:
        session = _Session(log)
        sessions.append(session)
        return session

    created = datetime.now(UTC)

    async def feed(session, user, since):
        assert session.open, "the feed must read inside the borrowed session"
        log.append("read")
        if len(sessions) == 1:
            return [
                {
                    "id": str(uuid.uuid4()),
                    "event_key": "leave.approved",
                    "subject": "Leave approved",
                    "body": "Your leave was approved.",
                    "is_urgent": False,
                    "created_at": created,
                }
            ]
        return []

    async def no_wait(seconds: float) -> None:
        log.append("sleep")
        # Waiting with a connection checked out is the bug.
        assert all(not s.open for s in sessions), "slept while holding a connection"

    monkeypatch.setattr(router.notification_feed, "new_in_app_since", feed)
    monkeypatch.setattr(router.asyncio, "sleep", no_wait)

    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4())
    response = await router.stream(user=user, request=_Request(polls=20), sessions=factory)
    frames = [frame async for frame in response.body_iterator]

    assert frames[0] == ": connected\n\n"
    assert any(f.startswith("event: notification\n") and "Leave approved" in f for f in frames)
    assert log.count("open") == log.count("close") >= 2, log
    assert all(not s.open for s in sessions)


async def test_a_departed_client_ends_the_stream_promptly(monkeypatch) -> None:
    waits: list[float] = []

    async def feed(session, user, since):
        return []

    async def no_wait(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(router.notification_feed, "new_in_app_since", feed)
    monkeypatch.setattr(router.asyncio, "sleep", no_wait)

    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4())
    # Gone just after the first poll: the stream notices within a second.
    response = await router.stream(
        user=user, request=_Request(polls=2), sessions=lambda: _Session([])
    )
    _ = [frame async for frame in response.body_iterator]
    assert sum(waits) <= 1, waits
