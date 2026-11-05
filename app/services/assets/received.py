"""Equipment received through ProcureFlow, added to the asset register from AskMira.

With ProcureFlow linked, its posted goods receipts (kind ``receipts``) are
listed for HR beside the register: what came in, how many, the serial numbers,
the landed cost and who asked for it. "Add to register" turns each unit into an
asset — in AskMira's own register, or in CGB Helpdesk's when the helpdesk is
linked (kind ``asset.create``, which only its IT team may do) — as a spare
ready to hand to the joiner it was bought for. A unit added once is never added
again (ProcuredAsset).
"""

from __future__ import annotations

import re
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.procured_assets import ProcuredAsset
from app.models.tenant import Tenant
from app.models.user import User
from app.services.products import data as products

__all__ = ["add_to_register", "list_received", "register_of"]

MAX_UNITS = 50


def _bad(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


async def register_of(session: AsyncSession, tenant_id: uuid.UUID) -> str:
    tenant = await session.get(Tenant, tenant_id)
    return "helpdesk" if tenant and (tenant.linked_products or {}).get("helpdesk") else "askmira"


async def _linked(session: AsyncSession, tenant_id: uuid.UUID) -> bool:
    tenant = await session.get(Tenant, tenant_id)
    return bool(tenant and (tenant.linked_products or {}).get("procurement"))


def _units(receipt: dict[str, Any]) -> int:
    try:
        n = int(Decimal(str(receipt.get("quantity") or 0)))
    except (InvalidOperation, ValueError):
        n = 0
    return max(0, min(n, MAX_UNITS))


async def list_received(session: AsyncSession, user: User) -> dict[str, Any]:
    if not permissions.is_hr(user):
        raise permissions.forbidden("Received equipment is added to the register by HR.")
    register = await register_of(session, user.tenant_id)
    if not await _linked(session, user.tenant_id):
        return {"linked": False, "register": register, "receipts": []}
    try:
        answer = await products.ask(session, user, "procurement", "receipts", {"days": 90})
    except products.ProductUnavailableError as exc:
        return {"linked": True, "register": register, "receipts": [], "error": str(exc)}
    added = {
        row.source_ref: row
        for row in (
            await session.scalars(tenant_scoped(select(ProcuredAsset), ProcuredAsset, user))
        ).all()
    }
    out = []
    for receipt in answer.get("receipts") or []:
        if not isinstance(receipt, dict) or not receipt.get("ref"):
            continue
        units = _units(receipt)
        done = [
            added[f"{receipt['ref']}:{n}"]
            for n in range(1, units + 1)
            if f"{receipt['ref']}:{n}" in added
        ]
        out.append(
            {
                **{
                    k: receipt.get(k)
                    for k in (
                        "ref",
                        "grn",
                        "received_on",
                        "item",
                        "description",
                        "category",
                        "sku",
                        "quantity",
                        "uom",
                        "serial_numbers",
                        "unit_cost",
                        "po",
                        "vendor",
                        "requested_by",
                        "requested_by_email",
                    )
                },  # fmt: skip
                "units": units,
                "added": len(done),
                "asset_codes": [d.asset_code for d in done],
            }
        )
    return {"linked": True, "register": register, "receipts": out}


def _code(grn: str, unit: int) -> str:
    base = re.sub(r"[^A-Za-z0-9-]+", "-", str(grn or "PF")).strip("-").upper()[:48] or "PF"
    return f"{base}-{unit:02d}"


async def add_to_register(
    session: AsyncSession,
    user: User,
    *,
    receipt_ref: str,
    category_id: uuid.UUID | None,
    request: Request | None = None,
) -> dict[str, Any]:
    """Add every unit of one received line that is not in the register yet."""
    from app.services.assets import service as assets

    listed = await list_received(session, user)
    receipt = next((r for r in listed["receipts"] if r["ref"] == receipt_ref), None)
    if receipt is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="That receipt is not among ProcureFlow's posted receipts of the last 90 days.",
        )
    if receipt["units"] == 0:
        raise _bad("Nothing was accepted on that receipt line.")
    register = listed["register"]
    if register == "askmira" and category_id is None:
        raise _bad("Choose the asset category the units go under.")
    serials = list(receipt.get("serial_numbers") or [])
    try:
        cost = Decimal(str(receipt.get("unit_cost"))) if receipt.get("unit_cost") else None
    except InvalidOperation:
        cost = None
    received_on = date.fromisoformat(receipt["received_on"]) if receipt.get("received_on") else None
    made: list[str] = []
    for unit in range(1, receipt["units"] + 1):
        source = f"{receipt_ref}:{unit}"
        exists = await session.scalar(
            tenant_scoped(
                select(ProcuredAsset).where(ProcuredAsset.source_ref == source),
                ProcuredAsset,
                user,
            )
        )
        if exists is not None:
            continue
        code = _code(receipt.get("grn") or "PF", unit)
        serial = serials[unit - 1] if unit - 1 < len(serials) else None
        if register == "askmira":
            asset = await assets.create_asset(
                session,
                user,
                {
                    "category_id": category_id,
                    "asset_tag": code,
                    "name": str(receipt.get("item") or "Equipment")[:160],
                    "serial_number": serial,
                    "model": (str(receipt.get("description") or "")[:120] or None),
                    "purchase_date": received_on,
                    "purchase_cost": cost,
                    "vendor": (str(receipt.get("vendor") or "")[:160] or None),
                    "invoice_number": None,
                    "notes": f"From ProcureFlow {receipt.get('po')} / {receipt.get('grn')}",
                },
                request=request,
            )
            asset_ref, asset_code = str(asset.id), asset.asset_tag
        else:
            try:
                answer = await products.act(
                    session,
                    user,
                    "helpdesk",
                    "asset.create",
                    {
                        "asset_code": code,
                        "asset_type": str(receipt.get("category") or "Equipment")[:64],
                        "model": str(receipt.get("item") or "")[:200],
                        "serial_no": serial,
                        "purchase_date": receipt.get("received_on"),
                        "vendor": receipt.get("vendor"),
                        "cost": receipt.get("unit_cost"),
                        "source_ref": f"procureflow:{source}",
                    },
                )
            except products.ProductUnavailableError as exc:
                if made:
                    await session.commit()
                raise HTTPException(status_code=exc.status or 502, detail=str(exc)) from exc
            asset_ref = str((answer.get("asset") or {}).get("id") or "")
            asset_code = str((answer.get("asset") or {}).get("code") or code)
        session.add(
            ProcuredAsset(
                tenant_id=user.tenant_id,
                source_ref=source,
                register=register,
                asset_ref=asset_ref[:64],
                asset_code=asset_code[:64],
                added_by_user_id=user.id,
            )
        )
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Someone added this delivery at the same moment. Refresh the list.",
            ) from exc
        made.append(asset_code)
    await session.commit()
    return {"added": made, "register": register}
