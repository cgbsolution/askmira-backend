"""Slice 25 — DPDP compliance against a real database.

The checklist gates, each as one test:

- consent is only requested where consent is genuinely the basis
- withdrawal actually halts the dependent processing
- erasure reconciles with statutory retention and reports what was retained
- the ACCESS export covers every module holding personal data
- an unannotated personal-data field fails a build-time check
- an AI model provider transfer is recorded and surfaced in the Trust Center
- the breach workflow runs end to end
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.models.dpdp import (
    BreachIncident,
    ConsentCapturedVia,
    ConsentRecord,
    CrossBorderTransfer,
    DataPrincipalKind,
    DataPrincipalRequest,
    DprRequestStatus,
    DprRequestType,
)
from app.models.employee import Employee
from app.services.dpdp import consent as consent_engine
from app.services.dpdp import service as dpdp
from tests.integration.conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]

NOW = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)


async def _configure(session, hr_user):
    return await dpdp.upsert_configuration(
        session,
        hr_user,
        grievance_contact="dpo@acme.test",
        data_protection_officer_name="Priya Ramesh",
        sla_days={"access": 21, "erasure": 45},
    )


# ══════════════════════════════════════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════════════════════════════════════


async def test_nothing_works_until_a_grievance_contact_exists(session, hr_user):
    """A notice pointing at nobody is worse than no notice: it looks like a route."""
    with pytest.raises(HTTPException) as exc:
        await dpdp.notice_for(session, hr_user)
    assert exc.value.status_code == 422
    assert "grievance contact" in exc.value.detail


async def test_a_tenant_cannot_publish_an_unlawful_response_period(session, hr_user):
    with pytest.raises(HTTPException) as exc:
        await dpdp.upsert_configuration(
            session,
            hr_user,
            grievance_contact="dpo@acme.test",
            sla_days={"grievance": 180},
        )
    assert exc.value.status_code == 400
    assert "not ours to extend" in exc.value.detail


# ══════════════════════════════════════════════════════════════════════════════
# Gate: consent only where consent is the basis
# ══════════════════════════════════════════════════════════════════════════════


async def test_consent_for_payroll_is_refused_at_the_service_boundary(
    session, hr_user, employee
):
    await _configure(session, hr_user)
    with pytest.raises(HTTPException) as exc:
        await dpdp.record_consent(
            session,
            hr_user,
            principal_id=employee.id,
            principal_kind=DataPrincipalKind.EMPLOYEE,
            purpose_key="payroll_processing",
            granted=True,
            captured_via=ConsentCapturedVia.ONBOARDING,
        )
    assert exc.value.status_code == 400
    assert "employment_contract" in exc.value.detail

    # And nothing was written.
    count = (
        await session.execute(select(func.count()).select_from(ConsentRecord))
    ).scalar_one()
    assert count == 0


async def test_consent_for_an_optional_purpose_is_recorded(
    session, hr_user, employee
):
    await _configure(session, hr_user)
    row = await dpdp.record_consent(
        session,
        hr_user,
        principal_id=employee.id,
        principal_kind=DataPrincipalKind.EMPLOYEE,
        purpose_key="culture_analytics",
        granted=True,
        captured_via=ConsentCapturedVia.ONBOARDING,
        notice_id="acme:en:v1",
    )
    assert row.granted
    assert row.notice_id == "acme:en:v1"


async def test_the_notice_separates_choices_from_disclosures(session, hr_user):
    await _configure(session, hr_user)
    notice = await dpdp.notice_for(session, hr_user)

    choice_keys = {item["purpose"] for item in notice["choices"]}
    disclosure_keys = {item["purpose"] for item in notice["disclosures"]}

    assert "culture_analytics" in choice_keys
    assert "payroll_processing" in disclosure_keys
    assert "statutory_filing" in disclosure_keys
    assert not (choice_keys & disclosure_keys)
    assert notice["grievance_contact"] == "dpo@acme.test"


async def test_an_untranslated_notice_language_says_it_fell_back(session, hr_user):
    await _configure(session, hr_user)
    notice = await dpdp.notice_for(session, hr_user, language="ta")
    assert notice["is_fallback"]
    assert notice["language"] == "en"
    assert "not yet available" in notice["fallback_line"]


# ══════════════════════════════════════════════════════════════════════════════
# Gate: withdrawal halts the dependent processing
# ══════════════════════════════════════════════════════════════════════════════


async def test_withdrawal_stops_the_dependent_capability(session, hr_user, employee):
    """The whole point of s.6(6): the button has to actually switch something off."""
    await _configure(session, hr_user)

    await dpdp.record_consent(
        session,
        hr_user,
        principal_id=employee.id,
        principal_kind=DataPrincipalKind.EMPLOYEE,
        purpose_key="culture_analytics",
        granted=True,
        captured_via=ConsentCapturedVia.ONBOARDING,
    )
    before = await dpdp.list_consent(session, hr_user, principal_id=employee.id)
    assert "culture_survey_invitations" in before["capabilities_permitted"]
    assert "culture_sentiment_analysis" in before["capabilities_permitted"]

    effect = await dpdp.withdraw_consent(
        session,
        hr_user,
        principal_id=employee.id,
        principal_kind=DataPrincipalKind.EMPLOYEE,
        purpose_key="culture_analytics",
    )
    assert "culture_survey_invitations" in effect["stops"]

    after = await dpdp.list_consent(session, hr_user, principal_id=employee.id)
    assert "culture_survey_invitations" not in after["capabilities_permitted"]
    assert "culture_sentiment_analysis" not in after["capabilities_permitted"]


async def test_withdrawing_one_consent_leaves_the_others_running(
    session, hr_user, employee
):
    await _configure(session, hr_user)
    for key in ("culture_analytics", "wellness"):
        await dpdp.record_consent(
            session,
            hr_user,
            principal_id=employee.id,
            principal_kind=DataPrincipalKind.EMPLOYEE,
            purpose_key=key,
            granted=True,
            captured_via=ConsentCapturedVia.ONBOARDING,
        )
    await dpdp.withdraw_consent(
        session,
        hr_user,
        principal_id=employee.id,
        principal_kind=DataPrincipalKind.EMPLOYEE,
        purpose_key="culture_analytics",
    )
    state = await dpdp.list_consent(session, hr_user, principal_id=employee.id)
    assert "wellness_nudges" in state["capabilities_permitted"]
    assert "culture_survey_invitations" not in state["capabilities_permitted"]


async def test_withdrawal_is_a_new_row_not_an_edit(session, hr_user, employee):
    """The grant survives the withdrawal, so the history stays answerable."""
    await _configure(session, hr_user)
    await dpdp.record_consent(
        session,
        hr_user,
        principal_id=employee.id,
        principal_kind=DataPrincipalKind.EMPLOYEE,
        purpose_key="wellness",
        granted=True,
        captured_via=ConsentCapturedVia.ONBOARDING,
    )
    await dpdp.withdraw_consent(
        session,
        hr_user,
        principal_id=employee.id,
        principal_kind=DataPrincipalKind.EMPLOYEE,
        purpose_key="wellness",
    )

    rows = (
        (
            await session.execute(
                select(ConsentRecord)
                .where(ConsentRecord.purpose_key == "wellness")
                .order_by(ConsentRecord.decided_at)
            )
        )
        .scalars()
        .all()
    )
    assert [r.granted for r in rows] == [True, False]


async def test_withdrawal_is_refused_where_there_is_nothing_to_withdraw(
    session, hr_user, employee
):
    await _configure(session, hr_user)
    with pytest.raises(HTTPException) as exc:
        await dpdp.withdraw_consent(
            session,
            hr_user,
            principal_id=employee.id,
            principal_kind=DataPrincipalKind.EMPLOYEE,
            purpose_key="statutory_filing",
        )
    assert "cannot be withdrawn" in exc.value.detail


async def test_never_asked_is_reported_separately_from_declined(
    session, hr_user, employee
):
    await _configure(session, hr_user)
    summary = await dpdp.consent_summary(session, hr_user)
    wellness = next(p for p in summary["purposes"] if p["purpose"] == "wellness")
    assert wellness["granted"] == 0
    assert wellness["declined"] == 0
    assert wellness["never_asked"] == summary["headcount"]

    await dpdp.record_consent(
        session,
        hr_user,
        principal_id=employee.id,
        principal_kind=DataPrincipalKind.EMPLOYEE,
        purpose_key="wellness",
        granted=False,
        captured_via=ConsentCapturedVia.SELF_SERVICE,
    )
    summary = await dpdp.consent_summary(session, hr_user)
    wellness = next(p for p in summary["purposes"] if p["purpose"] == "wellness")
    assert wellness["declined"] == 1
    assert wellness["never_asked"] == summary["headcount"] - 1


async def test_consent_is_scoped_to_its_tenant(
    session, hr_user, other_hr_user, employee
):
    await _configure(session, hr_user)
    await dpdp.record_consent(
        session,
        hr_user,
        principal_id=employee.id,
        principal_kind=DataPrincipalKind.EMPLOYEE,
        purpose_key="wellness",
        granted=True,
        captured_via=ConsentCapturedVia.ONBOARDING,
    )
    theirs = await dpdp.list_consent(
        session, other_hr_user, principal_id=employee.id
    )
    states = {p["purpose"]: p for p in theirs["purposes"]}
    assert states["wellness"]["status"] == "not_asked"


# ══════════════════════════════════════════════════════════════════════════════
# Gate: rights requests and their clocks
# ══════════════════════════════════════════════════════════════════════════════


async def test_a_request_carries_the_tenants_published_period(
    session, hr_user, employee
):
    await _configure(session, hr_user)
    row = await dpdp.raise_request(
        session,
        hr_user,
        principal_id=employee.id,
        principal_kind=DataPrincipalKind.EMPLOYEE,
        principal_label="Arjun Nair",
        request_type=DprRequestType.ACCESS,
        now=NOW,
    )
    assert row.sla_days == 21  # The tenant's published period, not the default.
    assert row.due_by == (NOW + timedelta(days=21)).date()
    assert row.escalate_at < row.due_by


async def test_an_overdue_request_escalates_ahead_of_an_approaching_one(
    session, hr_user, employee
):
    await _configure(session, hr_user)
    await dpdp.raise_request(
        session,
        hr_user,
        principal_id=employee.id,
        principal_kind=DataPrincipalKind.EMPLOYEE,
        principal_label="Old request",
        request_type=DprRequestType.ACCESS,
        now=NOW - timedelta(days=60),
    )
    await dpdp.raise_request(
        session,
        hr_user,
        principal_id=employee.id,
        principal_kind=DataPrincipalKind.EMPLOYEE,
        principal_label="Recent request",
        request_type=DprRequestType.ACCESS,
        now=NOW - timedelta(days=18),
    )
    listed = await dpdp.list_requests(
        session, hr_user, open_only=True, today=NOW.date()
    )
    escalations = listed["escalations"]
    assert len(escalations) == 2
    assert escalations[0]["is_overdue"]
    assert not escalations[1]["is_overdue"]


async def test_a_rejection_with_no_reason_is_refused(session, hr_user, employee):
    await _configure(session, hr_user)
    row = await dpdp.raise_request(
        session,
        hr_user,
        principal_id=employee.id,
        principal_kind=DataPrincipalKind.EMPLOYEE,
        principal_label="Arjun Nair",
        request_type=DprRequestType.ERASURE,
        now=NOW,
    )
    with pytest.raises(HTTPException) as exc:
        await dpdp.resolve_request(
            session,
            hr_user,
            request_id=row.id,
            new_status=DprRequestStatus.REJECTED,
        )
    assert "has to say why" in exc.value.detail


async def test_the_action_trail_is_appended_not_replaced(session, hr_user, employee):
    """A JSONB list mutated in place is never flagged dirty and never written."""
    await _configure(session, hr_user)
    row = await dpdp.raise_request(
        session,
        hr_user,
        principal_id=employee.id,
        principal_kind=DataPrincipalKind.EMPLOYEE,
        principal_label="Arjun Nair",
        request_type=DprRequestType.ACCESS,
        now=NOW,
    )
    await dpdp.resolve_request(
        session,
        hr_user,
        request_id=row.id,
        new_status=DprRequestStatus.IN_PROGRESS,
        note="Assembling the export.",
    )
    await dpdp.resolve_request(
        session,
        hr_user,
        request_id=row.id,
        new_status=DprRequestStatus.COMPLETED,
        response_payload={"sections": 3},
        note="Sent.",
    )

    fresh = (
        await session.execute(
            select(DataPrincipalRequest).where(DataPrincipalRequest.id == row.id)
        )
    ).scalar_one()
    await session.refresh(fresh)
    actions = [a["action"] for a in fresh.actions]
    assert actions == ["received", "in_progress", "completed"]
    assert fresh.responded_at is not None
    assert fresh.response_payload == {"sections": 3}


# ══════════════════════════════════════════════════════════════════════════════
# Gate: erasure reconciles with statutory retention
# ══════════════════════════════════════════════════════════════════════════════


async def test_erasure_retains_payroll_and_says_which_statute(
    session, hr_user, employee
):
    """The gate. Payroll survives, the person is told why, and it is not called complete."""
    employee.date_of_exit = (NOW - timedelta(days=400)).date()
    await session.commit()

    plan = await dpdp.preview_erasure(
        session, hr_user, principal_id=employee.id, today=NOW.date()
    )
    assert not plan["is_complete"]

    by_key = {d["category"]: d for d in plan["decisions"]}
    compensation = by_key["compensation"]
    assert compensation["outcome"] == "retained_statutory"
    assert compensation["statutory_reference"]
    assert compensation["retained_until"]
    assert "cannot be waived" in compensation["explanation"] or (
        "waive" in compensation["explanation"]
    )

    assert by_key["culture_response"]["outcome"] == "purged"
    # The audit trail carries its own statutory period, so 400 days after the
    # last working day it is retained like the payroll record rather than
    # pseudonymised. Pseudonymisation is what happens once that period has run
    # out, and the long-departed test below is where that is asserted.
    assert by_key["audit_trail"]["outcome"] == "retained_statutory"

    assert plan["review_again_on"] == max(
        d["retained_until"] for d in plan["decisions"] if d["retained_until"]
    )
    assert "required by law" in plan["summary"] or "law" in plan["summary"]


async def test_every_category_held_reaches_a_decision(session, hr_user, employee):
    plan = await dpdp.preview_erasure(
        session, hr_user, principal_id=employee.id, today=NOW.date()
    )
    from app.services.dpdp.inventory import build_inventory

    inventory = build_inventory(dpdp.current_schema_shape())
    held = {c.category_key for c in inventory.columns}
    decided = {d["category"] for d in plan["decisions"]}
    assert decided == held


async def test_a_long_departed_employee_can_be_erased_completely(
    session, hr_user, employee
):
    """Once every statutory period has run out, the answer changes to yes."""
    employee.date_of_exit = (NOW - timedelta(days=365 * 12)).date()
    await session.commit()

    plan = await dpdp.preview_erasure(
        session, hr_user, principal_id=employee.id, today=NOW.date()
    )
    retained = [d for d in plan["decisions"] if d["outcome"].startswith("retained")]
    assert not retained, (
        f"Still retaining {[d['category'] for d in retained]} twelve years after "
        "the last working day."
    )
    assert plan["is_complete"]

    # With every statutory period expired, the audit trail reaches the branch
    # that keeps the row and drops what identifies them — the one deletion that
    # would destroy the evidence the erasure was honoured.
    by_key = {d["category"]: d for d in plan["decisions"]}
    assert by_key["audit_trail"]["outcome"] == "pseudonymised"
    assert "erasure request being honoured" in by_key["audit_trail"]["explanation"]


# ══════════════════════════════════════════════════════════════════════════════
# Gate: the ACCESS export covers every module
# ══════════════════════════════════════════════════════════════════════════════


async def test_the_export_covers_every_module_holding_personal_data(
    session, hr_user, employee
):
    """Module by module, from the classification rather than from a hand-list."""
    await _configure(session, hr_user)
    export = await dpdp.build_access_export(
        session, hr_user, principal_id=employee.id
    )

    from app.services.dpdp.inventory import build_inventory

    inventory = build_inventory(dpdp.current_schema_shape())
    expected_tables = {c.table for c in inventory.columns}
    covered_tables = {t for section in export["sections"] for t in section["held_in"]}
    assert covered_tables == expected_tables, (
        "The export does not reach "
        f"{sorted(expected_tables - covered_tables)} — a module holding personal "
        "data that an access request would not surface."
    )


async def test_the_export_includes_the_persons_own_rows_and_withholds_credentials(
    session, hr_user, employee
):
    await _configure(session, hr_user)
    export = await dpdp.build_access_export(
        session, hr_user, principal_id=employee.id
    )
    assert export["completeness"]["contents_included"] is True
    tables = {section["table"]: section for section in export["contents"]}
    assert tables["employees"]["rows"][0]["work_email"] == employee.work_email
    assert "credential" in export["completeness"]["withheld"].lower()


async def test_the_export_explains_each_section_in_plain_language(
    session, hr_user, employee
):
    await _configure(session, hr_user)
    export = await dpdp.build_access_export(
        session, hr_user, principal_id=employee.id
    )
    for section in export["sections"]:
        assert section["what_this_is"]
        assert section["why_we_hold_it"]
        assert section["how_long"]
        # No table names in the human-readable fields.
        assert "_" not in section["name"]


# ══════════════════════════════════════════════════════════════════════════════
# Gate: an unannotated personal-data field fails a build-time check
# ══════════════════════════════════════════════════════════════════════════════


async def test_the_live_schema_is_fully_classified(session, hr_user):
    from app.services.dpdp.inventory import coverage_gaps

    gaps = coverage_gaps(dpdp.current_schema_shape())
    assert not gaps, "\n".join(str(g) for g in gaps)


async def test_the_compliance_surface_reports_gaps_rather_than_hiding_them(
    session, hr_user
):
    await _configure(session, hr_user)
    surface = await dpdp.tenant_compliance_surface(session, hr_user, today=NOW.date())
    assert surface["classification_gaps"] == []
    assert surface["known_gaps"], (
        "The compliance surface no longer lists what this module does not do. A "
        "page of green ticks is the invented reassurance this codebase refuses."
    )
    assert any("counsel" in gap for gap in surface["known_gaps"])


# ══════════════════════════════════════════════════════════════════════════════
# Gate: a model-provider transfer is recorded and disclosed
# ══════════════════════════════════════════════════════════════════════════════


async def test_a_transfer_to_a_model_provider_is_recorded_as_cross_border(
    session, hr_user
):
    await _configure(session, hr_user)
    row = await dpdp.record_transfer(
        session,
        hr_user,
        sub_processor_key="anthropic",
        purpose_key="culture_analytics",
        category_keys=("culture_response",),
        principal_count=42,
        description="Sentiment analysis over one pulse survey.",
        now=NOW,
    )
    assert row.was_cross_border

    history = await dpdp.transfer_history(session, hr_user)
    assert history["cross_border_transfers_recorded"] == 1
    assert history["recent"][0]["sub_processor"] == "anthropic"
    assert "Anthropic" in {
        p["name"] for p in history["disclosure"]["ai_model_providers"]
    }


async def test_bank_details_cannot_be_sent_to_a_model_provider(session, hr_user):
    await _configure(session, hr_user)
    with pytest.raises(HTTPException) as exc:
        await dpdp.record_transfer(
            session,
            hr_user,
            sub_processor_key="anthropic",
            purpose_key="ai_assistance",
            category_keys=("financial",),
            principal_count=1,
            description="Should never happen.",
        )
    assert exc.value.status_code == 400
    assert "Bank and payment details" in exc.value.detail

    count = (
        await session.execute(select(func.count()).select_from(CrossBorderTransfer))
    ).scalar_one()
    assert count == 0, "A refused transfer still wrote a row."


async def test_the_primary_database_is_not_a_cross_border_transfer(session, hr_user):
    await _configure(session, hr_user)
    row = await dpdp.record_transfer(
        session,
        hr_user,
        sub_processor_key="supabase",
        purpose_key="employment_administration",
        category_keys=("identity",),
        principal_count=1,
        description="Ordinary write.",
        now=NOW,
    )
    assert not row.was_cross_border


# ══════════════════════════════════════════════════════════════════════════════
# Gate: the breach workflow, end to end
# ══════════════════════════════════════════════════════════════════════════════


async def test_a_breach_is_graded_clocked_notified_and_closed(session, hr_user):
    """The rehearsal the checklist asks for, run against the real code path."""
    await _configure(session, hr_user)

    aware = NOW - timedelta(hours=6)
    incident = await dpdp.raise_breach(
        session,
        hr_user,
        reference="INC-2026-001",
        became_aware_at=aware,
        description=(
            "A payroll export was written to a storage location readable "
            "without authentication for four hours."
        ),
        affected_data_categories=("statutory_identifier", "financial", "identity"),
        affected_principal_count=412,
        data_left_our_control=True,
    )
    assert incident.severity.value == "severe"
    assert incident.board_report_due_at == aware + timedelta(hours=72)

    state = await dpdp.breach_state(session, hr_user, incident_id=incident.id, now=NOW)
    keys = [o["key"] for o in state["obligations"]]
    assert "intimate_principals" in keys
    assert "board_detailed_report" in keys
    assert not any(o["is_done"] for o in state["obligations"])

    # A notification cannot be drafted before there is a remediation to report.
    with pytest.raises(HTTPException):
        await dpdp.draft_breach_notification(
            session, hr_user, incident_id=incident.id
        )

    incident.remediation = "Access revoked in eleven minutes; the file was deleted."
    await session.commit()

    draft = await dpdp.draft_breach_notification(
        session, hr_user, incident_id=incident.id
    )
    assert "Bank and payment details" in draft["categories_named"]
    joined = " ".join(draft["actions_advised"])
    assert "myaadhaar.uidai.gov.in" in joined
    assert "dpo@acme.test" in joined

    incident.principals_notified_at = NOW
    incident.reported_to_board_at = NOW
    await session.commit()

    state = await dpdp.breach_state(session, hr_user, incident_id=incident.id, now=NOW)
    assert all(o["is_done"] for o in state["obligations"])


async def test_an_internal_over_exposure_grades_below_a_public_one(session, hr_user):
    await _configure(session, hr_user)
    internal = await dpdp.raise_breach(
        session,
        hr_user,
        reference="INC-2026-002",
        became_aware_at=NOW,
        description="A manager could see a report they should not have.",
        affected_data_categories=("financial",),
        affected_principal_count=200,
        data_left_our_control=False,
    )
    public = await dpdp.raise_breach(
        session,
        hr_user,
        reference="INC-2026-003",
        became_aware_at=NOW,
        description="The same data reachable from the internet.",
        affected_data_categories=("financial",),
        affected_principal_count=200,
        data_left_our_control=True,
    )
    assert internal.severity.value == "high"
    assert public.severity.value == "severe"


async def test_a_rehearsal_never_appears_in_the_breach_history(session, hr_user):
    await _configure(session, hr_user)
    await dpdp.raise_breach(
        session,
        hr_user,
        reference="REHEARSAL-1",
        became_aware_at=NOW,
        description="Rehearsal only.",
        affected_data_categories=("identity",),
        affected_principal_count=10,
        data_left_our_control=True,
        is_rehearsal=True,
    )
    surface = await dpdp.tenant_compliance_surface(session, hr_user, today=NOW.date())
    assert surface["breach_history"]["incidents"] == 0
    assert surface["breach_history"]["rehearsals"] == 1


async def test_the_breach_register_lists_every_incident_newest_first(session, hr_user):
    """An incident raised last week has to be findable without keeping its id."""
    await _configure(session, hr_user)
    older = await dpdp.raise_breach(
        session,
        hr_user,
        reference="INC-2026-010",
        became_aware_at=NOW - timedelta(days=7),
        description="An older incident.",
        affected_data_categories=("identity",),
        affected_principal_count=3,
        data_left_our_control=False,
    )
    newer = await dpdp.raise_breach(
        session,
        hr_user,
        reference="REHEARSAL-10",
        became_aware_at=NOW,
        description="A rehearsal.",
        affected_data_categories=("identity",),
        affected_principal_count=3,
        data_left_our_control=True,
        is_rehearsal=True,
    )

    register = await dpdp.list_breaches(session, hr_user)

    assert [row["id"] for row in register] == [str(newer.id), str(older.id)]
    assert register[0]["is_rehearsal"] is True
    assert register[1]["reference"] == "INC-2026-010"
    assert register[1]["board_report_due_at"] == older.board_report_due_at.isoformat()


async def test_an_incident_with_an_unclassified_category_cannot_be_graded(
    session, hr_user
):
    await _configure(session, hr_user)
    with pytest.raises(HTTPException) as exc:
        await dpdp.raise_breach(
            session,
            hr_user,
            reference="INC-2026-004",
            became_aware_at=NOW,
            description="Something happened.",
            affected_data_categories=("mystery",),
            affected_principal_count=5,
            data_left_our_control=True,
        )
    assert "guessing low" in exc.value.detail

    count = (
        await session.execute(select(func.count()).select_from(BreachIncident))
    ).scalar_one()
    assert count == 0


async def test_breach_records_are_scoped_to_their_tenant(
    session, hr_user, other_hr_user
):
    await _configure(session, hr_user)
    await dpdp.upsert_configuration(
        session, other_hr_user, grievance_contact="dpo@globex.test"
    )
    incident = await dpdp.raise_breach(
        session,
        hr_user,
        reference="INC-2026-005",
        became_aware_at=NOW,
        description="Acme only.",
        affected_data_categories=("identity",),
        affected_principal_count=3,
        data_left_our_control=True,
    )
    with pytest.raises(HTTPException) as exc:
        await dpdp.breach_state(
            session, other_hr_user, incident_id=incident.id, now=NOW
        )
    assert exc.value.status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# The RoPA
# ══════════════════════════════════════════════════════════════════════════════


async def test_the_processing_record_covers_every_declared_purpose(session, hr_user):
    await _configure(session, hr_user)
    surface = await dpdp.tenant_compliance_surface(session, hr_user, today=NOW.date())
    keys = {a["purpose"] for a in surface["record_of_processing"]}
    assert keys == set(consent_engine.PURPOSES)


async def test_the_processing_record_marks_which_activities_can_be_refused(
    session, hr_user
):
    await _configure(session, hr_user)
    surface = await dpdp.tenant_compliance_surface(session, hr_user, today=NOW.date())
    by_key = {a["purpose"]: a for a in surface["record_of_processing"]}
    assert by_key["statutory_filing"]["can_be_withdrawn"] is False
    assert by_key["culture_analytics"]["can_be_withdrawn"] is True


async def test_a_retired_purpose_on_record_is_surfaced_not_dropped(
    session, hr_user, employee
):
    """A consent row for something we stopped doing is evidence, not noise."""
    await _configure(session, hr_user)
    session.add(
        ConsentRecord(
            tenant_id=hr_user.tenant_id,
            principal_id=employee.id,
            principal_kind=DataPrincipalKind.EMPLOYEE,
            purpose_key="a_purpose_we_retired",
            granted=True,
            decided_at=NOW,
            purpose_version=1,
            captured_via=ConsentCapturedVia.IMPORTED,
        )
    )
    await session.commit()

    state = await dpdp.list_consent(session, hr_user, principal_id=employee.id)
    assert state["retired_purposes_on_record"] == ["a_purpose_we_retired"]


async def test_an_unknown_principal_produces_an_empty_but_valid_consent_view(
    session, hr_user
):
    state = await dpdp.list_consent(session, hr_user, principal_id=uuid.uuid4())
    assert {p["purpose"] for p in state["purposes"]} == set(consent_engine.PURPOSES)
    assert state["capabilities_permitted"] == []


async def test_the_employee_fixture_is_reachable(session, employee):
    """Guards the fixtures the rest of this module leans on."""
    assert isinstance(employee, Employee)
    assert employee.id is not None
