"""Equipment received through ProcureFlow, added to the asset register from AskMiraa.

Real database; ProcureFlow (``receipts``) and CGB Helpdesk (``asset.create``)
are fakes.

* Each accepted unit becomes an asset — in AskMiraa's register, with its serial,
  cost, vendor and date — and adding the line again adds nothing.
* With the helpdesk linked, the units go to its register through its own
  action, and are remembered the same way.
* Only HR; without ProcureFlow linked nothing is asked.
"""

from __future__ import annotations

import json
import types
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import httpx
import jwt
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.assets import Asset, AssetCategory
from app.models.procured_assets import ProcuredAsset
from app.models.user import UserRole
from app.services.assets import received
from app.services.products import client as product_client
from app.services.products import settings as product_settings
from tests.integration.conftest import as_caller, requires_db
from tests.integration.platform_support import make_person
from tests.integration.test_sales_procurement_link import _link

pytestmark = requires_db

SECRETS = {
    "procurement": "procure-secret-0123456789abcdef0123456789",
    "helpdesk": "helpdesk-secret-0123456789abcdef0123456789",
}
HOSTS = {"procurement": "pf-api.test", "helpdesk": "helpdesk.test"}
PREFIX = {"procurement": "PROCUREMENT_TOOL", "helpdesk": "HELPDESK_TOOL"}

RECEIPT = {
    "ref": "grnline-1",
    "grn": "GRN/2026/0042",
    "received_on": "2026-11-02",
    "item": "ThinkPad E14",
    "description": "Laptop 16GB",
    "category": "Laptop",
    "sku": "LAP-E14",
    "quantity": "2",
    "uom": "NOS",
    "serial_numbers": ["SN-A1", "SN-A2"],
    "unit_cost": "68500.00",
    "po": "PO/2026/0101",
    "vendor": "Dell India",
    "requested_by": "Mala Manager",
    "requested_by_email": "mala@acme.test",
}


class Fakes:
    def __init__(self) -> None:
        self.made: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        product = next(p for p, h in HOSTS.items() if h == request.url.host)
        jwt.decode(
            request.headers["Authorization"].removeprefix("Bearer "),
            SECRETS[product],
            algorithms=["HS256"],
            audience=f"askmira:{product}",
            issuer="askmira",
        )
        body = json.loads(request.content)
        if request.url.path.endswith("users/sync"):
            return httpx.Response(200, json={"created": 0, "updated": 0, "deactivated": 0, "members": []})
        if product == "procurement" and body["kind"] == "receipts":
            return httpx.Response(200, json={"kind": "receipts", "actor": {"role": "finance"}, "days": 90, "receipts": [RECEIPT]})
        if product == "helpdesk" and body["kind"] == "asset.create":
            self.made.append(body["params"])
            return httpx.Response(200, json={"kind": "asset.create", "asset": {"id": f"hd-{len(self.made)}", "code": body["params"]["asset_code"]}, "existing": False})
        return httpx.Response(422, json={"error": "unexpected"})


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> Iterator[Fakes]:
    for product, prefix in PREFIX.items():
        api = f"https://{HOSTS[product]}" + ("/api" if product == "helpdesk" else "")
        monkeypatch.setenv(f"{prefix}_API_URL", api)
        monkeypatch.setenv(f"{prefix}_APP_URL", f"https://{product}-app.test")
        monkeypatch.setenv(f"{prefix}_SECRET", SECRETS[product])
    product_settings.reset_cache()
    fake = Fakes()
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


async def _hr(session, tenant):
    return await as_caller(
        session, await make_person(session, tenant, role=UserRole.ADMIN, name="Asha Admin")
    )


async def test_received_units_become_assets_in_askmiras_register_once(session, tenant, fakes) -> None:
    hr = await _hr(session, tenant)
    await _link(session, tenant, "procurement", {hr.email: "finance"})
    category = AssetCategory(tenant_id=tenant.id, code="LAP", name="Laptop")
    session.add(category)
    await session.commit()

    listed = await received.list_received(session, hr)
    assert listed["register"] == "askmira"
    [row] = listed["receipts"]
    assert (row["units"], row["added"]) == (2, 0)

    result = await received.add_to_register(session, hr, receipt_ref="grnline-1", category_id=category.id)
    assert result["added"] == ["GRN-2026-0042-01", "GRN-2026-0042-02"]
    assets = (await session.scalars(select(Asset).order_by(Asset.asset_tag))).all()
    assert [(a.serial_number, a.purchase_cost, a.vendor) for a in assets] == [
        ("SN-A1", Decimal("68500.00"), "Dell India"),
        ("SN-A2", Decimal("68500.00"), "Dell India"),
    ]
    assert all(a.name == "ThinkPad E14" for a in assets)

    again = await received.add_to_register(session, hr, receipt_ref="grnline-1", category_id=category.id)
    assert again["added"] == []
    assert len((await session.scalars(select(Asset))).all()) == 2
    assert (await received.list_received(session, hr))["receipts"][0]["added"] == 2


async def test_with_the_helpdesk_units_go_to_its_register(session, tenant, fakes) -> None:
    hr = await _hr(session, tenant)
    await _link(session, tenant, "procurement", {hr.email: "finance"})
    await _link(session, tenant, "helpdesk", None)

    result = await received.add_to_register(session, hr, receipt_ref="grnline-1", category_id=None)
    assert result["register"] == "helpdesk"
    assert [m["serial_no"] for m in fakes.made] == ["SN-A1", "SN-A2"]
    assert fakes.made[0]["source_ref"] == "procureflow:grnline-1:1"
    kept = (await session.scalars(select(ProcuredAsset))).all()
    assert {k.register for k in kept} == {"helpdesk"} and len(kept) == 2
    assert (await session.scalars(select(Asset))).all() == []


async def test_only_hr_and_only_with_procureflow(session, tenant, fakes) -> None:
    hr = await _hr(session, tenant)
    assert (await received.list_received(session, hr))["linked"] is False
    staff = await as_caller(
        session, await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Eve Emp")
    )
    with pytest.raises(HTTPException) as refused:
        await received.list_received(session, staff)
    assert refused.value.status_code == 403
    await _link(session, tenant, "procurement", {hr.email: "finance"})
    with pytest.raises(HTTPException) as no_category:
        await received.add_to_register(session, hr, receipt_ref="grnline-1", category_id=None)
    assert "category" in no_category.value.detail
