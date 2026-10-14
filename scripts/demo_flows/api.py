"""An in-process client for the API, acting as one person at a time.

Requests go through the real application — routers, dependencies, role checks,
validation, services, the database — over an ASGI transport, so nothing needs to
be listening on a port. The background scheduler does not start (the app's
lifespan is not run); a flow that depends on a scheduled job triggers it through
the admin endpoint, the way an admin would.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.security import create_access_token

API = "/api/v1"
#: Tokens live an hour; re-mint well before that so a long seed never sees a 401.
TOKEN_TTL_SECONDS = 45 * 60


class SeedError(RuntimeError):
    """A request the flow needed came back with an error."""


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:500]
    if isinstance(body, dict) and "detail" in body:
        return str(body["detail"])
    return str(body)[:500]


@dataclass
class Api:
    """One person's session with the API."""

    http: httpx.AsyncClient
    user_id: uuid.UUID
    tenant_id: uuid.UUID | None
    role: str
    label: str
    _token: str | None = None
    _minted_at: float = 0.0
    calls: int = 0

    def _headers(self) -> dict[str, str]:
        if self._token is None or time.monotonic() - self._minted_at > TOKEN_TTL_SECONDS:
            self._token = create_access_token(
                user_id=str(self.user_id),
                tenant_id=str(self.tenant_id) if self.tenant_id else None,
                role=self.role,
            )
            self._minted_at = time.monotonic()
        return {"Authorization": f"Bearer {self._token}"}

    async def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        params: dict[str, Any] | None = None,
        files: Any = None,
        data: dict[str, Any] | None = None,
        expect: tuple[int, ...] = (200, 201, 204),
    ) -> Any:
        url = path if path.startswith("/") and path.startswith(API) else f"{API}{path}"
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        if data is not None:
            # A browser's form leaves out a field nobody filled in.
            data = {k: v for k, v in data.items() if v is not None}
        response = await self.http.request(
            method,
            url,
            json=body if files is None and data is None else None,
            params=clean or None,
            files=files,
            data=data,
            headers=self._headers(),
        )
        self.calls += 1
        if response.status_code not in expect:
            raise SeedError(
                f"[{self.label}] {method} {url} -> {response.status_code}: {_detail(response)}"
            )
        if response.status_code == 204 or not response.content:
            return None
        if "json" in response.headers.get("content-type", ""):
            return response.json()
        return response.content

    async def get(self, path: str, **params: Any) -> Any:
        return await self.request("GET", path, params=params)

    async def post(self, path: str, body: Any = None, **kwargs: Any) -> Any:
        return await self.request("POST", path, body, **kwargs)

    async def put(self, path: str, body: Any = None, **kwargs: Any) -> Any:
        return await self.request("PUT", path, body, **kwargs)

    async def patch(self, path: str, body: Any = None, **kwargs: Any) -> Any:
        return await self.request("PATCH", path, body, **kwargs)

    async def delete(self, path: str, **kwargs: Any) -> Any:
        return await self.request("DELETE", path, **kwargs)

    async def step_up(self, action_key: str) -> None:
        """Stand in for the person confirming it is them before a guarded action.

        The seed has no passwords to re-enter, so it records the satisfied
        challenge the step-up dialog would have — straight into the database,
        as the trusted script it is. Money-moving routes refuse without it.
        """
        from datetime import UTC, datetime

        from app.db.session import SessionLocal
        from app.models.opsec import StepUpChallenge

        assert self.tenant_id is not None
        async with SessionLocal() as session:
            session.add(
                StepUpChallenge(
                    tenant_id=self.tenant_id,
                    user_id=self.user_id,
                    action_key=action_key,
                    reason="Seeded: confirmed by the demo script",
                    satisfied_at=datetime.now(UTC),
                )
            )
            await session.commit()

    async def refused(self, method: str, path: str, body: Any = None, *, status: int) -> str:
        """A request that must be refused with `status` — a guard the flow relies on."""
        url = f"{API}{path}"
        response = await self.http.request(
            method, url, json=body, headers=self._headers()
        )
        self.calls += 1
        if response.status_code != status:
            raise SeedError(
                f"[{self.label}] {method} {url} should have been refused with {status}, "
                f"got {response.status_code}: {_detail(response)}"
            )
        return _detail(response)


@dataclass
class Person:
    """Someone in the demo company, with their API session once they have a login."""

    key: str
    code: str | None
    first_name: str
    last_name: str
    email: str
    role: str = "employee"
    department: str | None = None
    location: str = "BLR"
    designation: str | None = None
    grade: str | None = None
    manager: str | None = None
    gender: str = "undisclosed"
    date_of_birth: str | None = None
    date_of_joining: str | None = None
    ctc: int | None = None
    phone: str | None = None
    employee_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    password: str | None = None
    api: Api | None = None

    @property
    def name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


@dataclass
class Demo:
    """Everything the flows share: the people, and ids worth remembering."""

    http: httpx.AsyncClient
    tenant_id: uuid.UUID
    people: dict[str, Person]
    ids: dict[str, Any] = field(default_factory=dict)
    report: list[tuple[str, str]] = field(default_factory=list)

    def __getitem__(self, key: str) -> Api:
        person = self.people[key]
        if person.api is None:
            raise SeedError(f"{person.name} has no login yet")
        return person.api

    def hr_for(self, key: str) -> Api:
        """HR acting on someone's record. Nobody in HR acts on their own."""
        return self["karthik"] if key == "priya" else self["priya"]

    def emp(self, key: str) -> str:
        employee_id = self.people[key].employee_id
        if employee_id is None:
            raise SeedError(f"{self.people[key].name} has no employee record yet")
        return str(employee_id)

    def note(self, flow: str, what: str) -> None:
        self.report.append((flow, what))
        print(f"  [{flow}] {what}", flush=True)
