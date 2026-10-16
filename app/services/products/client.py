"""AskMira's calls to a product's backend.

Both endpoints are idempotent on the product side, so a retry after a timeout
does no harm:

    POST /integrations/askmira/organizations   create or adopt this company's organisation
    POST /integrations/askmira/users/sync      the full list of people who should have access
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx

from app.services.products.settings import Endpoint
from app.services.products.tokens import provision_token

TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class ProductError(Exception):
    """The product refused, failed or could not be reached. The message is safe to show."""


async def _post(
    endpoint: Endpoint, product: str, tenant_id: uuid.UUID, path: str, payload: dict[str, Any]
) -> dict[str, Any]:
    body = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
    token = provision_token(endpoint.secret, product, tenant_id, body)
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.post(
                f"{endpoint.api_url}{path}",
                content=body,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise ProductError(f"Could not reach the product: {exc.__class__.__name__}.") from exc
    if response.status_code >= 400:
        detail: Any = None
        try:
            data = response.json()
            detail = data.get("detail") or data.get("error") if isinstance(data, dict) else None
        except ValueError:
            pass
        message = (
            detail if isinstance(detail, str) else response.text[:200] or response.reason_phrase
        )
        raise ProductError(f"The product answered {response.status_code}: {message}")
    try:
        data = response.json()
    except ValueError as exc:
        raise ProductError("The product's answer was not JSON.") from exc
    if not isinstance(data, dict):
        raise ProductError("The product's answer was not an object.")
    return data


async def provision_organisation(
    endpoint: Endpoint, product: str, tenant_id: uuid.UUID, company: dict[str, Any]
) -> str:
    """Create or adopt the company's organisation; returns the product's id for it."""
    data = await _post(endpoint, product, tenant_id, "/integrations/askmira/organizations", company)
    ref = data.get("external_ref")
    if not ref:
        raise ProductError("The product did not say which organisation it linked.")
    return str(ref)


async def sync_people(
    endpoint: Endpoint, product: str, tenant_id: uuid.UUID, people: list[dict[str, Any]]
) -> dict[str, Any]:
    """Send the complete roster; the product adds, updates and switches off to match."""
    return await _post(
        endpoint,
        product,
        tenant_id,
        "/integrations/askmira/users/sync",
        {"tenant_id": str(tenant_id), "complete": True, "users": people},
    )
