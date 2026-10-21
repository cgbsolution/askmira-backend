"""Slice 30 — operational security against a real database.

The checklist gates:

- RPO and RTO published, and a timed restore drill has met them
- restore test recorded, with results
- DR region satisfies India residency for tenants configured that way
- payroll-deadline-during-incident runbook written
- pen test scope includes tenant isolation and the agent/MCP surface
- SIEM delivery failure alerts — tested by breaking delivery deliberately
- step-up auth on disbursement release, payroll rule change and bulk export
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.models.opsec import (
    DataTierEnum,
    DeliveryStateEnum,
    DrillOutcomeEnum,
    FindingSeverityEnum,
    PenTestFinding,
    RestoreDrill,
    SiemDelivery,
    SiemVendorEnum,
    StepUpChallenge,
    UserSession,
)
from app.services.opsec import access, continuity, pentest
from app.services.opsec import service as opsec
from tests.integration.conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
TODAY = NOW.date()


async def _destination(session, user, *, name="soc", interval=60):
    return await opsec.configure_destination(
        session,
        user,
        name=name,
        vendor=SiemVendorEnum.SPLUNK,
        endpoint_url="https://splunk.acme.test/services/collector",
        expected_interval_minutes=interval,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Gate: RPO/RTO published, and a timed drill has met them
# ══════════════════════════════════════════════════════════════════════════════


async def test_the_posture_starts_unevidenced_and_says_so(session, hr_user):
    """An untested backup is not a backup."""
    posture = await opsec.posture(session, hr_user, today=TODAY)
    tiers = posture["continuity"]["tiers"]
    assert tiers
    for tier in tiers:
        assert tier["rpo_minutes"] > 0
        assert tier["rto_minutes"] > 0
        assert not tier["evidence"]["is_evidenced"]
        assert "NOT currently backed by evidence" in tier["evidence"]["statement"]


async def test_a_timed_drill_that_meets_the_objectives_evidences_the_tier(session, hr_user):
    """The gate: a timed restore drill measured against the published RTO."""
    target = continuity.TIERS[continuity.DataTier.CRITICAL]

    drill = await opsec.record_drill(
        session,
        hr_user,
        tier=DataTierEnum.CRITICAL,
        performed_on=TODAY - timedelta(days=3),
        measured_rto_minutes=target.rto_minutes - 18,
        measured_rpo_minutes=target.rpo_minutes - 2,
        succeeded=True,
        notes="Point-in-time restore from WAL into a scratch instance.",
    )
    assert drill.outcome is DrillOutcomeEnum.MET
    assert drill.published_rto_minutes == target.rto_minutes

    posture = await opsec.posture(session, hr_user, today=TODAY)
    critical = next(t for t in posture["continuity"]["tiers"] if t["tier"] == "critical")
    assert critical["evidence"]["is_evidenced"]
    assert critical["evidence"]["last_outcome"] == "met"


async def test_a_drill_slower_than_the_rto_is_recorded_as_a_breach(session, hr_user):
    target = continuity.TIERS[continuity.DataTier.CRITICAL]
    drill = await opsec.record_drill(
        session,
        hr_user,
        tier=DataTierEnum.CRITICAL,
        performed_on=TODAY,
        measured_rto_minutes=target.rto_minutes + 40,
        measured_rpo_minutes=1,
        succeeded=True,
    )
    assert drill.outcome is DrillOutcomeEnum.EXCEEDED_RTO

    posture = await opsec.posture(session, hr_user, today=TODAY)
    critical = next(t for t in posture["continuity"]["tiers"] if t["tier"] == "critical")
    assert not critical["evidence"]["is_evidenced"], (
        "A tier whose latest drill breached its RTO is being reported as evidenced."
    )


async def test_the_drill_stores_the_published_targets_of_the_day(session, hr_user):
    """Revising a number later must not turn a past breach into a pass."""
    await opsec.record_drill(
        session,
        hr_user,
        tier=DataTierEnum.CORE,
        performed_on=TODAY,
        measured_rto_minutes=10,
        measured_rpo_minutes=1,
        succeeded=True,
    )
    row = (await session.execute(select(RestoreDrill))).scalars().one()
    assert row.published_rto_minutes == continuity.TIERS[continuity.DataTier.CORE].rto_minutes


async def test_a_negative_measurement_is_refused(session, hr_user):
    with pytest.raises(HTTPException):
        await opsec.record_drill(
            session,
            hr_user,
            tier=DataTierEnum.CRITICAL,
            performed_on=TODAY,
            measured_rto_minutes=-1,
            measured_rpo_minutes=0,
            succeeded=True,
        )
    count = (await session.execute(select(func.count()).select_from(RestoreDrill))).scalar_one()
    assert count == 0


async def test_drills_are_scoped_to_their_tenant(session, hr_user, other_hr_user):
    await opsec.record_drill(
        session,
        hr_user,
        tier=DataTierEnum.CRITICAL,
        performed_on=TODAY,
        measured_rto_minutes=10,
        measured_rpo_minutes=1,
        succeeded=True,
    )
    theirs = await opsec.posture(session, other_hr_user, today=TODAY)
    critical = next(t for t in theirs["continuity"]["tiers"] if t["tier"] == "critical")
    assert not critical["evidence"]["is_evidenced"]


# ══════════════════════════════════════════════════════════════════════════════
# Gate: residency and the payroll runbook
# ══════════════════════════════════════════════════════════════════════════════


async def test_the_posture_states_the_dr_region_is_in_india(session, hr_user):
    posture = await opsec.posture(session, hr_user, today=TODAY)
    statement = posture["continuity"]["residency_statement"]
    assert "India" in statement
    assert "cross-border" in statement


async def test_the_payroll_deadline_runbook_is_published(session, hr_user):
    posture = await opsec.posture(session, hr_user, today=TODAY)
    runbooks = {r["key"]: r for r in posture["continuity"]["runbooks"]}
    payroll = runbooks["payroll_deadline_during_incident"]
    assert payroll["manual_fallback"]
    assert payroll["severity"] == "sev1"
    assert payroll["steps"]
    assert not payroll["reviewed"], "The runbook claims to have been reviewed. Nobody has read it."


# ══════════════════════════════════════════════════════════════════════════════
# Gate: SIEM delivery failure alerts, tested by breaking delivery
# ══════════════════════════════════════════════════════════════════════════════


async def test_a_broken_delivery_raises_an_alert(session, hr_user):
    """The gate: break delivery deliberately and check somebody is told."""
    destination = await _destination(session, hr_user)

    await opsec.record_delivery(
        session,
        hr_user,
        destination_id=destination.id,
        succeeded=True,
        event_count=12,
        now=NOW - timedelta(minutes=10),
    )
    healthy = await opsec.siem_health(session, hr_user, now=NOW)
    assert not healthy["alerting"]

    for offset in (5, 4, 3):
        await opsec.record_delivery(
            session,
            hr_user,
            destination_id=destination.id,
            succeeded=False,
            error="connection refused",
            now=NOW - timedelta(minutes=offset),
        )

    broken = await opsec.siem_health(session, hr_user, now=NOW)
    assert broken["alerting"], "A failing destination raised no alert."
    entry = broken["alerting"][0]
    assert entry["state"] == "failing"
    assert entry["consecutive_failures"] == 3
    assert "gap in their AskMira events" in entry["message"]

    await session.refresh(destination)
    assert destination.health_state is DeliveryStateEnum.FAILING


async def test_a_silent_destination_alerts_even_with_no_errors(session, hr_user):
    """The dangerous state: a SOC with a dead feed believes nothing happened."""
    destination = await _destination(session, hr_user, interval=60)
    await opsec.record_delivery(
        session,
        hr_user,
        destination_id=destination.id,
        succeeded=True,
        event_count=5,
        now=NOW - timedelta(hours=30),
    )

    health = await opsec.siem_health(session, hr_user, now=NOW)
    assert health["alerting"]
    assert health["alerting"][0]["state"] == "silent"
    assert "dangerous state" in health["alerting"][0]["message"]


async def test_a_recovered_destination_stops_alerting(session, hr_user):
    destination = await _destination(session, hr_user)
    await opsec.record_delivery(
        session,
        hr_user,
        destination_id=destination.id,
        succeeded=False,
        error="timeout",
        now=NOW - timedelta(minutes=20),
    )
    assert (await opsec.siem_health(session, hr_user, now=NOW))["alerting"]

    await opsec.record_delivery(
        session,
        hr_user,
        destination_id=destination.id,
        succeeded=True,
        event_count=40,
        now=NOW - timedelta(minutes=1),
    )
    assert not (await opsec.siem_health(session, hr_user, now=NOW))["alerting"]

    await session.refresh(destination)
    assert destination.consecutive_failures == 0


async def test_successes_are_recorded_not_only_failures(session, hr_user):
    """Health is measured on silence, which a table of failures cannot show."""
    destination = await _destination(session, hr_user)
    await opsec.record_delivery(
        session, hr_user, destination_id=destination.id, succeeded=True, now=NOW
    )
    rows = (await session.execute(select(SiemDelivery))).scalars().all()
    assert len(rows) == 1
    assert rows[0].succeeded


async def test_a_plaintext_endpoint_is_refused(session, hr_user):
    with pytest.raises(HTTPException) as exc:
        await opsec.configure_destination(
            session,
            hr_user,
            name="insecure",
            vendor=SiemVendorEnum.GENERIC_HTTPS,
            endpoint_url="http://soc.acme.test/ingest",
        )
    assert "second copy of everything" in exc.value.detail


async def test_the_event_preview_uses_the_documented_schema(session, hr_user):
    """A SIEM engineer asking "what will arrive" gets real events."""
    from app.services.opsec.siem import ECS_FIELDS

    await _destination(session, hr_user)  # produces an audit row
    preview = await opsec.preview_events(session, hr_user, limit=10)

    assert preview["events"], "no audit rows mapped"
    for event in preview["events"]:
        assert set(event) <= ECS_FIELDS
        assert event["service.name"] == "askmira"
        assert all(value is not None for value in event.values())
    assert preview["unmapped_actions"] == []
    assert "Elastic Common Schema" in preview["schema"]["schema"]


async def test_siem_destinations_are_scoped_to_their_tenant(session, hr_user, other_hr_user):
    await _destination(session, hr_user)
    theirs = await opsec.siem_health(session, other_hr_user, now=NOW)
    assert theirs["destinations"] == []


async def test_recording_a_delivery_to_another_tenants_destination_is_refused(
    session, hr_user, other_hr_user
):
    destination = await _destination(session, hr_user)
    with pytest.raises(HTTPException) as exc:
        await opsec.record_delivery(
            session,
            other_hr_user,
            destination_id=destination.id,
            succeeded=True,
            now=NOW,
        )
    assert exc.value.status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# Gate: step-up auth on the three high-impact actions
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "action_key",
    ["disbursement.release", "payroll.rule_change", "data.bulk_export"],
)
async def test_step_up_is_required_on_each_high_impact_action(session, hr_user, action_key):
    """Tested per action, as the checklist asks."""
    result = await opsec.require_step_up(session, hr_user, action_key=action_key, now=NOW)
    assert result["required"]
    assert not result["satisfied"]
    assert result["challenge_id"]
    assert result["reason"].strip()


async def test_an_ordinary_action_needs_no_step_up(session, hr_user):
    result = await opsec.require_step_up(session, hr_user, action_key="leave.apply", now=NOW)
    assert not result["required"]

    count = (await session.execute(select(func.count()).select_from(StepUpChallenge))).scalar_one()
    assert count == 0, "A challenge was opened for an action that needs none."


async def test_a_satisfied_challenge_makes_the_next_check_fresh(session, hr_user):
    row = await opsec.start_session(
        session, hr_user, token_hash="h1", ip_address="203.0.113.4", now=NOW
    )
    opened = await opsec.require_step_up(
        session,
        hr_user,
        action_key="disbursement.release",
        session_id=row.id,
        now=NOW,
    )
    await opsec.satisfy_challenge(
        session,
        hr_user,
        challenge_id=uuid.UUID(opened["challenge_id"]),
        succeeded=True,
        now=NOW,
    )

    again = await opsec.require_step_up(
        session,
        hr_user,
        action_key="disbursement.release",
        session_id=row.id,
        now=NOW + timedelta(minutes=2),
    )
    assert again["satisfied"]


async def test_a_stale_verification_does_not_satisfy_a_later_check(session, hr_user):
    """The window is short on purpose: "the person at the keyboard right now"."""
    row = await opsec.start_session(session, hr_user, token_hash="h1", now=NOW)
    opened = await opsec.require_step_up(
        session,
        hr_user,
        action_key="disbursement.release",
        session_id=row.id,
        now=NOW,
    )
    await opsec.satisfy_challenge(
        session,
        hr_user,
        challenge_id=uuid.UUID(opened["challenge_id"]),
        succeeded=True,
        now=NOW,
    )

    later = await opsec.require_step_up(
        session,
        hr_user,
        action_key="disbursement.release",
        session_id=row.id,
        now=NOW + access.STEP_UP_WINDOW + timedelta(minutes=1),
    )
    assert not later["satisfied"]


async def test_a_failed_challenge_is_recorded(session, hr_user):
    """A run of failures against a disbursement release is the SOC's signal."""
    opened = await opsec.require_step_up(
        session, hr_user, action_key="disbursement.release", now=NOW
    )
    for _ in range(3):
        await opsec.satisfy_challenge(
            session,
            hr_user,
            challenge_id=uuid.UUID(opened["challenge_id"]),
            succeeded=False,
            now=NOW,
        )
    row = (await session.execute(select(StepUpChallenge))).scalars().one()
    assert row.failed_attempts == 3
    assert row.satisfied_at is None


