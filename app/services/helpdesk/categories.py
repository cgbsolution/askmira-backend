"""Helpdesk categories: what employees can ask about, who owns it, and its SLA.

Everyone reads the active list — it is the menu on the "Raise a ticket" form.
Admins manage it. A category that has tickets is deactivated
rather than deleted, so an old ticket never loses the label it was raised under.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.helpdesk import HelpdeskCategory, HelpdeskTicket
from app.models.user import User, UserRole
from app.schemas.helpdesk import (
    CategoryCreate,
    CategoryRead,
    CategoryUpdate,
    StandardCategoriesResult,
    StandardCategoryRead,
)
from app.services import audit
from app.services.helpdesk import rules

__all__ = [
    "add_standard_categories",
    "create_category",
    "delete_category",
    "list_categories",
    "standard_categories",
    "update_category",
]

_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")


async def _category(session: AsyncSession, user: User, category_id: uuid.UUID) -> HelpdeskCategory:
    row = await session.scalar(
        tenant_scoped(
            select(HelpdeskCategory).where(HelpdeskCategory.id == category_id),
            HelpdeskCategory,
            user,
        )
    )
    if row is None:
        raise _NOT_FOUND
    return row


async def _owner_names(
    session: AsyncSession, user: User, ids: set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = await session.execute(
        tenant_scoped(select(User.id, User.full_name).where(User.id.in_(ids)), User, user)
    )
    return {row.id: row.full_name for row in rows}


async def _ticket_counts(
    session: AsyncSession, user: User, ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    if not ids:
        return {}
    rows = await session.execute(
        tenant_scoped(
            select(HelpdeskTicket.category_id, func.count())
            .where(HelpdeskTicket.category_id.in_(ids))
            .group_by(HelpdeskTicket.category_id),
            HelpdeskTicket,
            user,
        )
    )
    return {category_id: count for category_id, count in rows}


async def _reads(
    session: AsyncSession, user: User, categories: list[HelpdeskCategory]
) -> list[CategoryRead]:
    names = await _owner_names(
        session, user, {c.owner_user_id for c in categories if c.owner_user_id}
    )
    # Ticket volumes are HR's business. An employee reading the menu does not
    # learn how many grievances were raised last quarter.
    counts = (
        await _ticket_counts(session, user, [c.id for c in categories])
        if permissions.is_hr(user)
        else None
    )
    return [
        CategoryRead(
            id=c.id,
            name=c.name,
            description=c.description,
            owner_user_id=c.owner_user_id,
            owner_name=names.get(c.owner_user_id) if c.owner_user_id else None,
            sla_hours=c.sla_hours,
            is_confidential=c.is_confidential,
            request_kind=c.request_kind,
            active=c.active,
            sort_order=c.sort_order,
            ticket_count=counts.get(c.id, 0) if counts is not None else None,
        )
        for c in categories
    ]


async def _check_owner(session: AsyncSession, user: User, owner_user_id: uuid.UUID) -> User:
    owner = await session.scalar(
        tenant_scoped(select(User).where(User.id == owner_user_id), User, user)
    )
    if owner is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="That person was not found."
        )
    if not owner.is_active:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{owner.full_name}'s login is deactivated. Pick someone who can sign in.",
        )
    if owner.role is UserRole.SUPER_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Pick somebody in your organisation to own this category.",
        )
    return owner


async def _refuse_duplicate_name(
    session: AsyncSession, user: User, name: str, *, except_id: uuid.UUID | None = None
) -> None:
    stmt = tenant_scoped(
        select(HelpdeskCategory.id).where(func.lower(HelpdeskCategory.name) == name.lower()),
        HelpdeskCategory,
        user,
    )
    if except_id is not None:
        stmt = stmt.where(HelpdeskCategory.id != except_id)
    if await session.scalar(stmt) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A category called '{name}' already exists.",
        )


# ═══════════════════════════════════════════════════════════════════════════
# Reads
# ═══════════════════════════════════════════════════════════════════════════


async def list_categories(
    session: AsyncSession, user: User, *, include_inactive: bool = False
) -> list[CategoryRead]:
    """The active categories, in the admin's order. Inactive ones only for HR."""
    if include_inactive and not permissions.is_hr(user):
        raise permissions.forbidden("Only HR can see deactivated categories.")
    stmt = tenant_scoped(
        select(HelpdeskCategory).order_by(HelpdeskCategory.sort_order, HelpdeskCategory.name),
        HelpdeskCategory,
        user,
    )
    if not include_inactive:
        stmt = stmt.where(HelpdeskCategory.active.is_(True))
    categories = list((await session.scalars(stmt)).all())
    return await _reads(session, user, categories)


# ═══════════════════════════════════════════════════════════════════════════
# Mutations (admins; the router gates the role)
# ═══════════════════════════════════════════════════════════════════════════


async def create_category(
    session: AsyncSession,
    user: User,
    payload: CategoryCreate,
    *,
    request: Request | None = None,
) -> CategoryRead:
    await _refuse_duplicate_name(session, user, payload.name)
    if payload.owner_user_id is not None:
        await _check_owner(session, user, payload.owner_user_id)

    category = HelpdeskCategory(tenant_id=user.tenant_id, **payload.model_dump())
    session.add(category)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="helpdesk_category",
        entity_id=category.id,
        actor=user,
        changes={
            "name": category.name,
            "owner_user_id": str(category.owner_user_id) if category.owner_user_id else None,
            "sla_hours": category.sla_hours,
            "is_confidential": category.is_confidential,
            "request_kind": category.request_kind.value,
        },
        summary=f"Created helpdesk category '{category.name}'",
        request=request,
    )
    await session.commit()
    await session.refresh(category)
    return (await _reads(session, user, [category]))[0]


