"""Candidates hired in AnytimeHire, listed for HR to add as joiners, against a real database.

AnytimeHire is a fake answering its ``hires`` query. Holds that only HR is
answered, that someone already in the directory is not listed again, that the
job's department and location are matched to this workspace's by name, and
that without AnytimeHire linked nothing is asked.
"""

from __future__ import annotations

import hashlib
import json
import types
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import jwt
import pytest
from fastapi import HTTPException

from app.models.org import Department, Location
from app.models.user import UserRole
from app.services.onboarding import linked_hires
from app.services.products import client as product_client
from app.services.products import settings as product_settings
from tests.integration.conftest import as_caller, requires_db
from tests.integration.platform_support import make_person
from tests.integration.test_sales_procurement_link import _link

pytestmark = requires_db

SECRET = "interview-secret-0123456789abcdef012345678"


class FakeHire:
    def __init__(self) -> None:
        self.hires: list[dict[str, Any]] = []
        self.asked: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        claims = jwt.decode(
            request.headers["Authorization"].removeprefix("Bearer "),
            SECRET,
            algorithms=["HS256"],
            audience="askmira:interview",
            issuer="askmira",
        )
        assert claims["bh"] == hashlib.sha256(request.content).hexdigest()
        body = json.loads(request.content)
        self.asked.append(body)
        if body.get("kind") == "hires":
            assert body["actor"]["role"] == "admin"
            return httpx.Response(200, json={"kind": "hires", "total": len(self.hires), "hires": self.hires})
        return httpx.Response(200, json={"created": 0, "updated": 0, "deactivated": 0})


@pytest.fixture
def hire(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeHire]:
    monkeypatch.setenv("INTERVIEW_TOOL_API_URL", "https://hire-api.test")
    monkeypatch.setenv("INTERVIEW_TOOL_APP_URL", "https://hire.test")
    monkeypatch.setenv("INTERVIEW_TOOL_SECRET", SECRET)
    product_settings.reset_cache()
    fake = FakeHire()
    real = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(fake.handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(
        product_client,
        "httpx",
        types.SimpleNamespace(AsyncClient=factory, HTTPError=httpx.HTTPError, Timeout=httpx.Timeout),
    )
    yield fake
    product_settings.reset_cache()


def _hire(name: str, email: str, **extra: Any) -> dict[str, Any]:
    return {
        "ref": f"cand-{uuid.uuid4().hex[:6]}",
        "name": name,
        "email": email,
        "phone": "+91 90000 00001",
        "job": "Account Executive",
        "department": "Sales West",
        "location": "Pune",
        "applied_at": "2026-10-01T10:00:00+00:00",
        **extra,
    }


async def test_hr_sees_hires_not_yet_in_the_directory_matched_to_departments(
    session, tenant, legal_entity, hire
) -> None:
    admin = await as_caller(
        session, await make_person(session, tenant, role=UserRole.ADMIN, name="Asha Admin")
    )
    existing = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Already Here")
    sales = Department(tenant_id=tenant.id, name="Sales West", code="SW")
    pune = Location(
        tenant_id=tenant.id, legal_entity_id=legal_entity.id, name="Pune", code="PNQ", state_code="MH"
    )
    session.add_all([sales, pune])
    await session.commit()
    await _link(session, tenant, "interview", None)
    hire.hires = [
        _hire("Priya Nair", "Priya@Example.test"),
        _hire("Already Here", existing.email),
        _hire("Kiran Rao", "kiran@example.test", department="Nowhere", location=None),
    ]

    answer = await linked_hires.list_hires(session, admin)
    assert answer["linked"] is True
    names = [h["name"] for h in answer["hires"]]
    assert names == ["Priya Nair", "Kiran Rao"], "someone already in the directory is not listed"
    priya = answer["hires"][0]
    assert (priya["first_name"], priya["last_name"], priya["email"]) == ("Priya", "Nair", "priya@example.test")
    assert priya["department_id"] == str(sales.id) and priya["location_id"] == str(pune.id)
    kiran = answer["hires"][1]
    assert kiran["department_id"] is None and kiran["location_id"] is None


async def test_only_hr_and_only_with_anytimehire_linked(session, tenant, hire) -> None:
    admin = await as_caller(
        session, await make_person(session, tenant, role=UserRole.ADMIN, name="Asha Admin")
    )
    staff = await as_caller(
        session, await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Eve Emp")
    )
    hire.hires = [_hire("Priya Nair", "priya@example.test")]

    assert await linked_hires.list_hires(session, admin) == {
        "linked": False,
        "brand": "AnytimeHire",
        "hires": [],
    }
    assert hire.asked == []

    await _link(session, tenant, "interview", None)
    with pytest.raises(HTTPException) as refused:
        await linked_hires.list_hires(session, staff)
    assert refused.value.status_code == 403