async def test_a_challenge_is_scoped_to_its_tenant(session, hr_user, other_hr_user):
    opened = await opsec.require_step_up(session, hr_user, action_key="data.bulk_export", now=NOW)
    with pytest.raises(HTTPException) as exc:
        await opsec.satisfy_challenge(
            session,
            other_hr_user,
            challenge_id=uuid.UUID(opened["challenge_id"]),
            succeeded=True,
            now=NOW,
        )
    assert exc.value.status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# Sessions
# ══════════════════════════════════════════════════════════════════════════════


async def test_a_session_policy_whose_timeout_exceeds_its_lifetime_is_refused(session, hr_user):
    with pytest.raises(HTTPException) as exc:
        await opsec.configure_sessions(
            session,
            hr_user,
            idle_timeout_minutes=1000,
            absolute_lifetime_hours=1,
        )
    assert "can never fire" in exc.value.detail


async def test_a_malformed_allowlist_entry_is_refused_at_configuration(session, hr_user):
    """A malformed CIDR discovered at login is a lockout; here it is a form error."""
    with pytest.raises(HTTPException) as exc:
        await opsec.configure_sessions(
            session,
            hr_user,
            idle_timeout_minutes=60,
            absolute_lifetime_hours=12,
            ip_allowlist=["10.0.0.0/33"],
        )
    assert "believing their office range is protected" in exc.value.detail


