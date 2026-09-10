"""The DB-facing half of retention. Sessions, models, transactions — nothing else.

The floor arithmetic, the anonymisation, the k-anonymity check and the planning
all live in the pure modules beside this, where a reviewer can read them without
reading SQLAlchemy. An invariant checks the isolation holds.

**Execution is deliberately narrow.** `execute` acts on one action kind for one
category at a time, inside one transaction, and writes its log row in the same
transaction as the change. A single function that swept every category and
committed as it went could leave a run half-applied with no way to tell which
half — and the half it deleted is not coming back.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.dpdp import DataPrincipalRequest
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
from app.services.retention import planner as planner_engine
from app.services.retention import policy as policy_engine
from app.services.retention.taxonomy import BACKUP_DISCLOSURE, trust_centre_taxonomy

__all__ = [
    "active_holds",
    "execute_plan",
    "place_hold",
    "plan_run",
    "release_hold",
    "seed_default_policies",
    "set_policy",
    "suppress_for_erasure",
    "trust_centre",
]

#: How long a suppression has to outlive the deletion: the backup retention
#: window, plus a week of slack for a restore that starts on the last day.
_SUPPRESSION_DAYS = BACKUP_DISCLOSURE["retention_window_days"] + 7


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
        plan_snapshot=computed.as_dict(),
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
    """Record the execution of an approved run.

    **What this does and does not do, stated rather than implied.** It checks
    the run is executable, writes an insert-only `RetentionActionLog` per
    category and action with hashed sample ids, adds a suppression entry for
    every deleted record, and marks the run executed — all in one transaction
    with the audit row.

    **It does not delete or anonymise any application row.** The per-module
    purge — which table, which cascade, which foreign key to null rather than
    follow — is not written. A function that claimed to have deleted records it
    had not touched would be the single most dangerous lie in this codebase, so
    the response says `records_actually_removed: 0` and the BUILD_LOG says it
    too.
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

    sample = row.plan_snapshot.get("sample", [])
    by_group: dict[tuple[str, str, str], list[str]] = {}
    for entry in sample:
        key = (entry["category"], entry["table"], entry["action"])
        by_group.setdefault(key, []).append(entry["record_id"])

    logs: list[RetentionActionLog] = []
    suppressions = 0
    for (category_key, table, action_value), record_ids in by_group.items():
        logs.append(
            RetentionActionLog(
                tenant_id=user.tenant_id,
                run_id=row.id,
                data_category_key=category_key,
                record_type=table,
                action=RetentionActionKind(action_value),
                record_count=len(record_ids),
                executed_at=moment,
                approved_by_user_id=row.approved_by_user_id,
                sample_record_hashes=[
                    _hash_record_id(rid, tenant_id=user.tenant_id)
                    for rid in record_ids[:20]
                ],
                policy_snapshot={"category": category_key, "action": action_value},
            )
        )
        if action_value == RetentionActionKind.DELETE.value:
            for record_id in record_ids:
                session.add(
                    ErasureSuppression(
                        tenant_id=user.tenant_id,
                        record_table=table,
                        record_id=uuid.UUID(record_id)
                        if _looks_like_uuid(record_id)
                        else uuid.uuid5(uuid.NAMESPACE_OID, record_id),
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
            f"Retention run executed: {len(logs)} action log(s), "
            f"{suppressions} suppression(s). No application rows removed — the "
            "per-module purge is not built."
        ),
        request=request,
    )
    await session.commit()

    return {
        "run_id": str(row.id),
        "action_logs_written": len(logs),
        "suppressions_written": suppressions,
        "records_actually_removed": 0,
        "not_built": (
            "The per-module purge is not implemented. This run recorded what "
            "would be removed and suppressed the identifiers against a restore; "
            "no application row was deleted or anonymised."
        ),
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
        "open_legal_holds": [
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
