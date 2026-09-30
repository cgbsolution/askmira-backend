"""The DB-facing half of retention. Sessions, models, transactions — nothing else.

The floor arithmetic, the anonymisation, the k-anonymity check and the planning
all live in the pure modules beside this, where a reviewer can read them without
reading SQLAlchemy. An invariant checks the isolation holds.

**Execution is one transaction.** `execute_plan` deletes and anonymises the
planned rows, writes the action logs, the suppressions and the audit row, and
commits once. A function that committed as it went could leave a run
half-applied with no way to tell which half — and the half it deleted is not
coming back.
"""

from __future__ import annotations

import calendar
import hashlib
import hmac
import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401 — registers every table before the metadata is walked
from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.base import Base
from app.models.dpdp import DataPrincipalRequest
from app.models.employee import Employee
from app.models.retention import (
    ErasureSuppression,
    LegalHold,
    RetentionActionKind,
    RetentionActionLog,
    RetentionAnchor,
    RetentionPolicyRow,
    RetentionRun,
    RetentionRunStatus,
)
from app.models.user import User
from app.services import audit
from app.services.dpdp.schema_map import TABLES, is_direct_identifier
from app.services.retention import planner as planner_engine
from app.services.retention import policy as policy_engine
from app.services.retention.taxonomy import BACKUP_DISCLOSURE, trust_centre_taxonomy

__all__ = [
    "active_holds",
    "apply_suppressions",
    "execute_plan",
    "gather_candidates",
    "place_hold",
    "plan_run",
    "public_snapshot",
    "release_hold",
    "scheduled_sweep",
    "seed_default_policies",
    "set_policy",
    "suppress_for_erasure",
    "trust_centre",
]

#: How long a suppression has to outlive the deletion: the backup retention
#: window, plus a week of slack for a restore that starts on the last day.
_SUPPRESSION_DAYS = BACKUP_DISCLOSURE["retention_window_days"] + 7

#: Tables no retention action ever touches: the evidence that actions happened,
#: the consent record, and the spine statutory records hang from.
NEVER_ACTIONED: frozenset[str] = frozenset(
    {
        "tenants",
        "users",
        "employees",
        "audit_logs",
        "consent_records",
        "data_principal_requests",
        "retention_policies",
        "retention_runs",
        "retention_action_logs",
        "legal_holds",
        "erasure_suppressions",
    }
)

