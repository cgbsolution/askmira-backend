"""Executing an approved erasure request. DB-facing.

`rights.reconcile_erasure` decides, category by category, what may go and what
the law says must stay. This module carries that decision out against the rows,
in one transaction with the request's resolution and its audit entry:

- A table whose every category is PURGED loses this person's rows. The row ids
  are added to the erasure suppression list so a backup restore cannot bring
  them back.
- The person's employee record and login are never deleted — payroll, statutory
  registers and the audit trail hang off them. Their nullable fields in a purged
  category are cleared instead.
- Anything else is retained, and the response says which obligation keeps it and
  until when.

**Refused outright** while a legal hold covers the person: a hold exempts records
from every deletion, erasure included, and the request stays open for a person
to review. Nobody executes an erasure of their own data.

The pseudonymisation the plan names for the audit trail is **not** executed: the
audit log is insert-only by design and nothing rewrites it. The response lists it
as retained with that reason.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_filter, tenant_scoped
from app.models.audit import AuditAction
from app.models.dpdp import DataPrincipalRequest, DprRequestStatus, DprRequestType
from app.models.employee import Employee
from app.models.user import User
from app.services import audit
from app.services.dpdp import rights as rights_engine
from app.services.dpdp.contents import person_predicate, personal_tables, withheld_column
from app.services.dpdp.schema_map import STRUCTURAL_COLUMNS, TABLES

__all__ = ["NEVER_DELETED", "execute_erasure"]

#: Rows about the person that an erasure never deletes. Each is either the spine
#: statutory records hang from, or the evidence that the erasure happened.
NEVER_DELETED: dict[str, str] = {
    "employees": "The employee record anchors statutory registers and payroll history.",
    "users": "The login anchors the audit trail of what was done and by whom.",
    "audit_logs": "The audit trail is insert-only; nothing rewrites or deletes it.",
    "consent_records": "Consent records are insert-only evidence of what was agreed.",
    "data_principal_requests": "This request is the record that the erasure happened.",
}

_OPEN = (
    DprRequestStatus.RECEIVED,
    DprRequestStatus.VERIFYING_IDENTITY,
    DprRequestStatus.IN_PROGRESS,
    DprRequestStatus.ESCALATED,
)


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _hold_covers(hold: Any, *, principal_id: uuid.UUID, employee: Employee | None) -> bool:
    ids = {str(value) for value in (hold.employee_ids or [])}
    if ids and str(principal_id) not in ids:
        return False
    departments = set(hold.departments or [])
    if departments:
        if employee is None or employee.department_id is None:
            return False
        if str(employee.department_id) not in departments:
            return False
    return True


async def execute_erasure(
    session: AsyncSession,
    user: User,
    *,
    request_id: uuid.UUID,
    request: Request | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    from app.services.dpdp.service import erasure_plan_for
    from app.services.retention import service as retention_service

    moment = now or datetime.now(UTC)
    row = await session.scalar(
        tenant_scoped(
            select(DataPrincipalRequest).where(DataPrincipalRequest.id == request_id),
            DataPrincipalRequest,
            user,
        )
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such request.")
    if row.request_type is not DprRequestType.ERASURE:
        raise _bad_request(f"This is a {row.request_type.value} request, not an erasure request.")
    if row.status not in _OPEN:
        raise _bad_request(f"This request is already {row.status.value}.")
    if row.principal_id in {user.employee_id, user.id}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="An erasure of your own data has to be executed by another administrator.",
        )

    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == row.principal_id), Employee, user)
    )
    login = await session.scalar(
        tenant_scoped(
            select(User).where(
                or_(User.employee_id == row.principal_id, User.id == row.principal_id)
            ),
            User,
            user,
        )
    )

    holds = await retention_service.active_holds(session, user)
    if any(_hold_covers(h, principal_id=row.principal_id, employee=employee) for h in holds):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "A legal hold covers this person, so nothing can be erased while it is in "
                "place. The request stays open: release the hold or reject the request with "
                "the reason."
            ),
        )

    plan = await erasure_plan_for(session, user, principal_id=row.principal_id, today=moment.date())
    decisions = {d.category_key: d for d in plan.decisions}
    purgeable = {
        key for key, d in decisions.items() if d.outcome is rights_engine.ErasureOutcome.PURGED
    }

    executed: list[dict[str, Any]] = []
    deleted: list[tuple[str, uuid.UUID]] = []
    anything_retained = False

    for table in personal_tables():
        predicate = person_predicate(
            table,
            employee_id=employee.id if employee else None,
            user_id=login.id if login else None,
        )
        if predicate is None or "id" not in table.c:
            continue
        ids = list(
            (
                await session.execute(
                    select(table.c.id).where(tenant_filter(table, user.tenant_id), predicate)
                )
            ).scalars()
        )
        if not ids:
            continue

        classification = TABLES[table.name]
        payload_columns = [
            c for c in table.columns if c.name not in STRUCTURAL_COLUMNS and not c.primary_key
        ]
        categories = {
            key for key in (classification.category_for(c.name) for c in payload_columns) if key
        }

        if table.name in NEVER_DELETED:
            clearable = [
                c.name
                for c in payload_columns
                if c.nullable
                and not withheld_column(c.name)
                and classification.category_for(c.name) in purgeable
                and not c.foreign_keys
            ]
            if clearable:
                await session.execute(
                    update(table)
                    .where(tenant_filter(table, user.tenant_id), table.c.id.in_(ids))
                    .values({name: None for name in clearable})
                )
                executed.append(
                    {
                        "table": table.name,
                        "outcome": "fields_cleared",
                        "fields": clearable,
                        "rows": len(ids),
                    }
                )
            anything_retained = True
            executed.append(
                {
                    "table": table.name,
                    "outcome": "retained",
                    "rows": len(ids),
                    "reason": NEVER_DELETED[table.name],
                }
            )
            continue

        if categories and categories <= purgeable:
            try:
                async with session.begin_nested():
                    await session.execute(
                        delete(table).where(
                            tenant_filter(table, user.tenant_id), table.c.id.in_(ids)
                        )
                    )
            except IntegrityError:
                anything_retained = True
                executed.append(
                    {
                        "table": table.name,
                        "outcome": "retained",
                        "rows": len(ids),
                        "reason": "Other records still refer to these rows, so they were kept.",
                    }
                )
                continue
            deleted.extend((table.name, rid) for rid in ids)
            executed.append({"table": table.name, "outcome": "deleted", "rows": len(ids)})
            continue

        anything_retained = True
        blocking = sorted(categories - purgeable)
        reasons = [
            {
                "category": key,
                "outcome": decisions[key].outcome.value if key in decisions else "not_decided",
                "retained_until": (
                    decisions[key].retained_until.isoformat()
                    if key in decisions and decisions[key].retained_until
                    else None
                ),
                "statutory_reference": (
                    decisions[key].statutory_reference if key in decisions else None
                ),
            }
            for key in blocking
        ]
        executed.append(
            {"table": table.name, "outcome": "retained", "rows": len(ids), "because": reasons}
        )

    suppressed = 0
    if deleted:
        suppressed = await retention_service.suppress_for_erasure(
            session,
            user,
            subject_id=row.principal_id,
            records=deleted,
            source_request_id=row.id,
            now=moment,
            commit=False,
        )

    complete = plan.is_complete and not anything_retained
    row.status = DprRequestStatus.COMPLETED if complete else DprRequestStatus.PARTIALLY_COMPLETED
    row.responded_at = moment
    row.response_payload = {
        "executed_at": moment.isoformat(),
        "plan": plan.as_dict(),
        "executed": executed,
        "rows_deleted": len(deleted),
        "suppressed_against_restore": suppressed,
        "not_executed": (
            "The audit trail is not pseudonymised: it is insert-only, and its entries "
            "about you are kept as the record of what was done."
        ),
    }
    row.actions = [
        *(row.actions or []),
        {
            "at": moment.isoformat(),
            "actor": str(user.id),
            "action": row.status.value,
            "note": f"Erasure executed: {len(deleted)} row(s) deleted.",
        },
    ]
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.DELETE,
        entity_type="data_principal_request",
        entity_id=row.id,
        actor=user,
        summary=(
            f"Erasure executed for {row.principal_label}: {len(deleted)} row(s) deleted, "
            f"{'nothing retained' if complete else 'statutory and evidential records retained'}."
        ),
        changes={
            "tables_deleted": sorted({t for t, _ in deleted}),
            "rows_deleted": len(deleted),
            "status": row.status.value,
        },
        request=request,
    )
    await session.commit()
    return {
        "id": str(row.id),
        "status": row.status.value,
        "rows_deleted": len(deleted),
        "executed": executed,
        "plan": plan.as_dict(),
    }
