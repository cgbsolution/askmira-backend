"""The report builder (slice 22).

The catalogue endpoint is what a builder UI is made of: it returns only the
entities and fields the caller's role may use, so the field picker cannot offer
something the compiler will refuse.

Note that every run compiles **as the caller**, including a saved report shared
by somebody more senior. A shared report is a shared question, not a shared
answer — running it as its author would turn every shared salary report into a
privilege-escalation route.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select

from app.core import permissions
from app.core.deps import CurrentUser, DbSession, require_roles, tenant_scoped
from app.models.reporting import (
    Cadence,
    ChartType,
    EmptyBehaviour,
    ExportFormat,
    Report,
    ReportDelivery,
    ReportRun,
    ReportSchedule,
    ReportVisibility,
)
from app.models.user import UserRole
from app.services.reporting import library, ranges
from app.services.reporting import service as report_service
from app.services.reporting.compiler import CompileError, Definition
from app.services.reporting.semantic import SemanticError, catalogue_for

router = APIRouter(tags=["reports"])

#: Reporting is open to everybody who can log in. What they can *see* is decided
#: by the catalogue and the row scope, not by the route — a role gate here would
#: be a second, weaker copy of a check that already exists.
ALL_ROLES = (UserRole.ADMIN, UserRole.SUB_ADMIN, UserRole.EMPLOYEE)
ADMIN_ROLES = (UserRole.ADMIN,)


# ── Contracts ───────────────────────────────────────────────────────────────


class SelectionIn(BaseModel):
    field_key: str = Field(min_length=1, max_length=64)
    aggregation: str | None = Field(default=None, max_length=32)
    label: str = Field(default="", max_length=128)


class FilterIn(BaseModel):
    field_key: str = Field(min_length=1, max_length=64)
    operator: str = Field(min_length=1, max_length=32)
    value: Any = None
    values: list[Any] = Field(default_factory=list)


class DefinitionIn(BaseModel):
    """A report definition, in catalogue keys only.

    There is no field here that can carry a table or column name. That is the
    point: an identifier can only enter the query by matching a catalogue key,
    and a key that does not exist is refused.
    """

    entity_key: str = Field(min_length=1, max_length=64)
    selections: list[SelectionIn] = Field(min_length=1)
    filters: list[FilterIn] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    sort_by: list[list[str]] = Field(default_factory=list)
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)

    def to_definition(self) -> Definition:
        return report_service.definition_from_json(self.model_dump())


class ReportCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str | None = None
    definition: DefinitionIn
    chart_type: ChartType = ChartType.TABLE
    visibility: ReportVisibility = ReportVisibility.PRIVATE
    shared_with_user_ids: list[uuid.UUID] = Field(default_factory=list)


class ReportRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    entity_key: str
    definition: dict
    chart_type: ChartType
    visibility: ReportVisibility
    owner_user_id: uuid.UUID | None
    created_from_starter: str | None
    last_run_at: datetime | None


class ScheduleCreate(BaseModel):
    report_id: uuid.UUID
    cadence: Cadence
    hour_of_day: int = Field(default=9, ge=0, le=23)
    day_of_week: int | None = Field(default=None, ge=0, le=6)
    day_of_month: int | None = Field(default=None, ge=1, le=31)
    timezone_name: str = Field(default="Asia/Kolkata", max_length=64)
    recipient_user_ids: list[uuid.UUID] = Field(min_length=1)
    export_format: ExportFormat = ExportFormat.CSV
    on_empty: EmptyBehaviour = EmptyBehaviour.SEND_ANYWAY


class CloneRequest(BaseModel):
    starter_key: str = Field(min_length=1, max_length=64)
    name: str | None = Field(default=None, max_length=160)


# ═══════════════════════════════════════════════════════════════════════════
# The catalogue
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/reports/catalogue", dependencies=[Depends(require_roles(*ALL_ROLES))])
async def catalogue(user: CurrentUser) -> dict[str, Any]:
    """What this role may build a report from.

    Filtered here so a field picker cannot offer something the compiler will
    refuse. The compiler checks again — this is a courtesy, that is the gate.
    """
    role = permissions.scope_role(user)
    return {
        "entities": catalogue_for(role),
        "relative_ranges": [
            {"key": key, "description": description}
            for key, description in ranges.RELATIVE_RANGES.items()
        ],
        "row_scope": report_service.compiler.resolve_scope(role).value,
    }


@router.get("/reports/starters", dependencies=[Depends(require_roles(*ALL_ROLES))])
async def starters(user: CurrentUser) -> list[dict[str, Any]]:
    """The starter library, filtered to what this role can actually run."""
    role = permissions.scope_role(user)
    return library.starters_for(role)


@router.post(
    "/reports/starters/clone",
    response_model=ReportRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*ALL_ROLES))],
)
async def clone_starter(
    payload: CloneRequest, user: CurrentUser, session: DbSession
) -> ReportRead:
    report = await report_service.clone_starter(
        session, user, starter_key=payload.starter_key, name=payload.name
    )
    await session.commit()
    await session.refresh(report)
    return ReportRead.model_validate(report)


# ═══════════════════════════════════════════════════════════════════════════
# Saved reports
# ═══════════════════════════════════════════════════════════════════════════


@router.post(
    "/reports",
    response_model=ReportRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*ALL_ROLES))],
)
async def create_report(
    payload: ReportCreate, user: CurrentUser, session: DbSession
) -> ReportRead:
    """Save a report, validating the definition before it is stored.

    Validated on save *and* on every run. Saving an unrunnable definition would
    put the error somewhere nobody sees it until a schedule fires at 7am.
    """
    definition = payload.definition.to_definition()
    role = permissions.scope_role(user)
    scope = await report_service.build_scope(session, user)
    try:
        report_service.compiler.compile_report(definition, role=role, scope=scope)
    except (CompileError, SemanticError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    report = Report(
        tenant_id=user.tenant_id,
        name=payload.name,
        description=payload.description,
        entity_key=definition.entity_key,
        definition=report_service.definition_to_json(definition),
        chart_type=payload.chart_type,
        visibility=payload.visibility,
        shared_with_user_ids=[str(u) for u in payload.shared_with_user_ids],
        owner_user_id=user.id,
    )
    session.add(report)
    await session.commit()
    await session.refresh(report)
    return ReportRead.model_validate(report)


@router.get(
    "/reports",
    response_model=list[ReportRead],
    dependencies=[Depends(require_roles(*ALL_ROLES))],
)
async def list_reports(user: CurrentUser, session: DbSession) -> list[ReportRead]:
    """Reports this user can open: their own, tenant-wide ones, and ones shared
    with them by name."""
    stmt = select(Report).where(
        or_(
            Report.owner_user_id == user.id,
            Report.visibility == ReportVisibility.TENANT,
            Report.shared_with_user_ids.contains([str(user.id)]),
        )
    ).order_by(Report.name)
    rows = await session.scalars(tenant_scoped(stmt, Report, user))
    return [ReportRead.model_validate(r) for r in rows.all()]


@router.post("/reports/run", dependencies=[Depends(require_roles(*ALL_ROLES))])
async def run_ad_hoc(
    payload: DefinitionIn,
    user: CurrentUser,
    session: DbSession,
    request: Request,
    today: date | None = None,
) -> dict[str, Any]:
    """Run a definition without saving it — the builder's preview."""
    result = await report_service.run_definition(
        session, user, payload.to_definition(), today=today, request=request
    )
    await session.commit()
    return result


