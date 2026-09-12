"""Culture layer endpoints: shoutouts and clubs (FRD 5.3, FS-CUL-01)."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, or_, select

from app.core import permissions
from app.core.deps import CurrentUser, DbSession, tenant_scoped
from app.models.audit import AuditAction
from app.models.culture import Club, Shoutout, ShoutoutVisibility
from app.models.employee import Employee
from app.schemas.common import Page
from app.services import audit

router = APIRouter(prefix="/culture", tags=["culture"])


class ShoutoutCreate(BaseModel):
    to_employee_id: uuid.UUID
    message: str = Field(min_length=1, max_length=2000)
    value_tag: str | None = Field(default=None, max_length=64)
    visibility: ShoutoutVisibility = ShoutoutVisibility.COMPANY


class ShoutoutRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    from_employee_id: uuid.UUID
    to_employee_id: uuid.UUID
    message: str
    value_tag: str | None
    visibility: ShoutoutVisibility
    is_pinned: bool
    points_awarded: int
    created_at: datetime


class ClubRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    description: str | None
    icon: str | None
    is_private: bool


@router.get("/shoutouts", response_model=Page[ShoutoutRead])
async def list_shoutouts(
    user: CurrentUser,
    session: DbSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> Page[ShoutoutRead]:
    """The home feed. Pinned first, then newest."""
    stmt = tenant_scoped(select(Shoutout), Shoutout, user).order_by(
        Shoutout.is_pinned.desc(), Shoutout.created_at.desc()
    )
    stmt = stmt.where(await _visible_shoutouts(session, user))
    rows = (
        await session.scalars(stmt.offset((page - 1) * page_size).limit(page_size))
    ).all()
    return Page[ShoutoutRead](
        items=[ShoutoutRead.model_validate(row) for row in rows],
        total=len(rows),
        page=page,
        page_size=page_size,
    )


@router.post("/shoutouts", response_model=ShoutoutRead, status_code=status.HTTP_201_CREATED)
async def create_shoutout(
    payload: ShoutoutCreate, user: CurrentUser, session: DbSession, request: Request
) -> ShoutoutRead:
    """Post a shoutout.

    The sender is the caller's own employee record — it is never taken from the
    request body, so nobody can post recognition in someone else's name.
    """
    if user.employee_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only users linked to an employee record can post a shoutout",
        )
    if payload.to_employee_id == user.employee_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="You cannot give yourself a shoutout",
        )

    shoutout = Shoutout(
        tenant_id=user.tenant_id,
        from_employee_id=user.employee_id,
        **payload.model_dump(),
    )
    session.add(shoutout)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="shoutout",
        entity_id=shoutout.id,
        actor=user,
        summary="Posted a shoutout",
        request=request,
    )
    await session.commit()
    await session.refresh(shoutout)
    return ShoutoutRead.model_validate(shoutout)


@router.get("/clubs", response_model=list[ClubRead])
async def list_clubs(user: CurrentUser, session: DbSession) -> list[ClubRead]:
    stmt = tenant_scoped(
        select(Club).where(Club.is_active.is_(True)).order_by(Club.name), Club, user
    )
    rows = (await session.scalars(stmt)).all()
    return [ClubRead.model_validate(row) for row in rows]


# TODO(V1 step 5): reactions, club join/leave, pinning.
# TODO(V2): pulse surveys and Your Voice. Your Voice needs a schema review with
# the compliance SME first — FS-CUL-02 requires that a submitter's identity is
# never stored anywhere a case handler can retrieve it, which is a data-model
# decision, not an access-control one.


async def _visible_shoutouts(session: DbSession, user: CurrentUser):
    """Which shoutouts this person may see.

    COMPANY is everybody's. DEPARTMENT is the recipient's department, the two
    people in it, and HR. PRIVATE is the two people in it and nobody else.
    """
    me = user.employee_id
    conditions = [Shoutout.visibility == ShoutoutVisibility.COMPANY]
    if me is not None:
        conditions += [Shoutout.from_employee_id == me, Shoutout.to_employee_id == me]
    if permissions.is_hr(user):
        conditions.append(Shoutout.visibility == ShoutoutVisibility.DEPARTMENT)
    elif me is not None:
        department = await session.scalar(
            tenant_scoped(
                select(Employee.department_id).where(Employee.id == me), Employee, user
            )
        )
        if department is not None:
            colleagues = tenant_scoped(
                select(Employee.id).where(Employee.department_id == department),
                Employee,
                user,
            )
            conditions.append(
                and_(
                    Shoutout.visibility == ShoutoutVisibility.DEPARTMENT,
                    Shoutout.to_employee_id.in_(colleagues),
                )
            )
    return or_(*conditions)
