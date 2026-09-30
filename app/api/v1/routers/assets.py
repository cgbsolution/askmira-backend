"""Asset management: the register, hand-overs, returns and requests.

Thin: parse, gate, delegate, serialise. Who may do what is decided in
`app.services.assets.service` through `app.core.permissions`:

- Everybody: their own assets (`/assets/me`), acknowledging receipt, the active
  categories, and asking for — or withdrawing — an asset request.
- Line managers and HR: reviewing requests (`scope=review`) and deciding them;
  never their own.
- HR: the register, categories, hand-over, return, status changes, reminders,
  the summary figures and the CSV export.

Static paths are declared before `/assets/{asset_id}` so that `me`, `summary`
and the rest are never parsed as an asset id.
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.assets import AssetRequestStatus, AssetStatus
from app.models.user import UserRole
from app.schemas.assets import (
    AssetCategoryCreate,
    AssetCategoryRead,
    AssetCategoryUpdate,
    AssetCreate,
    AssetDetailRead,
    AssetPage,
    AssetRead,
    AssetRequestCreate,
    AssetRequestRead,
    AssetStatusChange,
    AssetSummary,
    AssetUpdate,
    AssignIn,
    AssignmentRead,
    CustodianRead,
    DecisionIn,
    FulfilIn,
    MyAssetsRead,
    RejectIn,
    ReminderResult,
    ReturnIn,
)
from app.services.assets import service as assets

router = APIRouter(prefix="/assets", tags=["assets"])

HR_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN, UserRole.HR_OPS)
MANAGER_ROLES = (*HR_ROLES, UserRole.MANAGER)

_hr = [Depends(require_roles(*HR_ROLES))]
_managers = [Depends(require_roles(*MANAGER_ROLES))]


async def _asset_read(session, user, asset_id: uuid.UUID) -> AssetRead:
    return AssetRead(**(await assets.asset_detail(session, user, asset_id)))


async def _assignment_read(session, user, assignment) -> AssignmentRead:
    return AssignmentRead(**(await assets.assignment_reads(session, user, [assignment]))[0])


async def _request_read(session, user, asked) -> AssetRequestRead:
    return AssetRequestRead(**(await assets.request_reads(session, user, [asked]))[0])


# ── Mine ────────────────────────────────────────────────────────────────────


@router.get("/me", response_model=MyAssetsRead)
async def my_assets(user: CurrentUser, session: DbSession) -> MyAssetsRead:
    """What I hold now, and what I held before."""
    return MyAssetsRead(**(await assets.my_assets(session, user)))


@router.post("/assignments/{assignment_id}/acknowledge", response_model=AssignmentRead)
async def acknowledge(
    assignment_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> AssignmentRead:
    """The holder confirms receipt. Nobody can do it on their behalf."""
    row = await assets.acknowledge(session, user, assignment_id, request=request)
    await session.commit()
    return await _assignment_read(session, user, row)


# ── Categories ──────────────────────────────────────────────────────────────


@router.get("/categories", response_model=list[AssetCategoryRead])
async def list_categories(
    user: CurrentUser, session: DbSession, include_inactive: bool = False
) -> list[AssetCategoryRead]:
    """Active categories for anybody; HR may include the inactive ones."""
    rows = await assets.list_categories(session, user, include_inactive=include_inactive)
    return [AssetCategoryRead(**row) for row in rows]


@router.post(
    "/categories",
    response_model=AssetCategoryRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=_hr,
)
async def create_category(
    payload: AssetCategoryCreate, user: CurrentUser, session: DbSession, request: Request
) -> AssetCategoryRead:
    row = await assets.create_category(
        session,
        user,
        code=payload.code,
        name=payload.name,
        description=payload.description,
        custodian_user_id=payload.custodian_user_id,
        depreciation_months=payload.depreciation_months,
        requires_acknowledgement=payload.requires_acknowledgement,
        request=request,
    )
    await session.commit()
    return AssetCategoryRead(**(await assets.category_read(session, user, row.id)))


@router.patch("/categories/{category_id}", response_model=AssetCategoryRead, dependencies=_hr)
async def update_category(
    category_id: uuid.UUID,
    payload: AssetCategoryUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> AssetCategoryRead:
    row = await assets.update_category(
        session, user, category_id, payload.model_dump(exclude_unset=True), request=request
    )
    await session.commit()
    return AssetCategoryRead(**(await assets.category_read(session, user, row.id)))


@router.get("/custodians", response_model=list[CustodianRead], dependencies=_hr)
async def custodians(user: CurrentUser, session: DbSession) -> list[CustodianRead]:
    """Logins that may collect a category's assets at exit: HR and managers."""
    return [CustodianRead(**row) for row in await assets.custodian_candidates(session, user)]