@router.get("/reports/{report_id}/run", dependencies=[Depends(require_roles(*ALL_ROLES))])
async def run_saved(
    report_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    request: Request,
    limit: int | None = None,
    offset: int | None = None,
) -> dict[str, Any]:
    """Run a saved report **as the caller**, never as its author."""
    result = await report_service.run_saved_report(
        session, user, report_id=report_id, limit=limit, offset=offset, request=request
    )
    await session.commit()
    return result


@router.get(
    "/reports/{report_id}/export", dependencies=[Depends(require_roles(*ALL_ROLES))]
)
async def export(
    report_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    request: Request,
    export_format: ExportFormat = ExportFormat.CSV,
) -> Response:
    """Export a saved report. Restricted-field exports are audited."""
    result = await report_service.export_report(
        session, user, report_id=report_id, export_format=export_format, request=request
    )
    await session.commit()

    if result["should_deliver_asynchronously"]:
        raise HTTPException(
            status_code=status.HTTP_202_ACCEPTED,
            detail=(
                f"This export has {result['returned_rows']:,} rows. It will be "
                "prepared in the background and sent to you — a download this size "
                "times out on a slow connection more often than it completes."
            ),
        )

    headers = {"Content-Disposition": f'attachment; filename="{result["filename"]}"'}
    if result.get("format_note"):
        headers["X-Export-Note"] = "Delivered as CSV; this server does not produce Excel files."
    return Response(
        content=result["content"], media_type="text/csv; charset=utf-8", headers=headers
    )