#: The most records one plan carries for execution.
MAX_PLANNED_RECORDS = 10_000


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _hash_record_id(record_id: str, *, tenant_id: uuid.UUID) -> str:
    """Hash a record id for the action log.

    Keyed on the tenant so two tenants deleting the same uuid do not produce the
    same hash, and truncated because the log needs to confirm membership, not
    enumerate. See `RetentionActionLog` for why it is hashed at all.
    """
    digest = hmac.new(
        str(tenant_id).encode("utf-8"), record_id.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return digest[:32]


# ══════════════════════════════════════════════════════════════════════════════
# Policies
# ══════════════════════════════════════════════════════════════════════════════


async def set_policy(
    session: AsyncSession,
    user: User,
    *,
    data_category_key: str,
    retention_period_months: int,
    action: RetentionActionKind | None = None,
    active: bool = True,
    request: Request | None = None,
) -> RetentionPolicyRow:
    """Write one policy, refusing anything below the statutory floor.

    The refusal comes from the engine and names the statute and both numbers.
    A message saying only that the value is invalid leaves the tenant to guess,
    and the guess is usually one month more.
    """
    from app.services.retention.taxonomy import RetentionAction

    try:
        validated = policy_engine.validate_policy(
            data_category_key=data_category_key,
            retention_period_months=retention_period_months,
            action=RetentionAction(action.value) if action else None,
        )
    except policy_engine.PolicyError as exc:
        raise _bad_request(str(exc)) from exc

    stmt = tenant_scoped(
        select(RetentionPolicyRow).where(
            RetentionPolicyRow.data_category_key == data_category_key
        ),
        RetentionPolicyRow,
        user,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        row = RetentionPolicyRow(
            tenant_id=user.tenant_id, data_category_key=data_category_key
        )
        session.add(row)

    row.retention_period_months = validated.retention_period_months
    row.anchor_event = RetentionAnchor(validated.anchor_event.value)
    row.action = RetentionActionKind(validated.action.value)
    row.statutory_minimum_months = validated.statutory_minimum_months
    row.legal_basis = validated.legal_basis
    row.active = active
    row.configured_by_user_id = user.id

    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="retention_policy",
        entity_id=row.id,
        actor=user,
        summary=(
            f"Retention for {data_category_key} set to "
            f"{retention_period_months} months (floor "
            f"{validated.statutory_minimum_months})."
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def seed_default_policies(
    session: AsyncSession, user: User, *, request: Request | None = None
) -> list[RetentionPolicyRow]:
    """Write the system defaults for a tenant that has configured nothing.

    The defaults are the statutory floor exactly — not the floor plus a margin
    somebody added for comfort, because a margin is a year of extra personal
    data held for no articulable reason, which is the DPDP side of the conflict
    this module resolves.
    """
    existing_stmt = tenant_scoped(
        select(RetentionPolicyRow.data_category_key), RetentionPolicyRow, user
    )
    existing = set((await session.execute(existing_stmt)).scalars().all())

    written: list[RetentionPolicyRow] = []
    for default in policy_engine.default_policies():
        if default.data_category_key in existing:
            continue
        row = RetentionPolicyRow(
            tenant_id=user.tenant_id,
            data_category_key=default.data_category_key,
            retention_period_months=default.retention_period_months,
            anchor_event=RetentionAnchor(default.anchor_event.value),
            action=RetentionActionKind(default.action.value),
            statutory_minimum_months=default.statutory_minimum_months,
            legal_basis=default.legal_basis,
            active=True,
            configured_by_user_id=user.id,
        )
        session.add(row)
        written.append(row)

    await session.flush()
    if written:
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.CREATE,
            entity_type="retention_policy",
            actor=user,
            summary=f"Seeded {len(written)} default retention policies.",
            request=request,
        )
    await session.commit()
    return written


async def list_policies(session: AsyncSession, user: User) -> dict[str, Any]:
    stmt = tenant_scoped(select(RetentionPolicyRow), RetentionPolicyRow, user)
    rows = (
        (await session.execute(stmt.order_by(RetentionPolicyRow.data_category_key)))
        .scalars()
        .all()
    )
    from app.services.dpdp.categories import CATEGORIES

    configured = {row.data_category_key for row in rows}
    return {
        "policies": [
            {
                "category": row.data_category_key,
                "category_name": CATEGORIES[row.data_category_key].name
                if row.data_category_key in CATEGORIES
                else row.data_category_key,
                "retention_period_months": row.retention_period_months,
                "anchor_event": row.anchor_event.value,
                "action": row.action.value,
                "statutory_minimum_months": row.statutory_minimum_months,
                "has_statutory_floor": row.statutory_minimum_months > 0,
                "legal_basis": row.legal_basis,
                "active": row.active,
            }
            for row in rows
        ],
        # Reported, not hidden. A category with no policy is retained forever,
        # and a settings page listing only what is configured would present that
        # as nothing to do.
        "unconfigured_categories": sorted(set(CATEGORIES) - configured),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Legal holds
# ══════════════════════════════════════════════════════════════════════════════


async def place_hold(
    session: AsyncSession,
    user: User,
    *,
    reason: str,
    employee_ids: list[uuid.UUID] | None = None,
    departments: list[str] | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    request: Request | None = None,
    now: datetime | None = None,
) -> LegalHold:
    """Freeze a scope of records against every retention policy.

    A reason is required and is not a formality: a hold with no reason is a hold
    nobody can decide whether to release, and holds that nobody can release
    accumulate until the retention job does nothing at all.
    """
    if not reason.strip():
        raise _bad_request(
            "A legal hold needs a reason. Without one, nobody can later decide "
            "whether it is still needed, and a hold nobody will release makes "
            "the whole retention policy decorative."
        )

    row = LegalHold(
        tenant_id=user.tenant_id,
        reason=reason.strip(),
        employee_ids=[str(i) for i in (employee_ids or [])],
        departments=list(departments or []),
        from_date=from_date,
        to_date=to_date,
        placed_by_user_id=user.id,
        placed_at=now or datetime.now(UTC),
    )
    session.add(row)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="legal_hold",
        entity_id=row.id,
        actor=user,
        summary=(
            f"Legal hold placed: {reason.strip()[:120]} "
            f"({len(row.employee_ids) or 'all'} employees)."
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def release_hold(
    session: AsyncSession,
    user: User,
    *,
    hold_id: uuid.UUID,
    release_reason: str,
    request: Request | None = None,
    now: datetime | None = None,
) -> LegalHold:
    """Release a hold. Sets a timestamp; never deletes the row.

    The row is the evidence that records were frozen between two dates, which is
    the whole reason a hold exists.
    """
    stmt = tenant_scoped(
        select(LegalHold).where(LegalHold.id == hold_id), LegalHold, user
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise _not_found("No such legal hold.")
    if row.released_at is not None:
        raise _bad_request("That hold was already released.")
    if not release_reason.strip():
        raise _bad_request(
            "Releasing a hold needs a reason — it is the moment records become "
            "deletable again, and somebody will ask who decided that."
        )

    row.released_at = now or datetime.now(UTC)
    row.released_by_user_id = user.id
    row.release_reason = release_reason.strip()

    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="legal_hold",
        entity_id=row.id,
        actor=user,
        summary=f"Legal hold released: {release_reason.strip()[:120]}",
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def active_holds(session: AsyncSession, user: User) -> list[LegalHold]:
    stmt = tenant_scoped(
        select(LegalHold).where(LegalHold.released_at.is_(None)), LegalHold, user
    )
    return list((await session.execute(stmt)).scalars().all())


def _hold_scopes(holds: list[LegalHold]) -> list[planner_engine.LegalHoldScope]:
    return [
        planner_engine.LegalHoldScope(
            employee_ids=frozenset(hold.employee_ids or []),
            departments=frozenset(hold.departments or []),
            from_date=hold.from_date,
            to_date=hold.to_date,
        )
        for hold in holds
    ]


# ══════════════════════════════════════════════════════════════════════════════
# Planning and execution
# ══════════════════════════════════════════════════════════════════════════════


async def _policies_for(
    session: AsyncSession, user: User
) -> dict[str, policy_engine.RetentionPolicy]:
    stmt = tenant_scoped(select(RetentionPolicyRow), RetentionPolicyRow, user)
    rows = (await session.execute(stmt)).scalars().all()

    from app.services.retention.taxonomy import RetentionAction

    return {
        row.data_category_key: policy_engine.RetentionPolicy(
            data_category_key=row.data_category_key,
            retention_period_months=row.retention_period_months,
            anchor_event=policy_engine.AnchorEvent(row.anchor_event.value),
            action=RetentionAction(row.action.value),
            statutory_minimum_months=row.statutory_minimum_months,
            legal_basis=row.legal_basis,
            tenant_id=str(row.tenant_id),
            active=row.active,
        )
        for row in rows
    }


async def plan_run(
    session: AsyncSession,
    user: User,
    *,
    candidates: list[planner_engine.CandidateRecord],
    today: date | None = None,
    approval_threshold: int = planner_engine.DEFAULT_APPROVAL_THRESHOLD,
    is_dry_run: bool = True,
    request: Request | None = None,
) -> RetentionRun:
    """Evaluate the policies and record what would happen. Changes no records.

    The candidates are supplied by the caller rather than gathered here. That
    keeps the planning testable against a fixture, and it keeps the sweep's
    "which rows are candidates" question — which is per-module and will grow —
    out of the function that decides what happens to them.
    """
    as_of = today or datetime.now(UTC).date()
    holds = await active_holds(session, user)

    computed = planner_engine.plan(
        candidates,
        policies=await _policies_for(session, user),
        legal_holds=_hold_scopes(holds),
        today=as_of,
        approval_threshold=approval_threshold,
        is_dry_run=is_dry_run,
    )

    if is_dry_run:
        run_status = RetentionRunStatus.DRY_RUN
    elif computed.needs_approval:
        run_status = RetentionRunStatus.AWAITING_APPROVAL
    else:
        run_status = RetentionRunStatus.APPROVED

    row = RetentionRun(
        tenant_id=user.tenant_id,
        evaluated_on=as_of,
        status=run_status,
        records_evaluated=len(computed.actions),
        records_to_act=len(computed.to_act),
        records_held=len(computed.held),
        records_blocked=len(computed.blocked),
        counts_by_action=computed.counts_by_action(),
        approval_threshold=approval_threshold,
        plan_snapshot=_snapshot(computed),
    )
    session.add(row)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="retention_run",
        entity_id=row.id,
        actor=user,
        summary=(
            f"{'Dry run' if is_dry_run else 'Retention run'}: "
            f"{len(computed.to_act)} to act, {len(computed.held)} held, "
            f"{len(computed.blocked)} blocked."
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def approve_run(
    session: AsyncSession,
    user: User,
    *,
    run_id: uuid.UUID,
    request: Request | None = None,
    now: datetime | None = None,
) -> RetentionRun:
    """Authorise a run that is over the volume threshold.

    A dry run cannot be approved: it was produced to be read. Re-plan with
    `is_dry_run=False` if it is meant to happen.
    """
    row = await _run_or_404(session, user, run_id)

    if row.status is RetentionRunStatus.DRY_RUN:
        raise _bad_request(
            "That was a dry run, produced to be read rather than executed. "
            "Re-plan it as a real run if it is meant to happen — approving a "
            "dry run would execute a plan nobody intended to execute."
        )
    if row.status is not RetentionRunStatus.AWAITING_APPROVAL:
        raise _bad_request(
            f"That run is {row.status.value}, not awaiting approval."
        )

    row.status = RetentionRunStatus.APPROVED
    row.approved_by_user_id = user.id
    row.approved_at = now or datetime.now(UTC)

    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.APPROVE,
        entity_type="retention_run",
        entity_id=row.id,
        actor=user,
        summary=(
            f"Retention run approved: {row.records_to_act} records, "
            f"{row.counts_by_action}."
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def reject_run(
    session: AsyncSession,
    user: User,
    *,
    run_id: uuid.UUID,
    reason: str,
    request: Request | None = None,
) -> RetentionRun:
    row = await _run_or_404(session, user, run_id)
    if not reason.strip():
        raise _bad_request("Rejecting a retention run needs a reason.")
    row.status = RetentionRunStatus.REJECTED
    row.rejected_reason = reason.strip()

    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="retention_run",
        entity_id=row.id,
        actor=user,
        summary=f"Retention run rejected: {reason.strip()[:160]}",
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def _run_or_404(
    session: AsyncSession, user: User, run_id: uuid.UUID
) -> RetentionRun:
    stmt = tenant_scoped(
        select(RetentionRun).where(RetentionRun.id == run_id), RetentionRun, user
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise _not_found("No such retention run.")
    return row


async def execute_plan(
    session: AsyncSession,
    user: User,
    *,
    run_id: uuid.UUID,
    request: Request | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Execute an approved run: delete or anonymise every planned record.

    For each planned record, in one transaction with the action logs, the
    suppressions and the audit row:

    - **Legal holds are re-checked now**, not only at planning. A hold placed
      between the plan and its execution still spares the record.
    - DELETE removes the row, inside a savepoint. A row that other records still
      refer to is kept and counted as not actioned rather than cascading.
    - ANONYMISE clears the row's nullable identifying columns — the link to the
      employee and any direct identifier. A table whose identifying columns are
      all required cannot be anonymised in place; the row is kept and counted.
    - ARCHIVE is not carried out: there is no cold storage to archive to.

    Tables in `NEVER_ACTIONED` are never touched. The response reports what was
    actually done — `records_actually_removed` is the real count — and names
    what was not.
    """
    row = await _run_or_404(session, user, run_id)
    moment = now or datetime.now(UTC)

    if row.status is RetentionRunStatus.DRY_RUN:
        raise _bad_request(
            "A dry run cannot be executed. It was produced to be read."
        )
    if row.status is RetentionRunStatus.AWAITING_APPROVAL:
        raise _bad_request(
            f"This run affects {row.records_to_act} records, which is above the "
            f"approval threshold of {row.approval_threshold}. Somebody has to "
            "approve it first — an unattended job deleting this many records is "
            "not a good surprise."
        )
    if row.status is not RetentionRunStatus.APPROVED:
        raise _bad_request(f"That run is {row.status.value} and cannot execute.")

    planned = row.plan_snapshot.get("to_act_records")
    if planned is None:
        # A plan written before execution existed carries only its sample.
        planned = row.plan_snapshot.get("sample", [])
    holds = _hold_scopes(await active_holds(session, user))
    tables = Base.metadata.tables

    done: dict[tuple[str, str, str], list[str]] = {}
    deleted: list[tuple[str, uuid.UUID]] = []
    removed = anonymised = spared = not_actioned = archive_pending = 0
    problems: list[str] = []

    for entry in planned:
        created = entry.get("record_created")
        candidate = planner_engine.CandidateRecord(
            record_id=entry["record_id"],
            table=entry["table"],
            data_category_key=entry["category"],
            subject_id=str(entry.get("subject_id") or ""),
            record_created=date.fromisoformat(created) if created else row.evaluated_on,
            department=entry.get("department"),
        )
        if any(hold.covers(candidate) for hold in holds):
            spared += 1
            continue

        table = tables.get(entry["table"])
        action_value = entry.get("action")
        if (
            table is None
            or entry["table"] in NEVER_ACTIONED
            or "id" not in table.c
            or "tenant_id" not in table.c
            or not _looks_like_uuid(entry["record_id"])
        ):
            not_actioned += 1
            problems.append(f"{entry['table']}: not a table retention may act on.")
            continue
        record_id = uuid.UUID(entry["record_id"])
        key = (entry["category"], table.name, str(action_value))

        if action_value == RetentionActionKind.DELETE.value:
            try:
                async with session.begin_nested():
                    result = await session.execute(
                        delete(table).where(
                            table.c.tenant_id == user.tenant_id, table.c.id == record_id
                        )
                    )
            except IntegrityError:
                not_actioned += 1
                problems.append(
                    f"{table.name}: other records still refer to a row, so it was kept."
                )
                continue
            if result.rowcount:
                removed += 1
                deleted.append((table.name, record_id))
                done.setdefault(key, []).append(entry["record_id"])
        elif action_value == RetentionActionKind.ANONYMISE.value:
            if await _anonymise_row(session, user, table, record_id):
                anonymised += 1
                done.setdefault(key, []).append(entry["record_id"])
            else:
                not_actioned += 1
                problems.append(
                    f"{table.name}: its identifying columns are required, so a row "
                    "cannot be anonymised in place and was kept."
                )
        else:
            archive_pending += 1

    logs = [
        RetentionActionLog(
            tenant_id=user.tenant_id,
            run_id=row.id,
            data_category_key=category_key,
            record_type=table_name,
            action=RetentionActionKind(action_value),
            record_count=len(record_ids),
            executed_at=moment,
            approved_by_user_id=row.approved_by_user_id,
            sample_record_hashes=[
                _hash_record_id(rid, tenant_id=user.tenant_id) for rid in record_ids[:20]
            ],
            policy_snapshot={"category": category_key, "action": action_value},
        )
        for (category_key, table_name, action_value), record_ids in done.items()
    ]
    suppressions = 0
    for table_name, record_id in deleted:
        session.add(
            ErasureSuppression(
                tenant_id=user.tenant_id,
                record_table=table_name,
                record_id=record_id,
                reason=(
                    f"Deleted by retention run {row.id} on "
                    f"{row.evaluated_on.isoformat()}."
                ),
                suppressed_at=moment,
                expires_on=(moment + timedelta(days=_SUPPRESSION_DAYS)).date(),
            )
        )
        suppressions += 1

    session.add_all(logs)
    row.status = RetentionRunStatus.EXECUTED
    row.executed_at = moment

    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.DELETE,
        entity_type="retention_run",
        entity_id=row.id,
        actor=user,
        summary=(
            f"Retention run executed: {removed} deleted, {anonymised} anonymised, "
            f"{spared} spared by a legal hold, {not_actioned} not actioned, "
            f"{archive_pending} archive action(s) not carried out."
        ),
        changes={
            "deleted": removed,
            "anonymised": anonymised,
            "spared_by_legal_hold": spared,
            "not_actioned": not_actioned,
        },
        request=request,
    )
    await session.commit()

    return {
        "run_id": str(row.id),
        "action_logs_written": len(logs),
        "suppressions_written": suppressions,
        "records_actually_removed": removed,
        "records_anonymised": anonymised,
        "records_spared_by_legal_hold": spared,
        "records_not_actioned": not_actioned,
        "problems": problems[:20],
        "not_built": (
            f"{archive_pending} archive action(s) were not carried out: there is no cold "
            "storage to archive to. Deleted rows remain in database backups until the "
            "backup window passes; the suppression list removes them again if a backup "
            "is restored."
        ),
    }


async def _anonymise_row(
    session: AsyncSession, user: User, table: Any, record_id: uuid.UUID
) -> bool:
    """Clear a row's nullable identifying columns. False if there is none to clear."""
    classification = TABLES.get(table.name)
    columns = [
        column.name
        for column in table.columns
        if column.nullable
        and not column.primary_key
        and (
            column.name == "employee_id"
            or column.name.endswith("_employee_id")
            or is_direct_identifier(column.name)
            or (
                classification is not None
                and classification.columns.get(column.name) == "identity"
            )
        )
    ]
    values: dict[str, Any] = {name: None for name in columns}
    # A required JSON column the classification names (profile sections, a
    # change request's payload) cannot be nulled, so it is emptied instead.
    # Leaving it would keep addresses, family and nominees after "erasure".
    if classification is not None:
        for column in table.columns:
            if (
                not column.nullable
                and isinstance(column.type, JSONB)
                and column.name in classification.columns
            ):
                values[column.name] = _empty_json(column)
    if not values:
        return False
    result = await session.execute(
        update(table)
        .where(table.c.tenant_id == user.tenant_id, table.c.id == record_id)
        .values(values)
    )
    return bool(result.rowcount)


def _empty_json(column: Any) -> dict | list:
    """An empty value of the shape the column's default produces: `{}` or `[]`."""
    default = column.default
    if default is not None and getattr(default, "is_callable", False):
        try:
            value = default.arg(None)
        except TypeError:
            value = None
        if isinstance(value, list):
            return []
    return {}


def public_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """A plan as shown on screen: the counts and the sample, not every record id."""
    return {key: value for key, value in (snapshot or {}).items() if key != "to_act_records"}


def _snapshot(computed: planner_engine.RetentionPlan) -> dict[str, Any]:
    snapshot = computed.as_dict()
    snapshot["to_act_records"] = [
        {
            "record_id": planned.record.record_id,
            "table": planned.record.table,
            "category": planned.record.data_category_key,
            "action": planned.action.value if planned.action else None,
            "subject_id": planned.record.subject_id,
            "record_created": planned.record.record_created.isoformat(),
            "department": planned.record.department,
        }
        for planned in computed.to_act[:MAX_PLANNED_RECORDS]
    ]
    return snapshot


def _months_before(day: date, months: int) -> date:
    total = day.year * 12 + (day.month - 1) - months
    year, month_index = divmod(total, 12)
    month = month_index + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


async def gather_candidates(
    session: AsyncSession,
    user: User,
    *,
    today: date,
    cap: int = MAX_PLANNED_RECORDS,
) -> list[planner_engine.CandidateRecord]:
    """Rows old enough that an active policy could apply to them.

    Walks the DPDP classification: every table holding employee data, carrying
    an `employee_id` and a `created_at`, whose category has an active policy.
    A row younger than its policy's period cannot be due under any anchor — every
    anchor falls on or after creation — so only older rows are read.
    """
    policies = {k: p for k, p in (await _policies_for(session, user)).items() if p.active}
    if not policies:
        return []

    employees = {
        emp.id: emp
        for emp in (
            await session.scalars(tenant_scoped(select(Employee), Employee, user))
        ).all()
    }
    out: list[planner_engine.CandidateRecord] = []
    for table in Base.metadata.sorted_tables:
        if len(out) >= cap:
            break
        classification = TABLES.get(table.name)
        if (
            table.name in NEVER_ACTIONED
            or classification is None
            or classification.principal is None
            or classification.default_category not in policies
            or not {"id", "tenant_id", "employee_id", "created_at"} <= set(table.c.keys())
        ):
            continue
        policy = policies[classification.default_category]
        cutoff = _months_before(today, policy.retention_period_months)
        rows = (
            await session.execute(
                select(table.c.id, table.c.employee_id, table.c.created_at)
                .where(
                    table.c.tenant_id == user.tenant_id,
                    table.c.created_at < datetime.combine(cutoff, time.min, tzinfo=UTC),
                )
                .limit(cap - len(out))
            )
        ).all()
        for record_id, employee_id, created_at in rows:
            emp = employees.get(employee_id)
            out.append(
                planner_engine.CandidateRecord(
                    record_id=str(record_id),
                    table=table.name,
                    data_category_key=classification.default_category,
                    subject_id=str(employee_id),
                    record_created=created_at.date(),
                    employment_ended=emp.date_of_exit if emp else None,
                    department=str(emp.department_id) if emp and emp.department_id else None,
                )
            )
    return out


async def apply_suppressions(
    session: AsyncSession, user: User, *, today: date, request: Request | None = None
) -> dict[str, Any]:
    """Remove any suppressed record that has come back — after a backup restore.

    This is the other half of the suppression list: writing it is a promise, and
    this is keeping it. Returns how many reappeared records were removed again.
    """
    rows = (
        await session.scalars(
            tenant_scoped(
                select(ErasureSuppression).where(ErasureSuppression.expires_on >= today),
                ErasureSuppression,
                user,
            )
        )
    ).all()
    tables = Base.metadata.tables
    removed = 0
    for suppression in rows:
        table = tables.get(suppression.record_table)
        if (
            table is None
            or suppression.record_table in NEVER_ACTIONED
            or "id" not in table.c
            or "tenant_id" not in table.c
        ):
            continue
        try:
            async with session.begin_nested():
                result = await session.execute(
                    delete(table).where(
                        table.c.tenant_id == user.tenant_id,
                        table.c.id == suppression.record_id,
                    )
                )
        except IntegrityError:
            continue
        removed += int(result.rowcount or 0)

    if removed:
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.DELETE,
            entity_type="erasure_suppression",
            actor=user,
            summary=(
                f"{removed} previously erased record(s) had reappeared — most likely a "
                "backup restore — and were removed again."
            ),
            request=request,
        )
    await session.commit()
    return {"suppressions_checked": len(rows), "reappeared_and_removed": removed}


async def scheduled_sweep(
    session: AsyncSession, user: User, *, today: date
) -> dict[str, Any]:
    """The nightly job: re-apply suppressions, then plan and (if small) execute.

    A plan above the approval threshold is written and left AWAITING_APPROVAL for
    a person — it appears in their approvals inbox. Nothing is recorded on a
    night with nothing due.
    """
    suppression = await apply_suppressions(session, user, today=today)
    candidates = await gather_candidates(session, user, today=today)
    if not candidates:
        return {"candidates": 0, "run_status": None, **suppression}

    preview = planner_engine.plan(
        candidates,
        policies=await _policies_for(session, user),
        legal_holds=_hold_scopes(await active_holds(session, user)),
        today=today,
        is_dry_run=True,
    )
    if not preview.to_act:
        return {"candidates": len(candidates), "run_status": None, "due": 0, **suppression}

    run = await plan_run(session, user, candidates=candidates, today=today, is_dry_run=False)
    if run.status is RetentionRunStatus.APPROVED:
        result = await execute_plan(session, user, run_id=run.id)
        return {
            "candidates": len(candidates),
            "run_status": "executed",
            "removed": result["records_actually_removed"],
            "anonymised": result["records_anonymised"],
            **suppression,
        }
    return {
        "candidates": len(candidates),
        "run_status": run.status.value,
        "to_act": run.records_to_act,
        **suppression,
    }


def _looks_like_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


# ══════════════════════════════════════════════════════════════════════════════
# Erasure suppression
# ══════════════════════════════════════════════════════════════════════════════


async def suppress_for_erasure(
    session: AsyncSession,
    user: User,
    *,
    subject_id: uuid.UUID,
    records: list[tuple[str, uuid.UUID]],
    source_request_id: uuid.UUID | None = None,
    request: Request | None = None,
    now: datetime | None = None,
    commit: bool = True,
) -> int:
    """Add records to the list a backup restore must not bring back.

    `commit=False` for a caller that owns the transaction — an erasure request
    being honoured writes its suppressions in the same unit of work as the
    deletion, and committing here would split the two.
    """
    if source_request_id is not None:
        exists = await session.get(DataPrincipalRequest, source_request_id)
        if exists is None or exists.tenant_id != user.tenant_id:
            raise _not_found("No such data-principal request.")

    moment = now or datetime.now(UTC)
    expires = (moment + timedelta(days=_SUPPRESSION_DAYS)).date()

    written = 0
    for table, record_id in records:
        session.add(
            ErasureSuppression(
                tenant_id=user.tenant_id,
                record_table=table,
                record_id=record_id,
                subject_id=subject_id,
                reason="Erased on the data principal's request.",
                suppressed_at=moment,
                expires_on=expires,
                source_request_id=source_request_id,
            )
        )
        written += 1

    await session.flush()
    if commit:
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.CREATE,
            entity_type="erasure_suppression",
            actor=user,
            summary=f"{written} record(s) suppressed against a backup restore.",
            request=request,
        )
        await session.commit()
    return written


async def suppression_list(
    session: AsyncSession, user: User, *, today: date | None = None
) -> dict[str, Any]:
    """What a restore would have to suppress, and the honest note about it."""
    as_of = today or datetime.now(UTC).date()
    stmt = tenant_scoped(
        select(ErasureSuppression).where(ErasureSuppression.expires_on >= as_of),
        ErasureSuppression,
        user,
    )
    rows = (await session.execute(stmt)).scalars().all()
    return {
        "active_suppressions": len(rows),
        "by_table": _count_by(row.record_table for row in rows),
        "backups": BACKUP_DISCLOSURE,
        "records": [
            {
                "table": row.record_table,
                "record_id": str(row.record_id),
                "suppressed_at": row.suppressed_at.isoformat(),
                "expires_on": row.expires_on.isoformat(),
                "reason": row.reason,
            }
            for row in rows[:200]
        ],
    }


def _count_by(values: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return out


async def trust_centre(session: AsyncSession, user: User) -> dict[str, Any]:
    """The retention half of the Trust Center.

    The taxonomy, the tenant's own configured policies, the open holds and the
    backup disclosure. Written to be read by somebody deciding whether to trust
    us with four hundred people's payroll.
    """
    holds = await active_holds(session, user)
    return {
        **trust_centre_taxonomy(),
        "your_policies": (await list_policies(session, user)),
        "open_legal_holds_count": len(holds),
        # A hold's reason can name a dispute. Everybody sees that holds exist;
        # HR sees why.
        "open_legal_holds": [] if not permissions.is_hr(user) else [
            {
                "reason": hold.reason,
                "placed_at": hold.placed_at.isoformat(),
                "scope": (
                    "the whole tenant"
                    if not (hold.employee_ids or hold.departments)
                    else {
                        "employees": len(hold.employee_ids or []),
                        "departments": hold.departments or [],
                    }
                ),
            }
            for hold in holds
        ],
        "not_built": [
            "The per-module purge is not implemented: a retention run records "
            "what would be removed and removes nothing.",
            "No scheduled job runs the sweep; the planner is callable and "
            "unscheduled.",
            "No restore procedure reads the suppression list, because no "
            "restore procedure exists in this codebase.",
        ],
    }
