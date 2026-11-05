"""Ask Mira: which new joiners still have no equipment assigned.

The joiners are AskMira's: people joining in the next 30 days or who joined in
the last 30 (or the window asked about). What they hold comes from the asset
register — CGB Helpdesk's when it is linked (kind ``holdings``, by work email),
else AskMira's own assignments. Only HR asks; names stay on the screen.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.assets import AssetAssignment
from app.models.employee import Employee, EmploymentStatus
from app.models.org import Department
from app.models.tenant import Tenant
from app.models.user import User
from app.services.products import data as products
from app.services.products.catalogue import PRODUCTS

__all__ = ["joiners_equipment"]

WINDOW = timedelta(days=30)


def _day(value: Any, fallback: date) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return fallback


async def joiners_equipment(
    session: AsyncSession, user: User, inputs: dict[str, Any], *, today: date
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    start = _day(inputs.get("from") or "", today - WINDOW)
    end = _day(inputs.get("to") or "", today + WINDOW)
    if end < start:
        start, end = end, start
    rows = (
        await session.execute(
            tenant_scoped(
                select(
                    Employee.id,
                    Employee.display_name,
                    Employee.first_name,
                    Employee.last_name,
                    Employee.work_email,
                    Employee.date_of_joining,
                    Department.name.label("department"),
                )
                .outerjoin(Department, Department.id == Employee.department_id)
                .where(
                    Employee.date_of_joining >= start,
                    Employee.date_of_joining <= end,
                    Employee.status != EmploymentStatus.EXITED,
                )
                .order_by(Employee.date_of_joining, Employee.first_name),
                Employee,
                user,
            )
        )
    ).all()
    used = {"from": start.isoformat(), "to": end.isoformat()}
    span = f"{start:%d %b} to {end:%d %b %Y}"
    if not rows:
        return [
            {"type": "notice", "tone": "info", "text": f"Nobody joins or joined between {span}."}
        ], used

    tenant = await session.get(Tenant, user.tenant_id)
    source = "AskMira's asset register"
    held: dict[str, int] = {}
    if tenant and (tenant.linked_products or {}).get("helpdesk"):
        source = f"{PRODUCTS['helpdesk'].brand}'s asset register"
        emails = [str(r.work_email).lower() for r in rows if r.work_email][:200]
        answer = await products.ask(session, user, "helpdesk", "holdings", {"emails": emails})
        held = {
            str(h.get("email")).lower(): int(h.get("assets") or 0)
            for h in answer.get("holdings") or []
            if isinstance(h, dict)
        }
        count_of = {r.id: held.get(str(r.work_email).lower(), 0) for r in rows}
    else:
        counted = (
            await session.execute(
                tenant_scoped(
                    select(AssetAssignment.employee_id, func.count())
                    .where(
                        AssetAssignment.employee_id.in_([r.id for r in rows]),
                        AssetAssignment.returned_at.is_(None),
                    )
                    .group_by(AssetAssignment.employee_id),
                    AssetAssignment,
                    user,
                )
            )
        ).all()
        by_id = {emp: int(n) for emp, n in counted}
        count_of = {r.id: by_id.get(r.id, 0) for r in rows}

    table_rows = []
    waiting = 0
    for r in rows:
        name = r.display_name or " ".join(p for p in (r.first_name, r.last_name) if p)
        n = count_of[r.id]
        waiting += n == 0
        table_rows.append(
            [
                name,
                r.department or "—",
                f"{r.date_of_joining:%d %b %Y}",
                str(n),
                "Needs equipment" if n == 0 else "Has equipment",
            ]
        )
    table_rows.sort(key=lambda row: (row[4] != "Needs equipment", row[2]))
    return [
        {
            "type": "table",
            "title": "Joiners and their equipment",
            "caption": f"Joining {span} · {source}",
            "columns": ["Joiner", "Department", "Joins on", "Assets held", "Status"],
            "rows": table_rows,
            # Who joins is a name list: the model reads the count only.
            "model_view": (
                f"{len(rows)} joiner(s) between {span}; {waiting} have no equipment assigned "
                f"in {source}. Their names are on the person's screen."
            ),
        }
    ], used