# ── Requests ────────────────────────────────────────────────────────────────


@router.get("/requests", response_model=list[AssetRequestRead])
async def list_requests(
    user: CurrentUser,
    session: DbSession,
    scope: Literal["mine", "review"] = "mine",
    request_status: AssetRequestStatus | None = Query(default=None, alias="status"),
) -> list[AssetRequestRead]:
    """`mine` for anybody; `review` for line managers (their line) and HR (everybody)."""
    rows = await assets.list_requests(session, user, scope=scope, request_status=request_status)
    return [AssetRequestRead(**row) for row in rows]


@router.post("/requests", response_model=AssetRequestRead, status_code=status.HTTP_201_CREATED)
async def create_request(
    payload: AssetRequestCreate, user: CurrentUser, session: DbSession, request: Request
) -> AssetRequestRead:
    row = await assets.create_request(
        session,
        user,
        category_id=payload.category_id,
        reason=payload.reason,
        needed_by=payload.needed_by,
        request=request,
    )
    await session.commit()
    return await _request_read(session, user, row)


@router.post("/requests/{request_id}/cancel", response_model=AssetRequestRead)
async def cancel_request(
    request_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> AssetRequestRead:
    row = await assets.cancel_request(session, user, request_id, request=request)
    await session.commit()
    return await _request_read(session, user, row)


@router.post(
    "/requests/{request_id}/approve", response_model=AssetRequestRead, dependencies=_managers
)
async def approve_request(
    request_id: uuid.UUID,
    payload: DecisionIn,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> AssetRequestRead:
    row = await assets.decide_request(
        session, user, request_id, approve=True, note=payload.note, request=request
    )
    await session.commit()
    return await _request_read(session, user, row)


@router.post(
    "/requests/{request_id}/reject", response_model=AssetRequestRead, dependencies=_managers
)
async def reject_request(
    request_id: uuid.UUID,
    payload: RejectIn,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> AssetRequestRead:
    row = await assets.decide_request(
        session, user, request_id, approve=False, note=payload.note, request=request
    )
    await session.commit()
    return await _request_read(session, user, row)


@router.post("/requests/{request_id}/fulfil", response_model=AssetRequestRead, dependencies=_hr)
async def fulfil_request(
    request_id: uuid.UUID,
    payload: FulfilIn,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> AssetRequestRead:
    """Hand over an in-stock asset of the requested category; the request closes."""
    row = await assets.fulfil_request(
        session,
        user,
        request_id,
        asset_id=payload.asset_id,
        expected_return_date=payload.expected_return_date,
        condition_out=payload.condition_out,
        notes=payload.notes,
        request=request,
    )
    await session.commit()
    return await _request_read(session, user, row)


# ── Assignments ─────────────────────────────────────────────────────────────


@router.post(
    "/assignments/{assignment_id}/return", response_model=AssignmentRead, dependencies=_hr
)
async def return_asset(
    assignment_id: uuid.UUID,
    payload: ReturnIn,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> AssignmentRead:
    row = await assets.return_asset(
        session,
        user,
        assignment_id,
        condition_in=payload.condition_in,
        notes=payload.notes,
        recovery_amount=payload.recovery_amount,
        next_status=AssetStatus(payload.next_status),
        request=request,
    )
    await session.commit()
    return await _assignment_read(session, user, row)


@router.post(
    "/assignments/{assignment_id}/remind", response_model=ReminderResult, dependencies=_hr
)
async def remind(
    assignment_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> ReminderResult:
    sent = await assets.remind_return(session, user, assignment_id, request=request)
    await session.commit()
    return ReminderResult(reminded=sent)


@router.get(
    "/employees/{employee_id}", response_model=list[AssignmentRead], dependencies=_managers
)
async def employee_assets(
    employee_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[AssignmentRead]:
    """One person's assets: HR for anybody, a manager for their reporting line."""
    rows = await assets.employee_assets(session, user, employee_id)
    return [AssignmentRead(**row) for row in rows]


# ── The register ────────────────────────────────────────────────────────────


@router.get("/summary", response_model=AssetSummary, dependencies=_hr)
async def summary(user: CurrentUser, session: DbSession) -> AssetSummary:
    return AssetSummary(**(await assets.summary(session, user)))


@router.get("/export.csv", response_model=None, dependencies=_hr)
async def export_csv(
    user: CurrentUser,
    session: DbSession,
    request: Request,
    asset_status: AssetStatus | None = Query(default=None, alias="status"),
    category_id: uuid.UUID | None = None,
    location_id: uuid.UUID | None = None,
    q: str | None = Query(default=None, max_length=128),
    employee_id: uuid.UUID | None = None,
) -> Response:
    """The register as CSV, filtered like the screen. Cells cannot run as formulas."""
    content = await assets.export_csv(
        session,
        user,
        asset_status=asset_status,
        category_id=category_id,
        location_id=location_id,
        q=q,
        employee_id=employee_id,
        request=request,
    )
    await session.commit()
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="asset-register-{assets.today()}.csv"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("", response_model=AssetPage, dependencies=_hr)
async def list_assets(
    user: CurrentUser,
    session: DbSession,
    asset_status: AssetStatus | None = Query(default=None, alias="status"),
    category_id: uuid.UUID | None = None,
    location_id: uuid.UUID | None = None,
    q: str | None = Query(default=None, max_length=128),
    employee_id: uuid.UUID | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
) -> AssetPage:
    rows, total = await assets.list_assets(
        session,
        user,
        asset_status=asset_status,
        category_id=category_id,
        location_id=location_id,
        q=q,
        employee_id=employee_id,
        page=page,
        page_size=page_size,
    )
    return AssetPage(
        items=[AssetRead(**row) for row in rows], total=total, page=page, page_size=page_size
    )


@router.post("", response_model=AssetRead, status_code=status.HTTP_201_CREATED, dependencies=_hr)
async def create_asset(
    payload: AssetCreate, user: CurrentUser, session: DbSession, request: Request
) -> AssetRead:
    row = await assets.create_asset(session, user, payload.model_dump(), request=request)
    await session.commit()
    return await _asset_read(session, user, row.id)


@router.get("/{asset_id}", response_model=AssetDetailRead, dependencies=_hr)
async def get_asset(asset_id: uuid.UUID, user: CurrentUser, session: DbSession) -> AssetDetailRead:
    """The asset with every hand-over it has had, newest first."""
    return AssetDetailRead(**(await assets.asset_detail(session, user, asset_id)))


@router.patch("/{asset_id}", response_model=AssetRead, dependencies=_hr)
async def update_asset(
    asset_id: uuid.UUID,
    payload: AssetUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> AssetRead:
    row = await assets.update_asset(
        session, user, asset_id, payload.model_dump(exclude_unset=True), request=request
    )
    await session.commit()
    return await _asset_read(session, user, row.id)


@router.post("/{asset_id}/status", response_model=AssetRead, dependencies=_hr)
async def change_status(
    asset_id: uuid.UUID,
    payload: AssetStatusChange,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> AssetRead:
    """In for repair, lost, retired or disposed — each with its reason."""
    row = await assets.change_status(
        session,
        user,
        asset_id,
        target=AssetStatus(payload.status),
        reason=payload.reason,
        recovery_amount=payload.recovery_amount,
        request=request,
    )
    await session.commit()
    return await _asset_read(session, user, row.id)


@router.post("/{asset_id}/assign", response_model=AssignmentRead, dependencies=_hr)
async def assign(
    asset_id: uuid.UUID,
    payload: AssignIn,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> AssignmentRead:
    """Hand an in-stock asset to somebody; they are told and asked to acknowledge."""
    row = await assets.assign_asset(
        session,
        user,
        asset_id,
        employee_id=payload.employee_id,
        expected_return_date=payload.expected_return_date,
        condition_out=payload.condition_out,
        notes=payload.notes,
        request=request,
    )
    await session.commit()
    return await _assignment_read(session, user, row)