async def update_category(
    session: AsyncSession,
    user: User,
    category_id: uuid.UUID,
    payload: CategoryUpdate,
    *,
    request: Request | None = None,
) -> CategoryRead:
    """Change a category. Confidentiality and SLA apply to tickets raised from
    now on; a ticket already raised keeps what it was raised under."""
    category = await _category(session, user, category_id)
    changes = payload.model_dump(exclude_unset=True)
    for key in ("name", "sla_hours", "is_confidential", "request_kind", "active", "sort_order"):
        if key in changes and changes[key] is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"'{key}' cannot be cleared.",
            )
    if "name" in changes:
        changes["name"] = changes["name"].strip()
        if not changes["name"]:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="A category needs a name.",
            )
        await _refuse_duplicate_name(session, user, changes["name"], except_id=category.id)
    if "description" in changes and changes["description"] is not None:
        changes["description"] = changes["description"].strip() or None
    new_owner = changes.get("owner_user_id")
    if new_owner is not None and new_owner != category.owner_user_id:
        await _check_owner(session, user, new_owner)

    diff: dict[str, dict[str, str | None]] = {}
    for key, value in changes.items():
        before = getattr(category, key)
        if before != value:
            diff[key] = {
                "from": None if before is None else str(getattr(before, "value", before)),
                "to": None if value is None else str(getattr(value, "value", value)),
            }
            setattr(category, key, value)

    if diff:
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.UPDATE,
            entity_type="helpdesk_category",
            entity_id=category.id,
            actor=user,
            changes=diff,
            summary=f"Updated helpdesk category '{category.name}'",
            request=request,
        )
        await session.commit()
        await session.refresh(category)
    return (await _reads(session, user, [category]))[0]


async def delete_category(
    session: AsyncSession,
    user: User,
    category_id: uuid.UUID,
    *,
    request: Request | None = None,
) -> None:
    """Remove a category nobody has raised a ticket in. One with tickets is
    refused: deactivate it instead, so its tickets keep their label."""
    category = await _category(session, user, category_id)
    used = await session.scalar(
        tenant_scoped(
            select(func.count())
            .select_from(HelpdeskTicket)
            .where(HelpdeskTicket.category_id == category.id),
            HelpdeskTicket,
            user,
        )
    )
    if used:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"'{category.name}' has {used} ticket{'s' if used != 1 else ''}. "
                "Deactivate it instead, so they keep their category."
            ),
        )
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.DELETE,
        entity_type="helpdesk_category",
        entity_id=category.id,
        actor=user,
        changes={"name": category.name},
        summary=f"Deleted helpdesk category '{category.name}'",
        request=request,
    )
    await session.delete(category)
    await session.commit()


def standard_categories() -> list[StandardCategoryRead]:
    """What "Add the standard categories" would add, before anybody adds it."""
    return [
        StandardCategoryRead(
            name=spec.name,
            description=spec.description,
            request_kind=spec.request_kind,
            sla_hours=spec.sla_hours,
            is_confidential=spec.is_confidential,
        )
        for spec in rules.STANDARD_CATEGORIES
    ]


async def add_standard_categories(
    session: AsyncSession, user: User, *, request: Request | None = None
) -> StandardCategoriesResult:
    """Add the usual Indian-HR set, skipping any name the tenant already has.

    Only ever on an admin's click. Nothing seeds these automatically: a
    workspace that has not set up its helpdesk shows an honest empty state,
    not categories nobody chose.
    """
    existing = {
        name.lower()
        for name in (
            await session.scalars(
                tenant_scoped(select(HelpdeskCategory.name), HelpdeskCategory, user)
            )
        ).all()
    }
    next_order = (
        await session.scalar(
            tenant_scoped(
                select(func.coalesce(func.max(HelpdeskCategory.sort_order), 0)),
                HelpdeskCategory,
                user,
            )
        )
    ) or 0

    created: list[HelpdeskCategory] = []
    skipped: list[str] = []
    for spec in rules.STANDARD_CATEGORIES:
        if spec.name.lower() in existing:
            skipped.append(spec.name)
            continue
        next_order += 10
        category = HelpdeskCategory(
            tenant_id=user.tenant_id,
            name=spec.name,
            description=spec.description,
            request_kind=spec.request_kind,
            sla_hours=spec.sla_hours,
            is_confidential=spec.is_confidential,
            active=True,
            sort_order=next_order,
        )
        session.add(category)
        created.append(category)

    if created:
        await session.flush()
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.CREATE,
            entity_type="helpdesk_category",
            actor=user,
            changes={"created": [c.name for c in created], "skipped": skipped},
            summary=f"Added {len(created)} standard helpdesk categories",
            request=request,
        )
        await session.commit()
        for category in created:
            await session.refresh(category)
    return StandardCategoriesResult(created=await _reads(session, user, created), skipped=skipped)
