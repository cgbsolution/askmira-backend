"""Products not ready for a person yet, and how to get them ready from the chat.

Ask Miraa works in the products a workspace connects (catalogue.PRODUCTS). A tool
for a product that is not ready for the person (not connected, switched off,
connected but they are not a member there) is not offered, so on its own Miraa
could only say "I can't". Instead every message tells the model which products
are not ready and why, and `connect_product` shows the person what to do, on a
card: an admin gets a Connect button that starts Settings → Connectors' own
flow and comes back to the same conversation; anyone else is told who can.

Nothing here connects anything, and no product is asked: the state is read from
the workspace's own links.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.crypto import can_encrypt
from app.core.deps import tenant_scoped
from app.models.product_link import ProductLink
from app.models.user import User, UserRole
from app.services.products import data as products_data
from app.services.products.catalogue import PRODUCTS
from app.services.products.connections import endpoint
from app.services.products.service import ACTIVE
from app.services.products.settings import addresses

__all__ = ["READY", "connect_card", "not_ready", "not_ready_line", "refresh_cards", "state_of"]

READY = "ready"
NOT_CONNECTED = "not_connected"
SWITCHED_OFF = "switched_off"
NOT_MEMBER = "not_member"
NO_ROLE = "no_role"
NOT_ON_SERVER = "not_on_server"
OPERATOR = "operator"

#: What each product holds, as the person would ask for it.
HOLDS = {
    "interview": "jobs, candidates and interviews",
    "expense": "expense claims and their approvals",
    "helpdesk": "IT tickets and the assets people hold",
    "saleinx": "leads, deals and the sales pipeline",
    "procurement": "requisitions, purchase orders, invoices and budgets",
    "finance": "the books: accounts, journals and financial reports",
}
#: What Ask Miraa can do there once it is ready.
CAN_DO = {
    "interview": "show jobs, candidates and interviews",
    "expense": "show claims, totals and what waits for approval",
    "helpdesk": "raise IT tickets and follow them",
    "saleinx": "add leads and move deals",
    "procurement": "show requisitions and orders and decide approvals",
    "finance": "open the books for you",
}
_WHY = {
    NOT_CONNECTED: "not connected to this workspace",
    SWITCHED_OFF: "linked but switched off for this workspace",
    NOT_MEMBER: "connected, but the person is not a member there",
    NO_ROLE: "not part of the person's role",
    NOT_ON_SERVER: "not available on this AskMiraa server",
    OPERATOR: "not open to the platform operator",
}


async def _link(session: AsyncSession, user: User, key: str) -> ProductLink | None:
    return await session.scalar(
        tenant_scoped(select(ProductLink), ProductLink, user).where(ProductLink.product == key)
    )


async def state_of(session: AsyncSession, user: User, key: str) -> str:
    """Where the product stands for this person, as one of the states above."""
    product = PRODUCTS[key]
    if await products_data.role_in(session, user, key):
        return READY
    if getattr(user, "operator_grant_id", None):
        return OPERATOR
    row = await _link(session, user, key)
    if row is not None and row.status != ACTIVE:
        return SWITCHED_OFF
    if row is None or await endpoint(session, key, user.tenant_id) is None:
        # Not connected (or its key can't be read any more: connecting again mends it).
        standard = addresses(key)
        known = standard is not None or bool(row and row.api_url and row.app_url)
        return NOT_CONNECTED if known and can_encrypt() else NOT_ON_SERVER
    return NOT_MEMBER if product.roles_from_product else NO_ROLE


async def not_ready(session: AsyncSession, user: User) -> dict[str, str]:
    """Each product that is not ready for this person, and its state."""
    out: dict[str, str] = {}
    for key in PRODUCTS:
        state = await state_of(session, user, key)
        if state != READY:
            out[key] = state
    return out


async def not_ready_line(session: AsyncSession, user: User) -> str:
    """For the model with each message: what is not ready for this person, and why."""
    missing = await not_ready(session, user)
    if not missing:
        return ""
    parts = [
        f"{PRODUCTS[key].brand} ({HOLDS[key]}): {_WHY[state]}" for key, state in missing.items()
    ]
    return (
        "Products not ready for this person: "
        + "; ".join(parts)
        + ". When they ask for something there, call connect_product."
    )


async def _admins(session: AsyncSession, user: User) -> list[str]:
    names = await session.scalars(
        tenant_scoped(
            select(User.full_name)
            .where(User.role == UserRole.ADMIN, User.is_active.is_(True))
            .order_by(User.full_name)
            .limit(3),
            User,
            user,
        )
    )
    return [name for name in names if name]


def _names(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " or " + names[-1]


async def refresh_cards(
    session: AsyncSession, user: User, cards: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Saved connect cards as they stand now: one whose product has since become ready
    says so, without its buttons, so an old card never offers to connect it again."""
    if not any(card.get("type") == "connect" for card in cards):
        return cards
    states: dict[str, str] = {}
    out: list[dict[str, Any]] = []
    for card in cards:
        key = str(card.get("product") or "")
        if card.get("type") != "connect" or card.get("state") == READY or key not in PRODUCTS:
            out.append(card)
            continue
        if key not in states:
            states[key] = await state_of(session, user, key)
        if states[key] != READY:
            out.append(card)
            continue
        brand = PRODUCTS[key].brand
        out.append(
            {
                **card,
                "state": READY,
                "title": f"{brand} is connected",
                "text": f"{brand} is connected now and you can use it here.",
                "action": None,
            }
        )
    return out


