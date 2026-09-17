"""The retention run really deletes eligible records, spares held ones, and
re-applies its suppressions after a restore."""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    EmployeeDocument,
    ErasureSuppression,
    RetentionActionKind,
    RetentionActionLog,
    RetentionAnchor,
    RetentionPolicyRow,
    RetentionRunStatus,
    Tenant,
    UserRole,
)
from app.services.retention import service as retention
from tests.integration import platform_support
from tests.integration.conftest import requires_db
from tests.integration.platform_support import make_person

pytestmark = requires_db

client = platform_support.client

TODAY = date(2026, 9, 17)
OLD = datetime(2020, 1, 15, tzinfo=UTC)


async def _setup(session: AsyncSession, tenant: Tenant):
    admin = await make_person(session, tenant, role=UserRole.HR_ADMIN, name="Asha Admin")
    kept = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Held Person")
    gone = await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Free Person")
    session.add(
        RetentionPolicyRow(
            tenant_id=tenant.id,
            data_category_key="documents",
            retention_period_months=12,
            anchor_event=RetentionAnchor.RECORD_CREATED,
            action=RetentionActionKind.DELETE,
            statutory_minimum_months=0,
            legal_basis="Test policy",
            active=True,
        )
    )
    docs = {}
    for person in (kept, gone):
        doc = EmployeeDocument(
            tenant_id=tenant.id,
            employee_id=person.employee_id,
            category="other",
            title="Old scan",
            file_url="documents/old-scan",
            created_at=OLD,
        )
        session.add(doc)
        docs[person.full_name] = doc
    recent = EmployeeDocument(
        tenant_id=tenant.id,
        employee_id=gone.employee_id,
        category="other",
        title="New scan",
        file_url="documents/new-scan",
    )
    session.add(recent)
    await session.commit()
    return admin, kept, gone, docs, recent


async def _doc_ids(session: AsyncSession) -> set:
    return set((await session.scalars(select(EmployeeDocument.id))).all())


async def test_a_run_deletes_eligible_records_and_spares_held_ones(session, tenant):
    admin, kept, gone, docs, recent = await _setup(session, tenant)
    await retention.place_hold(
        session, admin, reason="Pending litigation", employee_ids=[kept.employee_id]
    )

    candidates = await retention.gather_candidates(session, admin, today=TODAY)
    assert {c.record_id for c in candidates} == {
        str(docs["Held Person"].id),
        str(docs["Free Person"].id),
    }, "a record younger than the policy period is never a candidate"

    run = await retention.plan_run(
        session, admin, candidates=candidates, today=TODAY, is_dry_run=False
    )
    assert run.status is RetentionRunStatus.APPROVED
    assert "to_act_records" not in retention.public_snapshot(run.plan_snapshot)

    result = await retention.execute_plan(session, admin, run_id=run.id)
    assert result["records_actually_removed"] == 1

    remaining = await _doc_ids(session)
    assert docs["Held Person"].id in remaining
    assert docs["Free Person"].id not in remaining
    assert recent.id in remaining

    logs = (await session.scalars(select(RetentionActionLog))).all()
    assert [log.record_count for log in logs] == [1]
    suppressed = (await session.scalars(select(ErasureSuppression))).all()
    assert [s.record_id for s in suppressed] == [docs["Free Person"].id]


async def test_a_hold_placed_after_planning_still_spares_the_record(session, tenant):
    admin, kept, gone, docs, recent = await _setup(session, tenant)
    candidates = await retention.gather_candidates(session, admin, today=TODAY)
    run = await retention.plan_run(
        session, admin, candidates=candidates, today=TODAY, is_dry_run=False
    )
    await retention.place_hold(
        session, admin, reason="Investigation opened", employee_ids=[gone.employee_id]
    )
    result = await retention.execute_plan(session, admin, run_id=run.id)
    assert result["records_actually_removed"] == 1
    assert result["records_spared_by_legal_hold"] == 1
    remaining = await _doc_ids(session)
    assert docs["Free Person"].id in remaining
    assert docs["Held Person"].id not in remaining


async def test_a_dry_run_changes_nothing(session, tenant):
    admin, kept, gone, docs, recent = await _setup(session, tenant)
    before = await _doc_ids(session)
    candidates = await retention.gather_candidates(session, admin, today=TODAY)
    run = await retention.plan_run(session, admin, candidates=candidates, today=TODAY)
    assert run.status is RetentionRunStatus.DRY_RUN
    assert run.records_to_act == 2
    assert await _doc_ids(session) == before


async def test_the_sweep_executes_and_the_suppression_list_is_reapplied(session, tenant):
    admin, kept, gone, docs, recent = await _setup(session, tenant)
    outcome = await retention.scheduled_sweep(session, admin, today=TODAY)
    assert outcome["run_status"] == "executed"
    assert outcome["removed"] == 2

    # A backup restore brings a deleted row back.
    deleted_id = docs["Free Person"].id
    session.add(
        EmployeeDocument(
            id=deleted_id,
            tenant_id=tenant.id,
            employee_id=gone.employee_id,
            category="other",
            title="Old scan",
            file_url="documents/old-scan",
            created_at=OLD,
        )
    )
    await session.commit()
    reapplied = await retention.apply_suppressions(session, admin, today=TODAY)
    assert reapplied["reappeared_and_removed"] == 1
    assert deleted_id not in await _doc_ids(session)


async def test_a_large_run_waits_for_approval(session, tenant):
    admin, *_ = await _setup(session, tenant)
    candidates = await retention.gather_candidates(session, admin, today=TODAY)
    run = await retention.plan_run(
        session,
        admin,
        candidates=candidates,
        today=TODAY,
        approval_threshold=1,
        is_dry_run=False,
    )
    assert run.status is RetentionRunStatus.AWAITING_APPROVAL
    count = await session.scalar(select(func.count(EmployeeDocument.id)))
    assert count == 3
