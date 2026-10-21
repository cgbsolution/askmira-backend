"""AskMira's calls to a product's backend.

    POST /integrations/askmira/organizations   create or adopt this company's organisation
    POST /integrations/askmira/users/sync      the full list of people who should have access
    POST /integrations/askmira/query           a read-only question for one person (Ask Mira)
    POST /integrations/askmira/connect/token   swap a one-time connect code for a secret
    POST /integrations/askmira/connect/revoke  the workspace disconnected

The first two are idempotent on the product side, so a retry after a timeout
does no harm; the third changes nothing. Each carries a token whose `use` names
the endpoint's kind (tokens.py) and whose `bh` binds it to the exact body.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx

from app.services.products.settings import Endpoint
from app.services.products.tokens import provision_token, query_token, revoke_token

TIMEOUT = httpx.Timeout(30.0, connect=10.0)
#: A person is waiting in a chat: a question gets less time than provisioning.
QUERY_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


class ProductError(Exception):
    """The product refused, failed or could not be reached. The message is safe to show.

    `status` is the product's HTTP status when it answered (None when it could
    not be reached), and `detail` its own message, when it gave one.
    """

    def __init__(self, message: str, *, status: int | None = None, detail: str | None = None):
        super().__init__(message)
        self.status = status
        self.detail = detail


async def _post(
    endpoint: Endpoint,
    product: str,
    tenant_id: uuid.UUID,
    path: str,
    payload: dict[str, Any],
    *,
    use: str = "provision",
    timeout: httpx.Timeout = TIMEOUT,
) -> dict[str, Any]:
    body = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
    sign = {"query": query_token, "revoke": revoke_token}.get(use, provision_token)
    token = sign(endpoint.secret, product, tenant_id, body)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
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
        raise ProductError(
            f"The product answered {response.status_code}: {message}",
            status=response.status_code,
            detail=detail if isinstance(detail, str) else None,
        )
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


async def query(
    endpoint: Endpoint,
    product: str,
    tenant_id: uuid.UUID,
    *,
    actor: dict[str, str],
    kind: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Ask the product one read-only question as `actor` (their id, email and role there)."""
    return await _post(
        endpoint,
        product,
        tenant_id,
        "/integrations/askmira/query",
        {"tenant_id": str(tenant_id), "actor": actor, "kind": kind, "params": params},
        use="query",
        timeout=QUERY_TIMEOUT,
    )


async def connect_token(
    api_url: str, product: str, *, code: str, verifier: str, tenant_id: uuid.UUID
) -> dict[str, Any]:
    """Swap the one-time code from the product's Allow for this workspace's own secret.

    Unsigned: there is no secret yet. The verifier — which only this server
    holds — is what proves the code was issued to us (PKCE).
    """
    body = {"code": code, "verifier": verifier, "tenant_id": str(tenant_id)}
    try:
        async with httpx.AsyncClient(timeout=QUERY_TIMEOUT) as client:
            response = await client.post(
                f"{api_url}/integrations/askmira/connect/token",
                json=body,
            )
    except httpx.HTTPError as exc:
        raise ProductError(f"Could not reach the product: {exc.__class__.__name__}.") from exc
    try:
        data = response.json()
    except ValueError:
        data = None
    if response.status_code >= 400 or not isinstance(data, dict):
        detail = data.get("detail") or data.get("error") if isinstance(data, dict) else None
        raise ProductError(
            f"The product answered {response.status_code}.",
            status=response.status_code,
            detail=detail if isinstance(detail, str) else None,
        )
    if data.get("product") != product or not isinstance(data.get("secret"), str):
        raise ProductError("The product's answer was not a connection.")
    return data


async def revoke(endpoint: Endpoint, product: str, tenant_id: uuid.UUID) -> None:
    """Tell the product this workspace disconnected, so it forgets the secret."""
    await _post(
        endpoint,
        product,
        tenant_id,
        "/integrations/askmira/connect/revoke",
        {"tenant_id": str(tenant_id)},
        use="revoke",
        timeout=QUERY_TIMEOUT,
    )