@router.delete(
    "/reports/{report_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    dependencies=[Depends(require_roles(*ALL_ROLES))],
)
async def delete_report(
    report_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> None:
    report = await session.scalar(
        tenant_scoped(select(Report).where(Report.id == report_id), Report, user)
    )
    if report is None or report.owner_user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Report not found, or not yours to delete.",
        )
    await session.delete(report)
    await session.commit()


# ═══════════════════════════════════════════════════════════════════════════
# Schedules
# ═══════════════════════════════════════════════════════════════════════════


@router.post(
    "/reports/schedules",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*ALL_ROLES))],
)
async def create_schedule(
    payload: ScheduleCreate, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    """Schedule a report.

    Each recipient's copy is compiled as that recipient when it runs, so a
    schedule cannot be used to send somebody data their own role would not
    return.
    """
    report = await session.scalar(
        tenant_scoped(
            select(Report).where(Report.id == payload.report_id), Report, user
        )
    )
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")

    from datetime import UTC

    schedule = ReportSchedule(
        tenant_id=user.tenant_id,
        report_id=payload.report_id,
        cadence=payload.cadence,
        hour_of_day=payload.hour_of_day,
        day_of_week=payload.day_of_week,
        day_of_month=payload.day_of_month,
        timezone_name=payload.timezone_name,
        recipient_user_ids=[str(u) for u in payload.recipient_user_ids],
        export_format=payload.export_format,
        on_empty=payload.on_empty,
        created_by_user_id=user.id,
    )
    schedule.next_run_at = report_service.next_run_after(
        schedule, after=datetime.now(UTC)
    )
    session.add(schedule)
    await session.commit()
    await session.refresh(schedule)
    return {
        "schedule_id": str(schedule.id),
        "next_run_at": schedule.next_run_at.isoformat(),
        "timezone": schedule.timezone_name,
        "recipients": len(schedule.recipient_user_ids),
    }


@router.get("/reports/schedules", dependencies=[Depends(require_roles(*ALL_ROLES))])
async def list_schedules(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    """Your schedules; every schedule in the workspace for an admin."""
    return await report_service.list_schedules(
        session, user, include_all=user.role in ADMIN_ROLES or user.role is UserRole.SUPER_ADMIN
    )


class ScheduleActiveIn(BaseModel):
    active: bool


@router.post(
    "/reports/schedules/{schedule_id}/active",
    dependencies=[Depends(require_roles(*ALL_ROLES))],
)
async def set_schedule_active(
    schedule_id: uuid.UUID,
    payload: ScheduleActiveIn,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    """Pause or resume a schedule you created (any schedule, for an admin)."""
    return await report_service.set_schedule_active(
        session,
        user,
        schedule_id=schedule_id,
        active=payload.active,
        is_admin=user.role in ADMIN_ROLES or user.role is UserRole.SUPER_ADMIN,
        request=request,
    )


@router.post(
    "/reports/schedules/{schedule_id}/run",
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def run_schedule_now(
    schedule_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    result = await report_service.run_schedule(session, user, schedule_id=schedule_id)
    await session.commit()
    return result


@router.get("/reports/schedules/due", dependencies=[Depends(require_roles(*ADMIN_ROLES))])
async def schedules_due(
    user: CurrentUser, session: DbSession, as_of: datetime | None = None
) -> list[dict[str, Any]]:
    rows = await report_service.due_schedules(session, user, as_of=as_of)
    return [
        {
            "schedule_id": str(s.id),
            "report_id": str(s.report_id),
            "cadence": s.cadence.value,
            "timezone": s.timezone_name,
            "recipients": len(s.recipient_user_ids or []),
        }
        for s in rows
    ]


# ═══════════════════════════════════════════════════════════════════════════
# The audit trail
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/reports/activity", dependencies=[Depends(require_roles(*ADMIN_ROLES))])
async def activity(
    user: CurrentUser,
    session: DbSession,
    sensitivity: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Who ran what, and who was sent what.

    The reason this endpoint exists: a report builder with scheduled distribution
    is a data-exfiltration path if nobody looks at it. Somebody scheduling the
    salary register weekly to a personal address is invisible unless the sends
    are recorded with their recipients — so they are, and this is where an admin
    reads them.
    """
    run_stmt = select(ReportRun).order_by(ReportRun.created_at.desc()).limit(limit)
    if sensitivity:
        run_stmt = run_stmt.where(ReportRun.max_sensitivity == sensitivity)
    runs = await session.scalars(tenant_scoped(run_stmt, ReportRun, user))

    delivery_stmt = (
        select(ReportDelivery).order_by(ReportDelivery.sent_at.desc()).limit(limit)
    )
    if sensitivity:
        delivery_stmt = delivery_stmt.where(ReportDelivery.max_sensitivity == sensitivity)
    deliveries = await session.scalars(
        tenant_scoped(delivery_stmt, ReportDelivery, user)
    )

    return {
        "runs": [
            {
                "run_id": str(r.id),
                "report_id": str(r.report_id) if r.report_id else None,
                "run_by": str(r.run_by_user_id) if r.run_by_user_id else None,
                "role": r.run_as_role,
                "row_scope": r.row_scope,
                "status": r.status.value,
                "rows": r.row_count,
                "max_sensitivity": r.max_sensitivity,
                "at": r.created_at.isoformat(),
            }
            for r in runs.all()
        ],
        "deliveries": [
            {
                "recipient": d.recipient_email,
                "rows": d.row_count,
                "max_sensitivity": d.max_sensitivity,
                "format": d.export_format.value,
                "sent_at": d.sent_at.isoformat(),
            }
            for d in deliveries.all()
        ],
    }


# ── People analytics and the dashboard ──────────────────────────────────────

ANALYTICS = [Depends(require_roles(UserRole.ADMIN, UserRole.SUB_ADMIN, line_managers=True))]


class PinIn(BaseModel):
    report_id: uuid.UUID


@router.get("/reports/analytics/headcount", dependencies=ANALYTICS)
async def headcount_trend(
    user: CurrentUser, session: DbSession, months: int = 12
) -> list[dict[str, Any]]:
    """Headcount at each month end, with joiners and leavers. Scoped to whom you may see."""
    from app.services.attendance.service import local_today
    from app.services.reporting import analytics

    return await analytics.headcount_trend(
        session, user, today=await local_today(session, user), months=max(1, min(months, 36))
    )


@router.get("/reports/analytics/attrition", dependencies=ANALYTICS)
async def attrition(
    user: CurrentUser, session: DbSession, months: int = 12, by_department: bool = False
) -> dict[str, Any]:
    """Attrition as a rate over average headcount, total and voluntary, by month."""
    from app.services.attendance.service import local_today
    from app.services.reporting import analytics

    return await analytics.attrition(
        session,
        user,
        today=await local_today(session, user),
        months=max(1, min(months, 36)),
        by_department=by_department,
    )


@router.get("/reports/analytics/leave-trend", dependencies=ANALYTICS)
async def leave_trend(user: CurrentUser, session: DbSession, months: int = 12) -> dict[str, Any]:
    """Approved leave days by month and leave type."""
    from app.services.attendance.service import local_today
    from app.services.reporting import analytics

    return await analytics.leave_trend(
        session, user, today=await local_today(session, user), months=max(1, min(months, 36))
    )


@router.get("/reports/analytics/signals", dependencies=ANALYTICS)
async def absence_signals(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    """Rule-based absence and burnout signals, each with its rule and evidence."""
    from app.services.attendance.service import local_today
    from app.services.reporting import analytics

    return await analytics.absence_signals(session, user, today=await local_today(session, user))


@router.get("/reports/analytics/performance", dependencies=ANALYTICS)
async def performance(user: CurrentUser, session: DbSession) -> dict[str, Any]:
    """Ratings over recent cycles, goal attainment, and rater spread (HR only)."""
    from app.services.reporting import performance_analytics

    return await performance_analytics.performance_overview(session, user)


@router.get("/reports/analytics/absence-scores", dependencies=ANALYTICS)
async def absence_scores(user: CurrentUser, session: DbSession) -> dict[str, Any]:
    """Bradford factor per person over 52 weeks, with its spells, days and formula."""
    from app.services.attendance.service import local_today
    from app.services.reporting import analytics

    return await analytics.absence_scores(session, user, today=await local_today(session, user))


@router.get("/reports/dashboard", dependencies=ANALYTICS)
async def my_dashboard(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    from app.services.reporting import analytics

    return await analytics.dashboard(session, user)


@router.post("/reports/dashboard", dependencies=ANALYTICS)
async def pin_report(payload: PinIn, user: CurrentUser, session: DbSession) -> list[dict]:
    from app.services.reporting import analytics

    return await analytics.pin_report(session, user, payload.report_id)


@router.delete("/reports/dashboard/{widget_id}", dependencies=ANALYTICS)
async def unpin_report(widget_id: uuid.UUID, user: CurrentUser, session: DbSession) -> list[dict]:
    from app.services.reporting import analytics

    return await analytics.unpin_report(session, user, widget_id)
