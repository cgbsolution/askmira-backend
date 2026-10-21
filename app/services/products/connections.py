"""Where a workspace's linked product lives, and the secret to sign its calls with.

A workspace whose admin connected the product from Settings → Connectors
(products/connectors.py) has a secret of its own, issued by the product for
that workspace alone and kept encrypted on its product link. A workspace linked
the older way falls back to the server-wide INTERVIEW_TOOL_… / EXPENSE_TOOL_…
settings. Every call to a product — people sync, a launch, an Ask Mira
question — learns its target here, by workspace.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_filter
from app.models.product_link import ProductLink
from app.models.user import User, UserRole
from app.services.products.settings import Endpoint
from app.services.products.settings import endpoint as env_endpoint

__all__ = ["endpoint", "require_super"]


def require_super(user: User) -> None:
    if user.role is not UserRole.SUPER_ADMIN or getattr(user, "operator_grant_id", None):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the platform's super admin links products.",
        )


async def endpoint(session: AsyncSession, product: str, tenant_id: uuid.UUID) -> Endpoint | None:
    """Where the product lives for this workspace and the secret it shares, or None."""
    row = await session.scalar(
        select(ProductLink).where(
            tenant_filter(ProductLink, tenant_id), ProductLink.product == product
        )
    )
    if row is not None and row.secret_encrypted and row.api_url and row.app_url:
        return Endpoint(api_url=row.api_url, app_url=row.app_url, secret=row.secret_encrypted)
    return env_endpoint(product)
