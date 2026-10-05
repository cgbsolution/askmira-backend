"""Organisation structure endpoints: legal entities, locations, departments.

Reads are reference material for every signed-in person. Writes are admin-only
and audited; nothing is deleted — a closed office is deactivated.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, status

from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.user import UserRole
from app.schemas.org import (
    DepartmentCreate,
    DepartmentRead,
    DepartmentUpdate,
    LegalEntityCreate,
    LegalEntityRead,
    LegalEntityUpdate,
    LocationCreate,
    LocationRead,
    LocationUpdate,
)
from app.services import org as org_service

router = APIRouter(prefix="/org", tags=["core-hr"])

ADMIN_ONLY = [Depends(require_roles(UserRole.ADMIN))]


# ── Legal entities ──────────────────────────────────────────────────────────


@router.get("/legal-entities", response_model=list[LegalEntityRead])
async def list_legal_entities(user: CurrentUser, session: DbSession) -> list[LegalEntityRead]:
    rows = await org_service.list_legal_entities(session, user)
    return [LegalEntityRead.model_validate(row) for row in rows]


@router.post(
    "/legal-entities",
    response_model=LegalEntityRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=ADMIN_ONLY,
)
async def create_legal_entity(
    payload: LegalEntityCreate, user: CurrentUser, session: DbSession, request: Request
) -> LegalEntityRead:
    row = await org_service.create_legal_entity(session, user, payload, request=request)
    return LegalEntityRead.model_validate(row)


@router.patch(
    "/legal-entities/{entity_id}", response_model=LegalEntityRead, dependencies=ADMIN_ONLY
)
async def update_legal_entity(
    entity_id: uuid.UUID,
    payload: LegalEntityUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> LegalEntityRead:
    row = await org_service.update_legal_entity(session, user, entity_id, payload, request=request)
    return LegalEntityRead.model_validate(row)


# ── Locations ───────────────────────────────────────────────────────────────


@router.get("/locations", response_model=list[LocationRead])
async def list_locations(user: CurrentUser, session: DbSession) -> list[LocationRead]:
    """Locations carry `state_code`, which drives PT slabs and holiday calendars."""
    rows = await org_service.list_locations(session, user)
    return [LocationRead.model_validate(row) for row in rows]


@router.post(
    "/locations",
    response_model=LocationRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=ADMIN_ONLY,
)
async def create_location(
    payload: LocationCreate, user: CurrentUser, session: DbSession, request: Request
) -> LocationRead:
    row = await org_service.create_location(session, user, payload, request=request)
    return LocationRead.model_validate(row)


@router.patch("/locations/{location_id}", response_model=LocationRead, dependencies=ADMIN_ONLY)
async def update_location(
    location_id: uuid.UUID,
    payload: LocationUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> LocationRead:
    row = await org_service.update_location(session, user, location_id, payload, request=request)
    return LocationRead.model_validate(row)


# ── Departments ─────────────────────────────────────────────────────────────


@router.get("/departments", response_model=list[DepartmentRead])
async def list_departments(user: CurrentUser, session: DbSession) -> list[DepartmentRead]:
    """Flat list. The client assembles the tree from `parent_id`."""
    rows = await org_service.list_departments(session, user)
    return [DepartmentRead.model_validate(row) for row in rows]


@router.post(
    "/departments",
    response_model=DepartmentRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=ADMIN_ONLY,
)
async def create_department(
    payload: DepartmentCreate, user: CurrentUser, session: DbSession, request: Request
) -> DepartmentRead:
    row = await org_service.create_department(session, user, payload, request=request)
    return DepartmentRead.model_validate(row)


@router.patch(
    "/departments/{department_id}", response_model=DepartmentRead, dependencies=ADMIN_ONLY
)
async def update_department(
    department_id: uuid.UUID,
    payload: DepartmentUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> DepartmentRead:
    row = await org_service.update_department(
        session, user, department_id, payload, request=request
    )
    return DepartmentRead.model_validate(row)
