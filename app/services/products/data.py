"""Reading a linked product's records for one person: what Ask Mira shows.

AskMira asks the product server to server (client.query), signed with that
product's own secret, on behalf of the person asking:

    POST /integrations/askmira/query
    {"tenant_id", "actor": {"id", "email", "role"}, "kind", "params"}

* The tenant is always the caller's own (`user.tenant_id`, from the JWT's `tid`),
  never a value from a request or from a model.
* The role is the one AskMira gives the person in that product
  (catalogue.role_for). The product checks it against its own record as well,
  and answers only for the organisation linked to this workspace.
* The workspace's link is read through tenant_scoped(). A product that is not
  configured on this server, not linked, or switched off is never asked.

What comes back is shown to the person who asked. It is never sent to a model.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.product_link import ProductLink
from app.models.user import User
from app.services.products import client
from app.services.products.catalogue import PRODUCTS
from app.services.products.connections import endpoint
from app.services.products.service import ACTIVE

__all__ = ["ProductUnavailableError", "ask", "connection", "role_in"]


class ProductUnavailableError(Exception):
    """The product can't answer this person now. The message is safe to show."""


async def role_in(session: AsyncSession, user: User, product_key: str) -> str | None:
    """The person's role in the product, or None when they can't read from it here.

    None when the product is unknown or not configured on this server, when the
    caller is an outside operator acting in this workspace (they have no account
    in the product), when the workspace has no active link to it, or when the
    person's AskMira role gives them no role there.
    """
    product = PRODUCTS.get(product_key)
    if product is None or await endpoint(session, product_key, user.tenant_id) is None:
        return None
    if getattr(user, "operator_grant_id", None):
        return None
    row = await session.scalar(
        tenant_scoped(select(ProductLink), ProductLink, user).where(
            ProductLink.product == product_key
        )
    )
    if row is None or row.status != ACTIVE:
        return None
    return product.role_for(user.role, is_manager=bool(getattr(user, "has_reports", False)))


async def connection(session: AsyncSession, user: User, product_key: str) -> dict[str, str]:
    """Where the product stands for this person: set up here, linked, and their access.

    Each answer is a short sentence a person can act on. Nothing is asked of the
    product itself.
    """
    product = PRODUCTS[product_key]
    if await endpoint(session, product_key, user.tenant_id) is None:
        return {
            "server": "Not set up",
            "workspace": "Can't be linked until the server is set up",
            "access": "None",
        }
    row = await session.scalar(
        tenant_scoped(select(ProductLink), ProductLink, user).where(
            ProductLink.product == product_key
        )
    )
    if row is None:
        workspace = "Not linked; the platform admin links it from the console"
    elif row.status != ACTIVE:
        workspace = "Linked but switched off"
    else:
        workspace = "Linked"
    if getattr(user, "operator_grant_id", None):
        access = "None while you act in this workspace as the platform operator"
    elif row is None or row.status != ACTIVE:
        access = "None"
    else:
        role = product.role_for(user.role, is_manager=bool(getattr(user, "has_reports", False)))
        access = f"As {role}" if role else "None for your role"
    return {"server": "Set up", "workspace": workspace, "access": access}


def _explain(brand: str, exc: client.ProductError) -> str:
    if exc.status == 403 and exc.detail:
        # The product's own words: why this person may not read this there.
        return exc.detail[:300]
    if exc.status == 404:
        return (
            f"{brand} does not have your company linked any more. "
            "The platform admin can link it again from the console."
        )
    if exc.status == 401:
        return (
            f"{brand} did not accept AskMira's request. "
            "The platform admin should check the shared secret on both sides."
        )
    if exc.status == 422 and exc.detail:
        return f"{brand} could not answer that: {exc.detail[:200]}"
    return f"{brand} could not be reached just now. Try again in a minute."


async def ask(
    session: AsyncSession, user: User, product_key: str, kind: str, params: dict[str, Any]
) -> dict[str, Any]:
    """Ask the product one read-only question as `user`; its answer, as it gave it."""
    product = PRODUCTS[product_key]
    role = await role_in(session, user, product_key)
    target = await endpoint(session, product_key, user.tenant_id)
    if role is None or target is None:
        raise ProductUnavailableError(f"{product.brand} is not linked to your workspace for you.")
    try:
        data = await client.query(
            target,
            product_key,
            user.tenant_id,
            actor={"id": str(user.id), "email": user.email.lower(), "role": role},
            kind=kind,
            params=params,
        )
    except client.ProductError as exc:
        raise ProductUnavailableError(_explain(product.brand, exc)) from exc
    if data.get("kind") != kind:
        raise ProductUnavailableError(f"{product.brand} answered a different question. Try again.")
    return data
