"""Organisation structure endpoints: legal entities, locations, departments.

Read paths are implemented; writes land with Core HR (V1 build order step 2).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.core.deps import CurrentUser, DbSession, tenant_scoped
from app.models.org import Department, LegalEntity, Location

router = APIRouter(prefix="/org", tags=["core-hr"])


class LegalEntityRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    code: str
    pan: str | None
    tan: str | None
    is_active: bool


class LocationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    legal_entity_id: uuid.UUID
    name: str
    code: str
    city: str | None
    state_code: str
    timezone: str
    is_active: bool


class DepartmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    code: str
    parent_id: uuid.UUID | None
    head_employee_id: uuid.UUID | None
    is_active: bool


@router.get("/legal-entities", response_model=list[LegalEntityRead])
async def list_legal_entities(user: CurrentUser, session: DbSession) -> list[LegalEntityRead]:
    stmt = tenant_scoped(select(LegalEntity).order_by(LegalEntity.name), LegalEntity, user)
    rows = (await session.scalars(stmt)).all()
    return [LegalEntityRead.model_validate(row) for row in rows]


@router.get("/locations", response_model=list[LocationRead])
async def list_locations(user: CurrentUser, session: DbSession) -> list[LocationRead]:
    """Locations carry `state_code`, which drives PT slabs and holiday calendars."""
    stmt = tenant_scoped(select(Location).order_by(Location.name), Location, user)
    rows = (await session.scalars(stmt)).all()
    return [LocationRead.model_validate(row) for row in rows]


@router.get("/departments", response_model=list[DepartmentRead])
async def list_departments(user: CurrentUser, session: DbSession) -> list[DepartmentRead]:
    """Flat list. The client assembles the tree from `parent_id`."""
    stmt = tenant_scoped(select(Department).order_by(Department.name), Department, user)
    rows = (await session.scalars(stmt)).all()
    return [DepartmentRead.model_validate(row) for row in rows]
