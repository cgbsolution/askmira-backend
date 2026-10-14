"""Slice 26 — retention against a real database.

The checklist gates, each as one test:

- a tenant cannot configure below the statutory floor
- a legal hold overrides every policy
- bulk actions are gated on approval above a threshold
- a dry run affects nothing and reports accurately
- the retention action log is immutable and stores hashes
- the erasure suppression list is written and survives
- the backup position is disclosed honestly in the Trust Center
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.models.retention import (
    ErasureSuppression,
    LegalHold,
    RetentionActionKind,
    RetentionActionLog,
    RetentionPolicyRow,
    RetentionRun,
    RetentionRunStatus,
)
from app.services.dpdp.categories import CATEGORIES
from app.services.retention import planner as planner_engine
from app.services.retention import service as retention
from tests.integration.conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]

NOW = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
TODAY = NOW.date()


def _candidates(count: int, *, category: str = "leave", subject_prefix: str = "e"):
    return [
        planner_engine.CandidateRecord(
            record_id=str(uuid.uuid4()),
            table="leave_requests",
            data_category_key=category,
            subject_id=f"{subject_prefix}{i}",
            record_created=date(2015, 1, 1),
            department="Engineering",
        )
        for i in range(count)
    ]


async def _real(session, employee, count: int) -> list:
    """Rows that exist, for the tests that execute a plan.

    Execution acts on records that are really there and reports only what it
    did, so a plan built from invented ids executes to nothing. Shoutouts are
    culture responses, whose default action is DELETE.
    """
    from app.models.culture import Shoutout

    rows = [
        Shoutout(
            tenant_id=employee.tenant_id, from_employee_id=employee.id,
            to_employee_id=employee.id, message=f"Thanks #{i}",
        )
        for i in range(count)
    ]
    session.add_all(rows)
    await session.commit()
    return [
        planner_engine.CandidateRecord(
            record_id=str(row.id),
            table="shoutouts",
            data_category_key="culture_response",
            subject_id=str(employee.id),
            record_created=date(2015, 1, 1),
            department="Engineering",
        )
        for row in rows
    ]


# ══════════════════════════════════════════════════════════════════════════════
# Gate: the statutory floor
# ══════════════════════════════════════════════════════════════════════════════


async def test_a_tenant_cannot_configure_below_the_statutory_minimum(
    session, hr_user
):
    with pytest.raises(HTTPException) as exc:
        await retention.set_policy(
            session,
            hr_user,
            data_category_key="compensation",
            retention_period_months=24,
        )
    assert exc.value.status_code == 400
    assert "96 months" in exc.value.detail
    assert "not a setting AskMira can unlock" in exc.value.detail

    count = (
        await session.execute(select(func.count()).select_from(RetentionPolicyRow))
    ).scalar_one()
    assert count == 0, "A refused policy still wrote a row."


async def test_a_tenant_can_configure_above_the_minimum(session, hr_user):
    row = await retention.set_policy(
        session,
        hr_user,
        data_category_key="compensation",
        retention_period_months=120,
    )
    assert row.retention_period_months == 120
    assert row.statutory_minimum_months == 96
    assert row.legal_basis


async def test_the_defaults_are_the_floor_and_cover_every_category(session, hr_user):
    written = await retention.seed_default_policies(session, hr_user)
    assert len(written) == len(CATEGORIES)

    listing = await retention.list_policies(session, hr_user)
    assert listing["unconfigured_categories"] == []
    for entry in listing["policies"]:
        cat = CATEGORIES[entry["category"]]
        assert entry["retention_period_months"] == cat.retention.months


async def test_seeding_twice_does_not_overwrite_a_configured_policy(
    session, hr_user
):
    await retention.set_policy(
        session,
        hr_user,
        data_category_key="compensation",
        retention_period_months=120,
    )
    await retention.seed_default_policies(session, hr_user)

    stmt = select(RetentionPolicyRow).where(
        RetentionPolicyRow.data_category_key == "compensation"
    )
    row = (await session.execute(stmt)).scalar_one()
    assert row.retention_period_months == 120


async def test_unconfigured_categories_are_reported_not_hidden(session, hr_user):
    await retention.set_policy(
        session,
        hr_user,
        data_category_key="culture_response",
        retention_period_months=12,
    )
    listing = await retention.list_policies(session, hr_user)
    assert "compensation" in listing["unconfigured_categories"]
    assert len(listing["policies"]) == 1


async def test_policies_are_scoped_to_their_tenant(session, hr_user, other_hr_user):
    await retention.seed_default_policies(session, hr_user)
    theirs = await retention.list_policies(session, other_hr_user)
    assert theirs["policies"] == []
    assert len(theirs["unconfigured_categories"]) == len(CATEGORIES)


# ══════════════════════════════════════════════════════════════════════════════
# Gate: legal hold overrides every policy
# ══════════════════════════════════════════════════════════════════════════════


async def test_a_legal_hold_exempts_records_from_an_expired_policy(
    session, hr_user
):
    """The gate. Held is held, even against a period that ran out years ago."""
    await retention.seed_default_policies(session, hr_user)
    candidates = _candidates(4)

    before = await retention.plan_run(
        session, hr_user, candidates=candidates, today=TODAY
    )
    assert before.records_to_act == 4
    assert before.records_held == 0

    await retention.place_hold(
        session,
        hr_user,
        reason="Employment tribunal claim filed by a former engineer.",
        employee_ids=[],
        departments=["Engineering"],
        now=NOW,
    )

    after = await retention.plan_run(
        session, hr_user, candidates=candidates, today=TODAY
    )
    assert after.records_to_act == 0
    assert after.records_held == 4


async def test_releasing_a_hold_lets_the_policy_run_again(session, hr_user):
    await retention.seed_default_policies(session, hr_user)
    candidates = _candidates(3)

    hold = await retention.place_hold(
        session,
        hr_user,
        reason="Investigation.",
        departments=["Engineering"],
        now=NOW,
    )
    held = await retention.plan_run(
        session, hr_user, candidates=candidates, today=TODAY
    )
    assert held.records_held == 3

    await retention.release_hold(
        session,
        hr_user,
        hold_id=hold.id,
        release_reason="Matter closed; nothing further required.",
        now=NOW,
    )
    freed = await retention.plan_run(
        session, hr_user, candidates=candidates, today=TODAY
    )
    assert freed.records_to_act == 3
    assert freed.records_held == 0


async def test_a_released_hold_is_kept_as_evidence_not_deleted(session, hr_user):
    hold = await retention.place_hold(
        session, hr_user, reason="Investigation.", now=NOW
    )
    await retention.release_hold(
        session, hr_user, hold_id=hold.id, release_reason="Closed.", now=NOW
    )
    rows = (await session.execute(select(LegalHold))).scalars().all()
    assert len(rows) == 1
    assert rows[0].released_at is not None
    assert rows[0].release_reason == "Closed."
    assert not rows[0].is_active


async def test_a_hold_with_no_scope_freezes_the_whole_tenant(session, hr_user):
    """The broadest possible hold must not read as no hold at all."""
    await retention.seed_default_policies(session, hr_user)
    await retention.place_hold(
        session,
        hr_user,
        reason="Regulator has frozen all processing pending an inquiry.",
        now=NOW,
    )
    result = await retention.plan_run(
        session,
        hr_user,
        candidates=_candidates(5, subject_prefix="anyone"),
        today=TODAY,
    )
    assert result.records_to_act == 0
    assert result.records_held == 5


async def test_a_hold_needs_a_reason(session, hr_user):
    with pytest.raises(HTTPException) as exc:
        await retention.place_hold(session, hr_user, reason="   ", now=NOW)
    assert "makes the whole retention policy decorative" in exc.value.detail


async def test_releasing_a_hold_needs_a_reason(session, hr_user):
    hold = await retention.place_hold(
        session, hr_user, reason="Investigation.", now=NOW
    )
    with pytest.raises(HTTPException) as exc:
        await retention.release_hold(
            session, hr_user, hold_id=hold.id, release_reason="", now=NOW
        )
    assert "somebody will ask who decided" in exc.value.detail


async def test_a_hold_cannot_be_released_twice(session, hr_user):
    hold = await retention.place_hold(
        session, hr_user, reason="Investigation.", now=NOW
    )
    await retention.release_hold(
        session, hr_user, hold_id=hold.id, release_reason="Closed.", now=NOW
    )
    with pytest.raises(HTTPException) as exc:
        await retention.release_hold(
            session, hr_user, hold_id=hold.id, release_reason="Again.", now=NOW
        )
    assert "already released" in exc.value.detail


async def test_a_hold_is_scoped_to_its_tenant(session, hr_user, other_hr_user):
    hold = await retention.place_hold(
        session, hr_user, reason="Acme only.", now=NOW
    )
    with pytest.raises(HTTPException) as exc:
        await retention.release_hold(
            session,
            other_hr_user,
            hold_id=hold.id,
            release_reason="Not theirs to release.",
            now=NOW,
        )
    assert exc.value.status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# Gate: dry run affects nothing; volume gates on approval
# ══════════════════════════════════════════════════════════════════════════════


async def test_a_dry_run_reports_accurately_and_cannot_execute(session, hr_user):
    await retention.seed_default_policies(session, hr_user)
    run = await retention.plan_run(
        session,
        hr_user,
        candidates=_candidates(6),
        today=TODAY,
        is_dry_run=True,
    )
    assert run.status is RetentionRunStatus.DRY_RUN
    assert run.records_evaluated == 6
    assert run.records_to_act == 6
    assert run.plan_snapshot["is_dry_run"] is True
    assert run.plan_snapshot["execute_ready"] is False

    with pytest.raises(HTTPException) as exc:
        await retention.execute_plan(session, hr_user, run_id=run.id)
    assert "produced to be read" in exc.value.detail

    logs = (
        await session.execute(select(func.count()).select_from(RetentionActionLog))
    ).scalar_one()
    assert logs == 0


async def test_a_run_above_the_threshold_waits_for_a_person(session, hr_user, employee):
    await retention.seed_default_policies(session, hr_user)
    run = await retention.plan_run(
        session,
        hr_user,
        candidates=await _real(session, employee, 10),
        today=TODAY,
        approval_threshold=5,
        is_dry_run=False,
    )
    assert run.status is RetentionRunStatus.AWAITING_APPROVAL

    with pytest.raises(HTTPException) as exc:
        await retention.execute_plan(session, hr_user, run_id=run.id)
    assert "above the approval threshold" in exc.value.detail
    assert "not a good surprise" in exc.value.detail

    approved = await retention.approve_run(
        session, hr_user, run_id=run.id, now=NOW
    )
    assert approved.status is RetentionRunStatus.APPROVED
    assert approved.approved_by_user_id == hr_user.id

    result = await retention.execute_plan(session, hr_user, run_id=run.id, now=NOW)
    assert result["action_logs_written"] >= 1


async def test_a_run_under_the_threshold_runs_without_approval(session, hr_user, employee):
    await retention.seed_default_policies(session, hr_user)
    run = await retention.plan_run(
        session,
        hr_user,
        candidates=await _real(session, employee, 2),
        today=TODAY,
        approval_threshold=100,
        is_dry_run=False,
    )
    assert run.status is RetentionRunStatus.APPROVED
    result = await retention.execute_plan(session, hr_user, run_id=run.id, now=NOW)
    assert result["action_logs_written"] >= 1


async def test_a_dry_run_cannot_be_approved(session, hr_user):
    await retention.seed_default_policies(session, hr_user)
    run = await retention.plan_run(
        session, hr_user, candidates=_candidates(2), today=TODAY, is_dry_run=True
    )
    with pytest.raises(HTTPException) as exc:
        await retention.approve_run(session, hr_user, run_id=run.id, now=NOW)
    assert "produced to be read" in exc.value.detail


async def test_a_rejected_run_cannot_execute(session, hr_user):
    await retention.seed_default_policies(session, hr_user)
    run = await retention.plan_run(
        session,
        hr_user,
        candidates=_candidates(10),
        today=TODAY,
        approval_threshold=5,
        is_dry_run=False,
    )
    await retention.reject_run(
        session,
        hr_user,
        run_id=run.id,
        reason="Too broad — the department filter caught two teams.",
    )
    with pytest.raises(HTTPException) as exc:
        await retention.execute_plan(session, hr_user, run_id=run.id)
    assert "rejected" in exc.value.detail


async def test_a_run_is_scoped_to_its_tenant(session, hr_user, other_hr_user):
    await retention.seed_default_policies(session, hr_user)
    run = await retention.plan_run(
        session, hr_user, candidates=_candidates(2), today=TODAY
    )
    with pytest.raises(HTTPException) as exc:
        await retention.execute_plan(session, other_hr_user, run_id=run.id)
    assert exc.value.status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# Gate: the action log
# ══════════════════════════════════════════════════════════════════════════════


async def test_the_action_log_records_who_authorised_it_and_hashes_the_ids(
    session, hr_user, employee
):
    await retention.seed_default_policies(session, hr_user)
    candidates = await _real(session, employee, 10)
    run = await retention.plan_run(
        session,
        hr_user,
        candidates=candidates,
        today=TODAY,
        approval_threshold=5,
        is_dry_run=False,
    )
    await retention.approve_run(session, hr_user, run_id=run.id, now=NOW)
    await retention.execute_plan(session, hr_user, run_id=run.id, now=NOW)

    logs = (await session.execute(select(RetentionActionLog))).scalars().all()
    assert logs
    plain_ids = {c.record_id for c in candidates}
    for log in logs:
        assert log.approved_by_user_id == hr_user.id
        assert log.record_count > 0
        assert log.sample_record_hashes
        for stored in log.sample_record_hashes:
            assert stored not in plain_ids, (
                "The action log stores a readable record id. A log of what was "
                "deleted containing identifiers of the deleted is a copy of it."
            )
            assert len(stored) == 32


async def test_the_action_log_survives_and_is_never_updated(session, hr_user, employee):
    """Insert-only: a second run adds rows rather than replacing them."""
    await retention.seed_default_policies(session, hr_user)
    for _ in range(2):
        run = await retention.plan_run(
            session,
            hr_user,
            candidates=await _real(session, employee, 2),
            today=TODAY,
            approval_threshold=100,
            is_dry_run=False,
        )
        await retention.execute_plan(session, hr_user, run_id=run.id, now=NOW)

    count = (
        await session.execute(select(func.count()).select_from(RetentionActionLog))
    ).scalar_one()
    assert count == 2


async def test_execution_claims_only_what_it_actually_removed(session, hr_user, employee):
    """The honest half. A planned record that is already gone is not counted as removed."""
    await retention.seed_default_policies(session, hr_user)
    there = await _real(session, employee, 2)
    gone = [
        planner_engine.CandidateRecord(
            record_id=str(uuid.uuid4()), table="shoutouts", data_category_key="culture_response",
            subject_id=str(employee.id), record_created=date(2015, 1, 1),
            department="Engineering",
        )
    ]
    run = await retention.plan_run(
        session,
        hr_user,
        candidates=[*there, *gone],
        today=TODAY,
        approval_threshold=100,
        is_dry_run=False,
    )
    result = await retention.execute_plan(session, hr_user, run_id=run.id, now=NOW)
    assert result["records_actually_removed"] == 2
    assert "archive" in result["not_built"], "what is not done is said, not implied"


# ══════════════════════════════════════════════════════════════════════════════
# Gate: erasure suppression against a backup restore
# ══════════════════════════════════════════════════════════════════════════════


async def test_a_deletion_writes_a_suppression_entry(session, hr_user, employee):
    await retention.seed_default_policies(session, hr_user)
    # culture_response is consent-based, so its action is DELETE.
    run = await retention.plan_run(
        session,
        hr_user,
        candidates=await _real(session, employee, 3),
        today=TODAY,
        approval_threshold=100,
        is_dry_run=False,
    )
    result = await retention.execute_plan(session, hr_user, run_id=run.id, now=NOW)
    assert result["suppressions_written"] == 3

    listing = await retention.suppression_list(session, hr_user, today=TODAY)
    assert listing["active_suppressions"] == 3
    assert listing["backups"]["retention_window_days"] > 0


async def test_an_anonymisation_writes_no_suppression(session, hr_user):
    """Anonymised rows are not deleted, so there is nothing to keep out of a restore."""
    await retention.seed_default_policies(session, hr_user)
    run = await retention.plan_run(
        session,
        hr_user,
        candidates=_candidates(3, category="leave"),
        today=TODAY,
        approval_threshold=100,
        is_dry_run=False,
    )
    result = await retention.execute_plan(session, hr_user, run_id=run.id, now=NOW)
    assert result["suppressions_written"] == 0
    assert run.counts_by_action == {RetentionActionKind.ANONYMISE.value: 3}


async def test_a_suppression_outlives_the_backup_window(session, hr_user, employee):
    written = await retention.suppress_for_erasure(
        session,
        hr_user,
        subject_id=employee.id,
        records=[("employee_documents", uuid.uuid4())],
        now=NOW,
    )
    assert written == 1

    row = (await session.execute(select(ErasureSuppression))).scalars().one()
    window = (row.expires_on - NOW.date()).days
    assert window > 30, (
        "A suppression that expires inside the backup window would stop "
        "suppressing while a restore could still bring the record back."
    )


async def test_a_suppression_is_scoped_to_its_tenant(
    session, hr_user, other_hr_user, employee
):
    await retention.suppress_for_erasure(
        session,
        hr_user,
        subject_id=employee.id,
        records=[("employee_documents", uuid.uuid4())],
        now=NOW,
    )
    theirs = await retention.suppression_list(session, other_hr_user, today=TODAY)
    assert theirs["active_suppressions"] == 0


async def test_a_suppression_from_an_unknown_request_is_refused(
    session, hr_user, employee
):
    with pytest.raises(HTTPException) as exc:
        await retention.suppress_for_erasure(
            session,
            hr_user,
            subject_id=employee.id,
            records=[("employee_documents", uuid.uuid4())],
            source_request_id=uuid.uuid4(),
            now=NOW,
        )
    assert exc.value.status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# Gate: the Trust Center is honest
# ══════════════════════════════════════════════════════════════════════════════


async def test_the_trust_centre_states_the_conflict_and_the_gaps(session, hr_user):
    await retention.seed_default_policies(session, hr_user)
    page = await retention.trust_centre(session, hr_user)

    assert "Both are binding" in page["the_conflict"]
    assert "not deletion" in page["anonymisation_is_not_deletion"]
    assert page["backups"]["what_is_not_true"]
    assert page["not_built"], (
        "The retention Trust Center no longer lists what is not built. A page of "
        "green ticks is the invented reassurance this codebase refuses."
    )
    assert any("purge is not implemented" in gap for gap in page["not_built"])
    assert any("restore procedure" in gap for gap in page["not_built"])


async def test_the_trust_centre_shows_open_holds(session, hr_user):
    await retention.place_hold(
        session,
        hr_user,
        reason="Employment tribunal claim.",
        departments=["Engineering"],
        now=NOW,
    )
    page = await retention.trust_centre(session, hr_user)
    assert len(page["open_legal_holds"]) == 1
    assert page["open_legal_holds"][0]["reason"] == "Employment tribunal claim."


async def test_a_whole_tenant_hold_is_described_as_such(session, hr_user):
    await retention.place_hold(
        session, hr_user, reason="Regulator inquiry.", now=NOW
    )
    page = await retention.trust_centre(session, hr_user)
    assert page["open_legal_holds"][0]["scope"] == "the whole tenant"


async def test_the_run_history_is_readable(session, hr_user):
    await retention.seed_default_policies(session, hr_user)
    await retention.plan_run(
        session, hr_user, candidates=_candidates(4), today=TODAY
    )
    rows = (await session.execute(select(RetentionRun))).scalars().all()
    assert len(rows) == 1
    assert rows[0].records_evaluated == 4
    assert rows[0].plan_snapshot["counts_by_action"]