async def test_sessions_are_listed_with_the_device_and_where_from(session, hr_user):
    await opsec.start_session(
        session,
        hr_user,
        token_hash="h1",
        ip_address="203.0.113.4",
        user_agent="Mozilla/5.0 (Macintosh)",
        approximate_location="Bengaluru, IN",
        now=NOW,
    )
    listing = await opsec.list_sessions(session, hr_user, now=NOW)
    assert listing["active_count"] == 1
    row = listing["sessions"][0]
    assert row["ip_address"] == "203.0.113.4"
    assert row["user_agent"]
    assert row["approximate_location"]
    assert "hint rather than a fact" in row["location_caveat"]
    # hr_user's tenant has saved no policy in this test, so the listing says
    # the policy is not enforced there.
    assert listing["enforced"] is False
    # No policy is saved for this tenant here, so the listing says it is not enforced.
    assert listing["enforced"] is False
    assert "does not consult this policy yet" in listing["not_built"]


async def test_a_concurrency_limit_evicts_the_oldest_session(session, hr_user):
    """Evicting the newest would let an attacker push the real user out."""
    await opsec.configure_sessions(
        session,
        hr_user,
        idle_timeout_minutes=60,
        absolute_lifetime_hours=12,
        max_concurrent_sessions=2,
    )
    first = await opsec.start_session(
        session, hr_user, token_hash="h1", now=NOW - timedelta(hours=2)
    )
    second = await opsec.start_session(
        session, hr_user, token_hash="h2", now=NOW - timedelta(hours=1)
    )
    third = await opsec.start_session(session, hr_user, token_hash="h3", now=NOW)

    await session.refresh(first)
    await session.refresh(second)
    await session.refresh(third)
    assert first.revoked_at is not None, "the oldest session was not evicted"
    assert second.revoked_at is None
    assert third.revoked_at is None
    assert "concurrent session limit" in first.revoked_reason


