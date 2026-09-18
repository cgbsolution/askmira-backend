"""Slice 30 — continuity, SIEM, sessions, step-up, allowlisting and pen tests.

Organised by the claim each module makes to a procurement questionnaire, and by
the way each claim goes wrong: a published RTO with no drill behind it, a SIEM
feed that died quietly, a step-up keyed on session age, an allowlist that locks
the tenant out, a pen test scoped to the marketing site.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.models.audit import AuditAction
from app.services.opsec import access, continuity, pentest, siem

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
TODAY = NOW.date()


# ══════════════════════════════════════════════════════════════════════════════
# Continuity: published numbers with evidence
# ══════════════════════════════════════════════════════════════════════════════


def test_every_tier_publishes_a_mechanism_behind_its_number():
    """A published objective with no mechanism is a promise."""
    for target in continuity.TIERS.values():
        assert target.mechanism.strip()
        assert len(target.mechanism) > 40
        assert target.degraded_mode.strip()


def test_the_critical_tier_recovers_fastest():
    critical = continuity.TIERS[continuity.DataTier.CRITICAL]
    analytical = continuity.TIERS[continuity.DataTier.ANALYTICAL]
    assert critical.rto_minutes < analytical.rto_minutes
    assert critical.rpo_minutes <= analytical.rpo_minutes


def test_a_drill_slower_than_the_rto_is_a_breach_not_a_pass():
    result = continuity.evaluate_drill(
        tier=continuity.DataTier.CRITICAL,
        performed_on=TODAY,
        measured_rto_minutes=95,
        measured_rpo_minutes=3,
        succeeded=True,
    )
    assert result.outcome is continuity.DrillOutcome.EXCEEDED_RTO
    assert not result.outcome.is_credible


def test_a_drill_losing_more_than_the_rpo_is_a_breach():
    result = continuity.evaluate_drill(
        tier=continuity.DataTier.CRITICAL,
        performed_on=TODAY,
        measured_rto_minutes=30,
        measured_rpo_minutes=45,
        succeeded=True,
    )
    assert result.outcome is continuity.DrillOutcome.EXCEEDED_RPO


def test_a_failed_drill_is_a_failure_whatever_the_timings():
    result = continuity.evaluate_drill(
        tier=continuity.DataTier.CRITICAL,
        performed_on=TODAY,
        measured_rto_minutes=1,
        measured_rpo_minutes=1,
        succeeded=False,
    )
    assert result.outcome is continuity.DrillOutcome.FAILED


def test_a_drill_inside_both_objectives_passes():
    result = continuity.evaluate_drill(
        tier=continuity.DataTier.CRITICAL,
        performed_on=TODAY,
        measured_rto_minutes=42,
        measured_rpo_minutes=3,
        succeeded=True,
    )
    assert result.outcome is continuity.DrillOutcome.MET
    assert result.outcome.is_credible


def test_a_tier_with_no_drill_is_not_evidenced():
    """An untested backup is not a backup."""
    statuses = {s.tier: s for s in continuity.drill_status([], today=TODAY)}
    for status in statuses.values():
        assert not status.is_evidenced
        assert status.is_overdue
        assert "NOT currently backed by evidence" in status.as_dict()["statement"]


def test_a_passing_drill_inside_the_interval_evidences_the_tier():
    drill = continuity.evaluate_drill(
        tier=continuity.DataTier.CRITICAL,
        performed_on=TODAY - timedelta(days=10),
        measured_rto_minutes=42,
        measured_rpo_minutes=3,
        succeeded=True,
    )
    status = {s.tier: s for s in continuity.drill_status([drill], today=TODAY)}
    assert status[continuity.DataTier.CRITICAL].is_evidenced


def test_an_old_passing_drill_stops_evidencing_the_tier():
    drill = continuity.evaluate_drill(
        tier=continuity.DataTier.CRITICAL,
        performed_on=TODAY - timedelta(days=400),
        measured_rto_minutes=42,
        measured_rpo_minutes=3,
        succeeded=True,
    )
    status = {s.tier: s for s in continuity.drill_status([drill], today=TODAY)}
    assert not status[continuity.DataTier.CRITICAL].is_evidenced


def test_the_most_recent_drill_wins_even_when_it_is_the_worse_one():
    """The newer measurement describes the system; an older pass does not."""
    passed = continuity.evaluate_drill(
        tier=continuity.DataTier.CRITICAL,
        performed_on=TODAY - timedelta(days=30),
        measured_rto_minutes=42,
        measured_rpo_minutes=3,
        succeeded=True,
    )
    failed = continuity.evaluate_drill(
        tier=continuity.DataTier.CRITICAL,
        performed_on=TODAY - timedelta(days=5),
        measured_rto_minutes=200,
        measured_rpo_minutes=3,
        succeeded=True,
    )
    status = {
        s.tier: s for s in continuity.drill_status([passed, failed], today=TODAY)
    }
    assert not status[continuity.DataTier.CRITICAL].is_evidenced


def test_a_negative_measurement_is_refused():
    with pytest.raises(continuity.ContinuityError):
        continuity.evaluate_drill(
            tier=continuity.DataTier.CRITICAL,
            performed_on=TODAY,
            measured_rto_minutes=-1,
            measured_rpo_minutes=0,
            succeeded=True,
        )


def test_the_payroll_deadline_runbook_exists_and_has_a_manual_fallback():
    """A continuity plan without this one has covered the easy case."""
    runbook = continuity.RUNBOOKS["payroll_deadline_during_incident"]
    assert runbook.manual_fallback.strip()
    assert "file" in runbook.manual_fallback.lower()
    assert runbook.severity is continuity.IncidentSeverity.SEV1
    assert runbook.steps


def test_the_region_outage_runbook_keeps_data_in_india():
    runbook = continuity.RUNBOOKS["region_outage"]
    joined = " ".join(runbook.steps).lower()
    assert "india" in joined
    assert "residency" in joined


def test_every_runbook_has_steps_and_a_degraded_mode():
    for runbook in continuity.RUNBOOKS.values():
        assert runbook.steps, f"{runbook.key} has no steps"
        assert runbook.degraded_mode.strip(), f"{runbook.key} has no degraded mode"
        assert runbook.trigger.strip()


def test_severity_response_targets_are_ordered():
    targets = [s.response_target_minutes for s in continuity.IncidentSeverity]
    assert targets == sorted(targets)


def test_the_published_posture_states_residency_and_says_what_is_unbuilt():
    posture = continuity.published_posture([], today=TODAY)
    assert "India" in posture["residency_statement"]
    assert "untested backup" in posture["honesty_statement"]
    assert posture["not_built"]
    assert any("no backup" in gap.lower() for gap in posture["not_built"])


# ══════════════════════════════════════════════════════════════════════════════
# SIEM: a standard schema, and silence is a failure
# ══════════════════════════════════════════════════════════════════════════════


def test_every_audit_action_has_a_siem_mapping():
    """An unmapped action is silently absent, which in a SOC looks like nothing happened."""
    missing = siem.unmapped_actions(frozenset(a.value for a in AuditAction))
    assert not missing, f"Audit actions with no SIEM mapping: {sorted(missing)}"


def test_an_unmapped_action_raises_rather_than_being_dropped():
    with pytest.raises(siem.SiemError) as exc:
        siem.to_ecs(
            audit_action="something_new",
            timestamp=NOW,
            event_id="e1",
            tenant_id="t1",
            tenant_name="Acme",
            summary="x",
        )
    assert "never happened" in str(exc.value)


def test_the_wire_format_uses_only_documented_ecs_fields():
    event = siem.to_ecs(
        audit_action="export",
        timestamp=NOW,
        event_id="e1",
        tenant_id="t1",
        tenant_name="Acme",
        summary="Downloaded a disbursement file.",
        user_id="u1",
        user_name="Priya",
        source_ip="203.0.113.4",
        record_count=412,
    )
    wire = event.as_wire()
    assert set(wire) <= siem.ECS_FIELDS
    assert wire["event.category"] == "database"
    assert wire["event.dataset"] == "askmira.data_export"
    assert wire["organization.id"] == "t1"


def test_nulls_are_dropped_rather_than_sent():
    """A detection filtering on user.id must not match events that have no user."""
    event = siem.to_ecs(
        audit_action="login",
        timestamp=NOW,
        event_id="e1",
        tenant_id="t1",
        tenant_name="Acme",
        summary="Signed in.",
    )
    wire = event.as_wire()
    assert "user.id" not in wire
    assert "source.ip" not in wire
    assert all(value is not None for value in wire.values())


def test_a_bulk_export_outranks_a_login_in_severity():
    export = siem.to_ecs(
        audit_action="export",
        timestamp=NOW,
        event_id="e1",
        tenant_id="t",
        tenant_name="A",
        summary="x",
    )
    login = siem.to_ecs(
        audit_action="login",
        timestamp=NOW,
        event_id="e2",
        tenant_id="t",
        tenant_name="A",
        summary="x",
    )
    assert export.as_wire()["event.severity"] > login.as_wire()["event.severity"]


def test_a_failed_action_carries_a_failure_outcome():
    event = siem.to_ecs(
        audit_action="login_failed",
        timestamp=NOW,
        event_id="e1",
        tenant_id="t",
        tenant_name="A",
        summary="Bad password.",
        succeeded=False,
    )
    assert event.as_wire()["event.outcome"] == "failure"


def test_an_event_carrying_a_non_ecs_field_is_refused():
    event = siem.SiemEvent(
        timestamp=NOW,
        action="login",
        category=siem.EventCategory.AUTHENTICATION,
        dataset="authentication",
        outcome=siem.EventOutcome.SUCCESS,
        severity=20,
        event_id="e1",
        tenant_id="t",
        tenant_name="A",
        message="x",
    )
    wire = event.as_wire()
    assert set(wire) <= siem.ECS_FIELDS


def test_a_failing_destination_alerts():
    health = siem.delivery_health(
        destination="splunk",
        last_success_at=NOW - timedelta(minutes=5),
        last_failure_at=NOW,
        consecutive_failures=3,
        expected_interval_minutes=60,
        now=NOW,
    )
    assert health.state is siem.HealthState.FAILING
    assert health.state.needs_alert


def test_a_silent_destination_alerts_even_with_no_errors():
    """The dangerous one: a SOC with a dead feed believes nothing has happened."""
    health = siem.delivery_health(
        destination="splunk",
        last_success_at=NOW - timedelta(hours=26),
        last_failure_at=None,
        consecutive_failures=0,
        expected_interval_minutes=60,
        now=NOW,
    )
    assert health.state is siem.HealthState.SILENT
    assert health.state.needs_alert
    assert "dangerous state" in health.message


def test_a_healthy_destination_does_not_alert():
    health = siem.delivery_health(
        destination="splunk",
        last_success_at=NOW - timedelta(minutes=5),
        last_failure_at=None,
        consecutive_failures=0,
        expected_interval_minutes=60,
        now=NOW,
    )
    assert health.state is siem.HealthState.HEALTHY
    assert not health.state.needs_alert


def test_a_never_used_destination_is_distinguished_from_a_healthy_one():
    health = siem.delivery_health(
        destination="splunk",
        last_success_at=None,
        last_failure_at=None,
        consecutive_failures=0,
        expected_interval_minutes=60,
        now=NOW,
    )
    assert health.state is siem.HealthState.NEVER_DELIVERED
    assert health.state.needs_alert


def test_a_quiet_tenant_inside_its_expected_interval_is_healthy():
    """Alerting on a normal quiet period is how the alert gets muted."""
    health = siem.delivery_health(
        destination="splunk",
        last_success_at=NOW - timedelta(hours=10),
        last_failure_at=None,
        consecutive_failures=0,
        expected_interval_minutes=1440,
        now=NOW,
    )
    assert health.state is siem.HealthState.HEALTHY


def test_backoff_is_capped():
    """A destination failing for a day should still be retried hourly."""
    assert siem.next_retry_delay(1) < siem.next_retry_delay(5)
    assert siem.next_retry_delay(50) == timedelta(seconds=3600)


def test_the_schema_documentation_names_ecs_and_the_one_extension():
    docs = siem.schema_documentation()
    assert "Elastic Common Schema" in docs["schema"]
    assert docs["fields"]
    assert "event.category" in docs["fields"]
    assert "extension" in docs
    assert docs["not_built"]


# ══════════════════════════════════════════════════════════════════════════════
# Step-up authentication
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "action_key",
    ["disbursement.release", "payroll.rule_change", "data.bulk_export"],
)
def test_the_three_named_actions_require_step_up(action_key):
    assert access.requires_step_up(action_key)
    assert access.step_up_reason(action_key).strip()


def test_an_ordinary_action_does_not_require_step_up():
    assert not access.requires_step_up("leave.apply")


def test_asking_the_reason_for_a_non_step_up_action_is_refused():
    """Prompting anyway trains people to confirm without reading."""
    with pytest.raises(access.AccessError) as exc:
        access.step_up_reason("leave.apply")
    assert "without reading" in str(exc.value)


def test_step_up_freshness_is_a_short_window():
    assert access.is_step_up_fresh(
        last_verified_at=NOW - timedelta(minutes=3), now=NOW
    )
    assert not access.is_step_up_fresh(
        last_verified_at=NOW - timedelta(minutes=9), now=NOW
    )


def test_never_verified_is_not_fresh():
    assert not access.is_step_up_fresh(last_verified_at=None, now=NOW)


def test_a_future_timestamp_is_refused_rather_than_treated_as_fresh():
    with pytest.raises(access.AccessError) as exc:
        access.is_step_up_fresh(
            last_verified_at=NOW + timedelta(minutes=5), now=NOW
        )
    assert "clock skew" in str(exc.value)


def test_every_step_up_action_explains_itself():
    for action_key in access.STEP_UP_ACTIONS:
        assert access.step_up_reason(action_key).strip()


# ══════════════════════════════════════════════════════════════════════════════
# Session policy
# ══════════════════════════════════════════════════════════════════════════════


def test_a_policy_whose_idle_timeout_exceeds_its_lifetime_is_refused():
    policy = access.SessionPolicy(
        idle_timeout_minutes=1000, absolute_lifetime_hours=1
    )
    with pytest.raises(access.AccessError) as exc:
        policy.validate()
    assert "can never fire" in str(exc.value)


def test_a_zero_idle_timeout_is_refused():
    with pytest.raises(access.AccessError):
        access.SessionPolicy(idle_timeout_minutes=0).validate()


def test_the_default_policy_is_valid():
    access.DEFAULT_SESSION_POLICY.validate()


def test_a_revoked_session_is_revoked_before_any_timeout_is_considered():
    """Forced logout has to be immediate, not "when it happens to go idle"."""
    state = access.evaluate_session(
        started_at=NOW - timedelta(minutes=1),
        last_seen_at=NOW,
        revoked_at=None,
        tokens_valid_from=NOW,
        policy=access.DEFAULT_SESSION_POLICY,
        now=NOW,
    )
    assert state is access.SessionState.REVOKED
    assert not state.is_usable


def test_an_explicitly_revoked_session_is_unusable():
    state = access.evaluate_session(
        started_at=NOW - timedelta(minutes=1),
        last_seen_at=NOW,
        revoked_at=NOW - timedelta(seconds=1),
        tokens_valid_from=None,
        policy=access.DEFAULT_SESSION_POLICY,
        now=NOW,
    )
    assert state is access.SessionState.REVOKED


def test_an_idle_session_expires():
    state = access.evaluate_session(
        started_at=NOW - timedelta(hours=2),
        last_seen_at=NOW - timedelta(hours=2),
        revoked_at=None,
        tokens_valid_from=None,
        policy=access.DEFAULT_SESSION_POLICY,
        now=NOW,
    )
    assert state is access.SessionState.IDLE_EXPIRED


def test_an_old_but_active_session_hits_the_absolute_lifetime():
    state = access.evaluate_session(
        started_at=NOW - timedelta(hours=13),
        last_seen_at=NOW,
        revoked_at=None,
        tokens_valid_from=None,
        policy=access.DEFAULT_SESSION_POLICY,
        now=NOW,
    )
    assert state is access.SessionState.LIFETIME_EXPIRED


def test_a_current_session_is_active():
    state = access.evaluate_session(
        started_at=NOW - timedelta(minutes=30),
        last_seen_at=NOW - timedelta(minutes=1),
        revoked_at=None,
        tokens_valid_from=None,
        policy=access.DEFAULT_SESSION_POLICY,
        now=NOW,
    )
    assert state is access.SessionState.ACTIVE
    assert state.is_usable


def test_concurrency_eviction_counts_the_excess():
    policy = access.SessionPolicy(max_concurrent_sessions=2)
    assert access.sessions_to_evict(active_started_at=[NOW], policy=policy) == 0
    assert (
        access.sessions_to_evict(active_started_at=[NOW, NOW], policy=policy) == 1
    )


def test_unlimited_concurrency_evicts_nothing():
    policy = access.SessionPolicy(max_concurrent_sessions=0)
    assert (
        access.sessions_to_evict(active_started_at=[NOW] * 20, policy=policy) == 0
    )


# ══════════════════════════════════════════════════════════════════════════════
# IP allowlisting
# ══════════════════════════════════════════════════════════════════════════════


def test_an_empty_allowlist_permits_everything():
    """Unconfigured and deny-all are different states."""
    decision = access.check_allowlist(address="203.0.113.9", entries=())
    assert decision.outcome is access.AllowlistOutcome.NOT_CONFIGURED
    assert decision.permits
    assert "not deny-all" in decision.message


def test_an_address_inside_a_range_is_allowed():
    decision = access.check_allowlist(
        address="10.1.2.3", entries=("10.1.0.0/16", "203.0.113.0/24")
    )
    assert decision.outcome is access.AllowlistOutcome.ALLOWED
    assert decision.matched_entry == "10.1.0.0/16"


def test_an_address_outside_every_range_is_blocked():
    decision = access.check_allowlist(
        address="198.51.100.7", entries=("10.1.0.0/16",)
    )
    assert decision.outcome is access.AllowlistOutcome.BLOCKED
    assert not decision.permits


def test_break_glass_works_from_any_address():
    """A control with no recovery is a control somebody disables entirely."""
    decision = access.check_allowlist(
        address="198.51.100.7",
        entries=("10.1.0.0/16",),
        break_glass_active=True,
    )
    assert decision.outcome is access.AllowlistOutcome.BREAK_GLASS
    assert decision.permits
    assert "audited" in decision.message


def test_a_single_address_entry_works():
    decision = access.check_allowlist(
        address="203.0.113.9", entries=("203.0.113.9",)
    )
    assert decision.permits


def test_an_ipv6_address_matches_an_ipv6_range():
    decision = access.check_allowlist(
        address="2001:db8::1", entries=("2001:db8::/32",)
    )
    assert decision.outcome is access.AllowlistOutcome.ALLOWED


def test_an_ipv4_address_does_not_match_an_ipv6_range():
    decision = access.check_allowlist(
        address="10.1.2.3", entries=("2001:db8::/32",)
    )
    assert decision.outcome is access.AllowlistOutcome.BLOCKED


def test_a_malformed_entry_is_refused_rather_than_skipped():
    """Skipping it leaves an admin believing their range is protected."""
    with pytest.raises(access.AccessError) as exc:
        access.check_allowlist(address="10.1.2.3", entries=("10.0.0.0/33",))
    assert "believing their office range is protected" in str(exc.value)


def test_a_malformed_address_is_refused_rather_than_blocked():
    """A misread proxy header must not lock out the whole tenant."""
    with pytest.raises(access.AccessError) as exc:
        access.check_allowlist(address="not-an-ip", entries=("10.0.0.0/8",))
    assert "locks out the whole tenant" in str(exc.value)


def test_the_access_posture_states_what_is_not_wired():
    """Enforcement now runs on every authenticated request (`core.deps`), so the
    honest remaining gaps are the unconfigured tenant and the missing second
    factor — not the absence of enforcement itself."""
    posture = access.posture_summary()
    assert posture["not_built"]
    assert any("unconfigured workspaces" in gap for gap in posture["not_built"])
    assert any("second factor" in gap for gap in posture["not_built"])


# ══════════════════════════════════════════════════════════════════════════════
# Penetration testing
# ══════════════════════════════════════════════════════════════════════════════


def test_tenant_isolation_and_the_agent_surface_are_in_the_minimum_scope():
    assert pentest.ScopeArea.TENANT_ISOLATION in pentest.REQUIRED_SCOPE
    assert pentest.ScopeArea.AGENT_AND_MCP in pentest.REQUIRED_SCOPE


def test_a_test_missing_required_scope_is_reported_incomplete():
    covered = frozenset({pentest.ScopeArea.AUTHENTICATION})
    report = pentest.summary_report(
        tested_on=TODAY,
        tester="Third party",
        covered=covered,
        findings=[],
        today=TODAY,
    )
    assert not report["scope_is_complete"]
    assert "tenant_isolation" in report["scope_missing"]
    assert "did not cover" in report["scope_statement"]


def test_a_complete_scope_is_reported_complete():
    report = pentest.summary_report(
        tested_on=TODAY,
        tester="Third party",
        covered=frozenset(pentest.REQUIRED_SCOPE),
        findings=[],
        today=TODAY,
    )
    assert report["scope_is_complete"]
    assert report["scope_missing"] == []


def test_the_sla_clock_runs_from_discovery():
    finding = pentest.Finding(
        reference="F1",
        title="Cross-tenant read",
        severity=pentest.FindingSeverity.CRITICAL,
        area=pentest.ScopeArea.TENANT_ISOLATION,
        discovered_on=date(2026, 8, 1),
    )
    assert finding.due_on == date(2026, 8, 8)


def test_a_critical_finding_has_the_shortest_sla():
    slas = pentest.REMEDIATION_SLA_DAYS
    assert slas[pentest.FindingSeverity.CRITICAL] < slas[
        pentest.FindingSeverity.HIGH
    ]
    assert slas[pentest.FindingSeverity.HIGH] < slas[pentest.FindingSeverity.MEDIUM]


def test_an_overdue_finding_is_reported_with_how_late():
    finding = pentest.Finding(
        reference="F1",
        title="Cross-tenant read",
        severity=pentest.FindingSeverity.CRITICAL,
        area=pentest.ScopeArea.TENANT_ISOLATION,
        discovered_on=date(2026, 8, 1),
    )
    assert finding.is_overdue(TODAY)
    assert finding.days_over(TODAY) == 30


def test_a_remediated_finding_is_not_overdue():
    finding = pentest.Finding(
        reference="F1",
        title="x",
        severity=pentest.FindingSeverity.CRITICAL,
        area=pentest.ScopeArea.TENANT_ISOLATION,
        discovered_on=date(2026, 8, 1),
        remediated_on=date(2026, 8, 5),
    )
    assert not finding.is_overdue(TODAY)
    assert finding.is_closed


def test_an_accepted_risk_is_closed_but_distinguishable():
    """An unfixed critical must not hide as a decision."""
    finding = pentest.Finding(
        reference="F1",
        title="x",
        severity=pentest.FindingSeverity.LOW,
        area=pentest.ScopeArea.API,
        discovered_on=date(2026, 1, 1),
        risk_accepted_reason="Only reachable by an authenticated tenant admin.",
    )
    assert finding.is_closed
    assert not finding.is_overdue(TODAY)
    assert finding.as_dict()["risk_accepted_reason"]
    assert finding.as_dict()["remediated_on"] is None


def test_overdue_findings_sort_worst_first():
    critical = pentest.Finding(
        reference="F1",
        title="x",
        severity=pentest.FindingSeverity.CRITICAL,
        area=pentest.ScopeArea.TENANT_ISOLATION,
        discovered_on=date(2026, 8, 25),
    )
    low = pentest.Finding(
        reference="F2",
        title="y",
        severity=pentest.FindingSeverity.LOW,
        area=pentest.ScopeArea.API,
        discovered_on=date(2025, 1, 1),
    )
    ordered = pentest.overdue_findings([low, critical], today=TODAY)
    assert [f.reference for f in ordered] == ["F1", "F2"]


def test_the_summary_carries_no_exploit_detail():
    """A summary a client can pass around is not a how-to-get-in document."""
    report = pentest.summary_report(
        tested_on=TODAY,
        tester="Third party",
        covered=frozenset(pentest.REQUIRED_SCOPE),
        findings=[],
        today=TODAY,
    )
    assert report["contains_no_exploit_detail"] is True
    assert report["not_built"]
