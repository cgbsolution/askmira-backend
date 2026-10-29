"""Client accounts and their P&L: accounts, billing and costs, people mapping, the P&L.

Admins and sub-admins only, inside the payroll area (`/accounts` maps to it in
core/areas.py), so a sub-admin's payroll rights apply: view to read, create,
edit and delete to change. Every change writes its audit row in the same
transaction.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile, status

from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.user import UserRole
from app.schemas.accounts import (
    AccountEntryCreate,
    AccountEntryRead,
    AccountEntryUpdate,
    AccountImportRead,
    AccountPnlRead,
    AllocationCreate,
    AllocationEnd,
    AllocationRead,
    ClientAccountCreate,
    ClientAccountRead,
    ClientAccountUpdate,
    PnlDetailRead,
)
from app.services.accounts import service

router = APIRouter(
    prefix="/accounts",
    tags=["accounts"],
    dependencies=[Depends(require_roles(UserRole.ADMIN, UserRole.SUB_ADMIN))],
)

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# ── Accounts ─────────────────────────────────────────────────────────────────


@router.get("", response_model=list[ClientAccountRead])
async def list_accounts(
    user: CurrentUser, session: DbSession, include_inactive: bool = True
) -> list[ClientAccountRead]:
    return await service.list_accounts(session, user, include_inactive=include_inactive)


@router.post("", response_model=ClientAccountRead, status_code=status.HTTP_201_CREATED)
async def create_account(
    payload: ClientAccountCreate, user: CurrentUser, session: DbSession, request: Request
) -> ClientAccountRead:
    return await service.create_account(session, user, payload, request=request)


@router.patch("/{account_id}", response_model=ClientAccountRead)
async def update_account(
    account_id: uuid.UUID,
    payload: ClientAccountUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> ClientAccountRead:
    return await service.update_account(session, user, account_id, payload, request=request)


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_account(
    account_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> None:
    await service.delete_account(session, user, account_id, request=request)


# ── Billing and costs ───────────────────────────────────────────────────────


@router.get("/entries", response_model=list[AccountEntryRead])
async def list_entries(
    user: CurrentUser,
    session: DbSession,
    account_id: uuid.UUID | None = None,
    kind: str | None = None,
    start: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}$")] = None,
    end: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}$")] = None,
) -> list[AccountEntryRead]:
    return await service.list_entries(
        session, user, account_id=account_id, kind=kind, start=start, end=end
    )


@router.post("/entries", response_model=AccountEntryRead, status_code=status.HTTP_201_CREATED)
async def create_entry(
    payload: AccountEntryCreate, user: CurrentUser, session: DbSession, request: Request
) -> AccountEntryRead:
    return await service.create_entry(session, user, payload, request=request)


@router.patch("/entries/{entry_id}", response_model=AccountEntryRead)
async def update_entry(
    entry_id: uuid.UUID,
    payload: AccountEntryUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> AccountEntryRead:
    return await service.update_entry(session, user, entry_id, payload, request=request)


@router.delete("/entries/{entry_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_entry(
    entry_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> None:
    await service.delete_entry(session, user, entry_id, request=request)


@router.get("/entries/template")
async def entries_template(user: CurrentUser, session: DbSession) -> Response:
    data = await service.template(session, user)
    return Response(
        content=data,
        media_type=_XLSX,
        headers={"Content-Disposition": 'attachment; filename="account-billing-and-costs.xlsx"'},
    )


@router.post("/entries/import", response_model=AccountImportRead)
async def import_entries(
    file: Annotated[UploadFile, File()],
    user: CurrentUser,
    session: DbSession,
    request: Request,
    apply: Annotated[bool, Form()] = False,
) -> AccountImportRead:
    """Check a filled sheet row by row; with `apply`, save every row (none if any has an error)."""
    data = await file.read(service.MAX_UPLOAD_BYTES + 1)
    return await service.import_entries(
        session, user, data, file.filename or "billing.xlsx", apply=apply, request=request
    )


# ── People on accounts ──────────────────────────────────────────────────────


@router.get("/allocations", response_model=list[AllocationRead])
async def list_allocations(
    user: CurrentUser,
    session: DbSession,
    account_id: uuid.UUID | None = None,
    employee_id: uuid.UUID | None = None,
    current_only: bool = False,
) -> list[AllocationRead]:
    return await service.list_allocations(
        session, user, account_id=account_id, employee_id=employee_id, current_only=current_only
    )


@router.post("/allocations", response_model=AllocationRead, status_code=status.HTTP_201_CREATED)
async def create_allocation(
    payload: AllocationCreate, user: CurrentUser, session: DbSession, request: Request
) -> AllocationRead:
    return await service.create_allocation(session, user, payload, request=request)


@router.post("/allocations/{allocation_id}/end", response_model=AllocationRead)
async def end_allocation(
    allocation_id: uuid.UUID,
    payload: AllocationEnd,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> AllocationRead:
    return await service.end_allocation(session, user, allocation_id, payload, request=request)


@router.delete(
    "/allocations/{allocation_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None
)
async def delete_allocation(
    allocation_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> None:
    await service.delete_allocation(session, user, allocation_id, request=request)


# ── P&L ─────────────────────────────────────────────────────────────────────


@router.get("/pnl", response_model=AccountPnlRead)
async def pnl(
    user: CurrentUser,
    session: DbSession,
    start: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}$")] = None,
    end: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}$")] = None,
    include_inactive: bool = False,
) -> AccountPnlRead:
    return await service.pnl(session, user, start=start, end=end, include_inactive=include_inactive)


@router.get("/{account_id}/pnl", response_model=PnlDetailRead)
async def pnl_detail(
    account_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    start: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}$")] = None,
    end: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}$")] = None,
) -> PnlDetailRead:
    return await service.pnl_detail(session, user, account_id, start=start, end=end)