async def test_revoking_one_session_leaves_the_others(session, hr_user):
    first = await opsec.start_session(session, hr_user, token_hash="h1", now=NOW)
    second = await opsec.start_session(session, hr_user, token_hash="h2", now=NOW)

    result = await opsec.revoke_session(session, hr_user, session_id=first.id, now=NOW)
    assert result["revoked"] == 1
    assert not result["tokens_invalidated"]

    await session.refresh(first)
    await session.refresh(second)
    assert first.revoked_at is not None
    assert second.revoked_at is None


async def test_revoking_all_is_immediate_across_every_session(session, hr_user):
    """The token floor is the half that actually stops a request."""
    await opsec.start_session(session, hr_user, token_hash="h1", now=NOW)
    await opsec.start_session(session, hr_user, token_hash="h2", now=NOW)

    result = await opsec.revoke_session(session, hr_user, all_for_user=hr_user.id, now=NOW)
    assert result["revoked"] == 2
    assert result["tokens_invalidated"]
    assert "Immediate" in result["effective"]

    await session.refresh(hr_user)
    assert hr_user.tokens_valid_from == NOW

    rows = (await session.execute(select(UserSession))).scalars().all()
    assert all(row.revoked_at is not None for row in rows)


async def test_revoking_a_nonexistent_session_is_refused(session, hr_user):
    with pytest.raises(HTTPException) as exc:
        await opsec.revoke_session(session, hr_user, session_id=uuid.uuid4(), now=NOW)
    assert exc.value.status_code == 404


