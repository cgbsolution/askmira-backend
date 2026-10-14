"""Organisation structure endpoints: legal entities, locations, departments.

Reads are reference material for every signed-in person. Writes are admin-only
and audited; nothing is deleted — a closed office is deactivated.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, status

from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.user import UserRole
from app.schemas.custom_fields import CustomFieldCreate, CustomFieldRead, CustomFieldUpdate
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
from app.services import custom_fields, custom_tables
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


# ── Custom fields on the employee record ────────────────────────────────────


@router.get("/custom-fields", response_model=list[CustomFieldRead])
async def list_custom_fields(
    user: CurrentUser, session: DbSession, include_inactive: bool = False
) -> list[CustomFieldRead]:
    """The fields the profile draws. Sensitive ones are listed for HR only."""
    rows = await custom_fields.list_definitions(
        session, user, include_inactive=include_inactive
    )
    return [CustomFieldRead.model_validate(row) for row in rows]


@router.post(
    "/custom-fields",
    response_model=CustomFieldRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=ADMIN_ONLY,
)
async def create_custom_field(
    payload: CustomFieldCreate, user: CurrentUser, session: DbSession, request: Request
) -> CustomFieldRead:
    row = await custom_fields.create_definition(session, user, payload, request=request)
    return CustomFieldRead.model_validate(row)


@router.patch(
    "/custom-fields/{definition_id}", response_model=CustomFieldRead, dependencies=ADMIN_ONLY
)
async def update_custom_field(
    definition_id: uuid.UUID,
    payload: CustomFieldUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> CustomFieldRead:
    """Label, options, required, sensitive, order, active. Never the key or type."""
    row = await custom_fields.update_definition(
        session, user, definition_id, payload, request=request
    )
    return CustomFieldRead.model_validate(row)



# ── Custom tables on the employee record ────────────────────────────────────


@router.get("/custom-tables")
async def list_custom_tables(
    user: CurrentUser, session: DbSession, include_inactive: bool = False
) -> list[dict]:
    """Table designs the profile draws. Sensitive ones are listed for HR only."""
    return await custom_tables.list_tables(session, user, include_inactive=include_inactive)


@router.post("/custom-tables", status_code=status.HTTP_201_CREATED, dependencies=ADMIN_ONLY)
async def create_custom_table(
    payload: custom_tables.TableIn, user: CurrentUser, session: DbSession, request: Request
) -> dict:
    return await custom_tables.create_table(session, user, payload, request=request)


@router.patch("/custom-tables/{table_id}", dependencies=ADMIN_ONLY)
async def update_custom_table(
    table_id: uuid.UUID,
    payload: custom_tables.TableUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict:
    return await custom_tables.update_table(session, user, table_id, payload, request=request)
