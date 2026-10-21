"""The signed assertions AskMira hands a product.

Four kinds, all HS256 with the product's own secret, all good for 60 seconds:

* **provision** — sent as the Bearer token on AskMira's server-to-server calls
  that change the product (the organisation, the roster). `bh` is the SHA-256
  of the exact request body, so a captured token can't be replayed with a
  different body.
* **query** — the same shape, for a read-only question asked on behalf of one
  person (Ask Mira). A separate `use`, so a query token can never provision
  and a provisioning token can never read.
* **revoke** — sent when a workspace's admin disconnects the product from
  Connectors: the product forgets that workspace's secret. `bh` as above.
* **launch** — carried in the URL fragment when a person opens the product. It
  names the person and their role there; the product accepts each `jti` once.

Common claims: iss "askmira", aud "askmira:<product>", sub, iat, exp, jti, use.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

ISSUER = "askmira"
LIFETIME = timedelta(seconds=60)


def audience(product: str) -> str:
    return f"askmira:{product}"


def _sign(secret: str, product: str, subject: str, use: str, extra: dict[str, Any]) -> str:
    now = datetime.now(UTC)
    claims = {
        "iss": ISSUER,
        "aud": audience(product),
        "sub": subject,
        "use": use,
        "iat": int(now.timestamp()),
        "exp": int((now + LIFETIME).timestamp()),
        "jti": secrets.token_urlsafe(18),
        **extra,
    }
    return jwt.encode(claims, secret, algorithm="HS256")


def body_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def provision_token(secret: str, product: str, tenant_id: uuid.UUID, body: bytes) -> str:
    return _sign(secret, product, str(tenant_id), "provision", {"bh": body_hash(body)})


def query_token(secret: str, product: str, tenant_id: uuid.UUID, body: bytes) -> str:
    return _sign(secret, product, str(tenant_id), "query", {"bh": body_hash(body)})


def revoke_token(secret: str, product: str, tenant_id: uuid.UUID, body: bytes) -> str:
    """The workspace disconnected: the product forgets the secret this token is signed with."""
    return _sign(secret, product, str(tenant_id), "revoke", {"bh": body_hash(body)})


def launch_token(
    secret: str,
    product: str,
    *,
    tenant_id: uuid.UUID,
    external_ref: str | None,
    user_id: uuid.UUID,
    email: str,
    name: str,
    role: str,
) -> str:
    return _sign(
        secret,
        product,
        str(user_id),
        "launch",
        {
            "tid": str(tenant_id),
            "ext": external_ref,
            "email": email.lower(),
            "name": name,
            "role": role,
        },
    )