async def test_sessions_are_scoped_to_their_tenant(session, hr_user, other_hr_user):
    await opsec.start_session(session, hr_user, token_hash="h1", now=NOW)
    theirs = await opsec.list_sessions(session, other_hr_user, now=NOW)
    assert theirs["sessions"] == []


# ══════════════════════════════════════════════════════════════════════════════
# Gate: pen-test scope and remediation SLAs
# ══════════════════════════════════════════════════════════════════════════════


async def test_a_finding_gets_its_sla_from_discovery(session, hr_user):
    row = await opsec.record_finding(
        session,
        hr_user,
        reference="F1",
        title="Cross-tenant read via a report filter",
        severity=FindingSeverityEnum.CRITICAL,
        area="tenant_isolation",
        discovered_on=date(2026, 8, 1),
        tester="Third party",
    )
    assert row.due_on == date(2026, 8, 8)


async def test_an_unknown_scope_area_is_refused(session, hr_user):
    with pytest.raises(HTTPException) as exc:
        await opsec.record_finding(
            session,
            hr_user,
            reference="F1",
            title="x",
            severity=FindingSeverityEnum.LOW,
            area="marketing_site",
            discovered_on=TODAY,
        )
    assert "not a scope area" in exc.value.detail

    count = (await session.execute(select(func.count()).select_from(PenTestFinding))).scalar_one()
    assert count == 0