async def connect_card(session: AsyncSession, user: User, key: str) -> tuple[list[dict], dict]:
    """The card that says what is missing for this product and what to do about it."""
    if key not in PRODUCTS:
        names = ", ".join(product.brand for product in PRODUCTS.values())
        return [
            {
                "type": "notice",
                "tone": "info",
                "text": f"Which product do you mean? Ask Miraa works with {names}.",
            }
        ], {}
    product = PRODUCTS[key]
    brand = product.brand
    state = await state_of(session, user, key)
    admin = permissions.is_admin(user) and not getattr(user, "operator_grant_id", None)
    admins = [] if admin else await _admins(session, user)
    ask = (
        f"Ask your admin ({_names(admins)}) to connect it under Settings → Connectors."
        if admins
        else "Ask your workspace's admin to connect it under Settings → Connectors."
    )
    action: str | None = None
    if state == READY:
        title = f"{brand} is ready"
        text = f"{brand} is connected and you can use it here. Ask me to {CAN_DO[key]}."
    elif state == NOT_CONNECTED:
        title = f"{brand} isn't connected yet"
        if admin:
            action = "connect"
            text = (
                f"Connect {brand} once and I can {CAN_DO[key]} for you, right here. You sign in to "
                f"{brand} as an admin of your organisation there and press Allow; then you come "
                "straight back to this chat and we carry on."
            )
        else:
            text = f"Your workspace hasn't connected {brand} yet, so I can't {CAN_DO[key]}. {ask}"
    elif state == SWITCHED_OFF:
        title = f"{brand} is switched off"
        if admin:
            action = "connect"
            text = f"{brand} is linked but switched off here. Connect it again to turn it back on."
        else:
            text = f"{brand} is switched off for your workspace. {ask}"
    elif state == NOT_MEMBER:
        title = f"You aren't a member in {brand} yet"
        text = (
            f"Your workspace is connected to {brand}, but it has no account for {user.email}. "
            f"{brand}'s own admins decide who is a member and their role: ask one of them to "
            f"invite {user.email}."
        )
        if admin:
            action = "sync"
            text += f" Once they have, press Sync people and I can {CAN_DO[key]} for you."
        else:
            text += " Once they have, it works here after the next sync."
    elif state == NO_ROLE:
        title = f"{brand} isn't part of your role"
        text = (
            f"{brand} is for HR and for managers with a team. Ask HR if you need a position opened."
            if key == "interview"
            else f"Your role here doesn't include {brand}. {ask}"
        )
    elif state == OPERATOR:
        title = f"{brand} isn't open to you here"
        text = (
            "You are in this workspace as the platform operator, so its connected products are "
            "not open to you."
        )
    else:
        title = f"{brand} can't be connected on this server yet"
        text = (
            f"This AskMiraa server doesn't know where {brand} is yet, or can't keep its key "
            "safely. Ask your AskMiraa provider to set it up."
        )

    view = f"Card shown to the person, under your reply: {title}. "
    if state == READY:
        view += "It is ready: use its tools."
    elif action == "connect":
        view += (
            "The person is an admin: the card has a Connect button, which brings them back to "
            "this conversation once it is connected. Tell them in a sentence or two (the card "
            "is below your words), and say you will carry on with their request then (keep "
            "what you prepared)."
        )
    elif action == "sync":
        view += (
            f"The person is an admin, but not a member in {brand} as {user.email}. A {brand} "
            "admin must invite them; then they press Sync people on the card."
        )
    elif state == NOT_MEMBER:
        view += (
            f"{brand} is connected, but the person ({user.email}) has no account in it: one of "
            f"{brand}'s own admins must invite them. Say it is their account that is missing "
            "(you act as them); the card below your words says what to do."
        )
    else:
        view += (
            "Tell them plainly what is missing and who can fix it; the card below your words "
            "names who to ask."
        )
    card: dict[str, Any] = {
        "type": "connect",
        "product": key,
        "brand": brand,
        "state": state,
        "title": title,
        "text": text,
        "action": action,
        "model_view": view,
    }
    return [card], {"product": key}