async def test_a_test_covering_only_part_of_the_scope_is_reported_incomplete(session, hr_user):
    """ "We had a pen test" is worth nothing without knowing what it looked at."""
    await opsec.record_finding(
        session,
        hr_user,
        reference="F1",
        title="Weak password reset token",
        severity=FindingSeverityEnum.HIGH,
        area="authentication",
        discovered_on=TODAY - timedelta(days=60),
        tester="Third party",
    )
    posture = await opsec.posture(session, hr_user, today=TODAY)
    report = posture["penetration_testing"]
    assert not report["scope_is_complete"]
    assert "tenant_isolation" in report["scope_missing"]
    assert "agent_and_mcp" in report["scope_missing"]
    assert report["overdue_count"] == 1
    assert report["contains_no_exploit_detail"]


async def test_the_pen_test_summary_carries_no_exploit_detail(session, hr_user):
    posture = await opsec.posture(session, hr_user, today=TODAY)
    report = posture["penetration_testing"]
    assert report["contains_no_exploit_detail"] is True
    assert "remediation_slas" in report
    assert "triaging late" in report["sla_statement"]


async def test_findings_are_scoped_to_their_tenant(session, hr_user, other_hr_user):
    await opsec.record_finding(
        session,
        hr_user,
        reference="F1",
        title="x",
        severity=FindingSeverityEnum.CRITICAL,
        area="tenant_isolation",
        discovered_on=TODAY,
    )
    theirs = await opsec.posture(session, other_hr_user, today=TODAY)
    assert theirs["penetration_testing"]["findings_total"] == 0


# ══════════════════════════════════════════════════════════════════════════════
# Gate: the posture is honest
# ══════════════════════════════════════════════════════════════════════════════


async def test_the_posture_lists_everything_that_is_not_built(session, hr_user):
    """A questionnaire answered from a half-wired feature is verified later."""
    posture = await opsec.posture(session, hr_user, today=TODAY)

    assert posture["not_built"]
    joined = " ".join(posture["not_built"]).lower()
    for expected in ("backup", "penetration test", "transport", "middleware"):
        assert expected in joined, f"the posture does not mention {expected}"

    assert posture["continuity"]["not_built"]
    assert posture["access"]["not_built"]
    assert posture["siem"]["not_built"]


async def test_the_posture_covers_every_section_a_questionnaire_asks(session, hr_user):
    posture = await opsec.posture(session, hr_user, today=TODAY)
    for section in ("continuity", "access", "penetration_testing", "siem"):
        assert section in posture

    assert posture["access"]["step_up_actions"]
    assert "disbursement.release" in posture["access"]["step_up_actions"]
    assert posture["siem"]["fields"]


async def test_the_allowlist_can_be_tested_before_it_locks_somebody_out(session, hr_user):
    """An admin should be able to check their range before going home."""
    await opsec.configure_sessions(
        session,
        hr_user,
        idle_timeout_minutes=60,
        absolute_lifetime_hours=12,
        ip_allowlist=["10.1.0.0/16"],
    )
    inside = access.check_allowlist(address="10.1.2.3", entries=("10.1.0.0/16",))
    outside = access.check_allowlist(address="203.0.113.9", entries=("10.1.0.0/16",))
    assert inside.permits
    assert not outside.permits
    assert access.check_allowlist(
        address="203.0.113.9",
        entries=("10.1.0.0/16",),
        break_glass_active=True,
    ).permits


async def test_the_pentest_scope_requires_tenant_isolation_and_the_agent_surface(session, hr_user):
    assert pentest.ScopeArea.TENANT_ISOLATION in pentest.REQUIRED_SCOPE
    assert pentest.ScopeArea.AGENT_AND_MCP in pentest.REQUIRED_SCOPE
