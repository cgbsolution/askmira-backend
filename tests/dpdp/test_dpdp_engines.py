"""Slice 25 — the DPDP engines.

Organised by the thing that would actually go wrong: consent asked for where it
is not the basis, an erasure that deletes what it must keep or keeps what it
could delete, a clock that starts in the wrong place, a notice that silently
falls back, a payload that leaves the country with something in it that should
not.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.services.dpdp import breach, consent, inventory, notices, rights, transfers
from app.services.dpdp.categories import (
    CATEGORIES,
    HIGH_HARM_CATEGORIES,
    LawfulBasis,
    category,
    category_keys,
)
from app.services.dpdp.schema_map import TABLES, is_direct_identifier

NOW = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
TODAY = date(2026, 9, 7)


# ══════════════════════════════════════════════════════════════════════════════
# The catalogue
# ══════════════════════════════════════════════════════════════════════════════


def test_every_category_names_purposes_that_exist():
    for cat in CATEGORIES.values():
        for purpose_key in cat.purposes:
            assert purpose_key in consent.PURPOSES, (
                f"Category {cat.key!r} cites purpose {purpose_key!r}, which the "
                "purpose catalogue does not define."
            )


def test_every_purpose_names_categories_that_exist():
    for spec in consent.PURPOSES.values():
        for key in spec.data_categories:
            assert key in CATEGORIES, f"Purpose {spec.key!r} cites category {key!r}."


def test_only_consent_is_withdrawable():
    for basis in LawfulBasis:
        assert basis.is_withdrawable == (basis is LawfulBasis.CONSENT)


def test_a_statutory_retention_names_the_statute():
    for cat in CATEGORIES.values():
        if cat.retention.is_statutory:
            assert cat.retention.statutory_reference
        else:
            # A non-statutory retention has to explain itself, because "we keep
            # this for five years" with no reason is a number somebody guessed.
            assert cat.retention.note, (
                f"{cat.key} has a non-statutory retention with no note saying why that period."
            )


def test_high_harm_categories_all_exist():
    assert category_keys() >= HIGH_HARM_CATEGORIES


def test_looking_up_a_missing_category_says_what_exists():
    with pytest.raises(KeyError) as exc:
        category("nonsense")
    assert "identity" in str(exc.value)


# ══════════════════════════════════════════════════════════════════════════════
# Consent: the checkbox that lies
# ══════════════════════════════════════════════════════════════════════════════


def test_consent_cannot_be_recorded_for_a_statutory_obligation():
    with pytest.raises(consent.ConsentError) as exc:
        consent.record_decision(
            "statutory_filing",
            granted=True,
            now=NOW,
            captured_via=consent.CapturedVia.ONBOARDING,
        )
    message = str(exc.value)
    assert "legal_obligation" in message
    assert "withdraw" in message


def test_consent_cannot_be_recorded_for_payroll():
    with pytest.raises(consent.ConsentError):
        consent.record_decision(
            "payroll_processing",
            granted=True,
            now=NOW,
            captured_via=consent.CapturedVia.ONBOARDING,
        )


def test_consent_can_be_recorded_where_consent_is_the_basis():
    decision = consent.record_decision(
        "celebrations",
        granted=True,
        now=NOW,
        captured_via=consent.CapturedVia.SELF_SERVICE,
    )
    assert decision.granted
    assert decision.version == consent.purpose("celebrations").version


def test_a_mandatory_purpose_never_rests_on_consent():
    """The pairing that makes the checkbox unrepresentable rather than discouraged."""
    for spec in consent.PURPOSES.values():
        if spec.is_mandatory_for_employment:
            assert spec.lawful_basis is not LawfulBasis.CONSENT, (
                f"{spec.key} is mandatory for employment and rests on consent. "
                "If somebody cannot say no, their agreement was not freely given."
            )


def test_every_consent_purpose_stops_something():
    for spec in consent.PURPOSES.values():
        if spec.requires_consent:
            assert spec.stops, (
                f"{spec.key} rests on consent but names nothing that stops when "
                "it is withdrawn. A consent whose withdrawal changes nothing is "
                "a consent we should not have asked for."
            )


def test_every_capability_a_purpose_stops_is_declared():
    for spec in consent.PURPOSES.values():
        for capability in spec.stops:
            assert capability in consent.CAPABILITIES, (
                f"{spec.key} claims to stop {capability!r}, which is not a declared capability."
            )


def test_a_non_consent_purpose_stops_nothing():
    for spec in consent.PURPOSES.values():
        if not spec.requires_consent:
            assert not spec.stops


def test_never_asked_is_not_the_same_as_declined():
    states = {s.purpose_key: s for s in consent.consent_state([])}
    assert states["celebrations"].status is consent.ConsentStatus.NOT_ASKED
    assert not states["celebrations"].is_permitted


def test_a_non_consent_purpose_is_permitted_without_a_decision():
    states = {s.purpose_key: s for s in consent.consent_state([])}
    payroll = states["payroll_processing"]
    assert payroll.status is consent.ConsentStatus.NOT_APPLICABLE
    assert payroll.is_permitted


def test_the_latest_decision_wins_and_the_earlier_one_survives():
    decisions = [
        consent.record_decision(
            "celebrations",
            granted=True,
            now=NOW,
            captured_via=consent.CapturedVia.ONBOARDING,
        ),
        consent.record_decision(
            "celebrations",
            granted=False,
            now=NOW + timedelta(days=30),
            captured_via=consent.CapturedVia.SELF_SERVICE,
        ),
    ]
    states = {s.purpose_key: s for s in consent.consent_state(decisions)}
    assert states["celebrations"].status is consent.ConsentStatus.DECLINED
    # Both rows are still in the input; the fold does not destroy history.
    assert len(decisions) == 2


def test_a_stale_version_is_not_consent():
    spec = consent.purpose("celebrations")
    old = consent.ConsentDecision(
        purpose_key="celebrations",
        granted=True,
        decided_at=NOW,
        version=spec.version - 1 if spec.version > 1 else 0,
        captured_via=consent.CapturedVia.ONBOARDING,
    )
    states = {s.purpose_key: s for s in consent.consent_state([old])}
    assert states["celebrations"].status is consent.ConsentStatus.STALE
    assert not states["celebrations"].is_permitted


def test_withdrawing_consent_stops_the_named_capability():
    granted = consent.record_decision(
        "culture_analytics",
        granted=True,
        now=NOW,
        captured_via=consent.CapturedVia.SELF_SERVICE,
    )
    states = consent.consent_state([granted])
    assert consent.is_permitted("culture_survey_invitations", states)

    withdrawn = consent.record_decision(
        "culture_analytics",
        granted=False,
        now=NOW + timedelta(days=1),
        captured_via=consent.CapturedVia.SELF_SERVICE,
    )
    states = consent.consent_state([granted, withdrawn])
    assert not consent.is_permitted("culture_survey_invitations", states)
    assert not consent.is_permitted("culture_sentiment_analysis", states)


def test_withdrawing_one_consent_does_not_stop_another():
    decisions = [
        consent.record_decision(
            "culture_analytics",
            granted=False,
            now=NOW,
            captured_via=consent.CapturedVia.SELF_SERVICE,
        ),
        consent.record_decision(
            "celebrations",
            granted=True,
            now=NOW,
            captured_via=consent.CapturedVia.SELF_SERVICE,
        ),
    ]
    states = consent.consent_state(decisions)
    assert not consent.is_permitted("culture_survey_invitations", states)
    assert consent.is_permitted("birthday_announcements", states)


def test_an_unknown_capability_is_refused_not_allowed():
    with pytest.raises(consent.ConsentError) as exc:
        consent.is_permitted("some_new_feature", consent.consent_state([]))
    assert "refused" in str(exc.value)


def test_withdrawal_effect_is_honest_about_what_stays():
    effect = consent.withdrawal_effect("culture_analytics")
    assert effect.stops
    # Anonymous answers cannot be found to delete, and the person is told so.
    assert effect.retained and "without your name" in effect.retained[0]
    # Unanswered invitations rest only on this consent, so they go (s.8(7)(a)).
    assert effect.erased
    assert "deleted" in effect.summary


def test_withdrawing_ai_consent_names_what_is_deleted():
    effect = consent.withdrawal_effect("ai_assistance")
    assert any("Ask Mira" in item for item in effect.erased)


def test_purposes_for_features_that_do_not_exist_are_gone():
    """No switch for a wellness programme or product news that nothing sends."""
    assert "wellness" not in consent.PURPOSES
    assert "marketing_communication" not in consent.PURPOSES


def test_candidate_purposes_are_not_on_an_employees_privacy_screen():
    keys = consent.employee_purpose_keys()
    for candidate_only in ("talent_pool", "background_verification", "recruitment", "ai_screening"):
        assert candidate_only not in keys
    assert "directory_visibility" in keys and "celebrations" in keys


def test_withdrawal_is_refused_for_a_non_consent_purpose():
    with pytest.raises(consent.ConsentError) as exc:
        consent.withdrawal_effect("statutory_filing")
    assert "cannot be withdrawn" in str(exc.value)


# ══════════════════════════════════════════════════════════════════════════════
# Rights: clocks
# ══════════════════════════════════════════════════════════════════════════════


def test_a_clock_escalates_before_it_is_due():
    clock = rights.clock_for(rights.RequestType.ACCESS, received_on=TODAY)
    assert clock.escalate_at < clock.due_by


def test_a_tenant_cannot_publish_a_period_longer_than_the_statute():
    with pytest.raises(ValueError) as exc:
        rights.validate_sla(rights.RequestType.GRIEVANCE, 120)
    assert "not ours to extend" in str(exc.value)


def test_a_tenant_can_publish_a_shorter_period():
    clock = rights.clock_for(rights.RequestType.ACCESS, received_on=TODAY, sla_days=7)
    assert clock.due_by == TODAY + timedelta(days=7)


def test_a_short_period_still_escalates_at_least_a_day_in():
    clock = rights.clock_for(rights.RequestType.NOMINATION, received_on=TODAY, sla_days=1)
    assert clock.escalate_at > TODAY


def test_overdue_requests_sort_ahead_of_approaching_ones():
    old = rights.clock_for(rights.RequestType.ACCESS, received_on=TODAY - timedelta(days=60))
    approaching = rights.clock_for(
        rights.RequestType.ACCESS, received_on=TODAY - timedelta(days=25)
    )
    out = rights.escalations([("approaching", approaching), ("old", old)], today=TODAY)
    assert [e.request_id for e in out] == ["old", "approaching"]
    assert out[0].is_overdue


def test_a_request_inside_its_window_does_not_escalate():
    fresh = rights.clock_for(rights.RequestType.ACCESS, received_on=TODAY)
    assert rights.escalations([("fresh", fresh)], today=TODAY) == []


# ══════════════════════════════════════════════════════════════════════════════
# Rights: erasure
# ══════════════════════════════════════════════════════════════════════════════


def test_erasure_keeps_payroll_and_says_which_statute():
    plan = rights.reconcile_erasure(
        ("compensation", "culture_response"),
        requested_on=TODAY,
        last_working_day=date(2025, 6, 30),
        financial_year_end=date(2026, 3, 31),
    )
    by_key = {d.category_key: d for d in plan.decisions}
    assert by_key["compensation"].outcome is rights.ErasureOutcome.RETAINED_STATUTORY
    assert by_key["compensation"].statutory_reference
    assert by_key["compensation"].retained_until is not None
    assert by_key["culture_response"].outcome is rights.ErasureOutcome.PURGED


def test_a_partial_erasure_is_never_reported_as_complete():
    plan = rights.reconcile_erasure(
        ("compensation", "culture_response"),
        requested_on=TODAY,
        last_working_day=date(2025, 6, 30),
        financial_year_end=date(2026, 3, 31),
    )
    assert not plan.is_complete
    assert plan.retained
    assert "could not be" in plan.summary


def test_an_erasure_with_nothing_retained_is_complete():
    plan = rights.reconcile_erasure(
        ("culture_response", "performance"),
        requested_on=TODAY,
        last_working_day=date(2019, 1, 1),
    )
    assert plan.is_complete
    assert plan.review_again_on is None


def test_the_audit_trail_is_pseudonymised_not_deleted():
    plan = rights.reconcile_erasure(
        ("audit_trail",),
        requested_on=TODAY,
        # Long enough ago that the statutory period has run out, so the only
        # reason this row survives is the pseudonymisation rule.
        last_working_day=date(2010, 1, 1),
        financial_year_end=date(2010, 3, 31),
    )
    decision = plan.decisions[0]
    assert decision.outcome is rights.ErasureOutcome.PSEUDONYMISED
    assert "erasure request being honoured" in decision.explanation


def test_an_expired_statutory_period_no_longer_blocks_erasure():
    plan = rights.reconcile_erasure(
        ("compensation",),
        requested_on=TODAY,
        last_working_day=date(2010, 3, 31),
        financial_year_end=date(2010, 3, 31),
    )
    assert plan.decisions[0].outcome is rights.ErasureOutcome.PURGED
    assert plan.is_complete


def test_a_legal_hold_beats_everything_and_has_no_end_date():
    plan = rights.reconcile_erasure(
        ("culture_response",),
        requested_on=TODAY,
        last_working_day=date(2019, 1, 1),
        legal_holds=frozenset({"culture_response"}),
    )
    decision = plan.decisions[0]
    assert decision.outcome is rights.ErasureOutcome.RETAINED_LEGAL_HOLD
    assert decision.retained_until is None


def test_every_category_reaches_a_decision():
    """No category is silently skipped — the whole point of the module."""
    keys = tuple(CATEGORIES)
    plan = rights.reconcile_erasure(keys, requested_on=TODAY, last_working_day=date(2025, 1, 1))
    assert {d.category_key for d in plan.decisions} == set(keys)


def test_an_unknown_category_raises_rather_than_being_skipped():
    with pytest.raises(rights.ReconciliationError) as exc:
        rights.reconcile_erasure(("not_a_category",), requested_on=TODAY)
    assert "survives an erasure request silently" in str(exc.value)


def test_the_retention_clock_runs_from_the_right_starting_point():
    """A payroll record's clock starts at financial year end, not at the request."""
    plan = rights.reconcile_erasure(
        ("compensation",),
        requested_on=TODAY,
        last_working_day=date(2024, 8, 15),
        financial_year_end=date(2025, 3, 31),
    )
    retained = plan.decisions[0]
    # 96 months from 31 March 2025.
    assert retained.retained_until == date(2033, 3, 31)


def test_an_end_of_month_clock_does_not_overflow():
    """31 January plus one month is February, not 3 March."""
    from app.services.dpdp.rights import _obligation_ends

    ends = _obligation_ends(
        category("identity"),
        last_working_day=date(2024, 1, 31),
        financial_year_end=None,
        requested_on=TODAY,
    )
    assert ends.month == 1  # 96 months is exactly eight years.
    assert ends == date(2032, 1, 31)


def test_the_review_again_date_is_the_longest_obligation():
    plan = rights.reconcile_erasure(
        ("compensation", "attendance"),
        requested_on=TODAY,
        last_working_day=date(2025, 6, 30),
        financial_year_end=date(2026, 3, 31),
    )
    assert plan.review_again_on == max(d.retained_until for d in plan.retained if d.retained_until)


# ══════════════════════════════════════════════════════════════════════════════
# Notices
# ══════════════════════════════════════════════════════════════════════════════


def test_the_eighth_schedule_is_all_there():
    # 22 scheduled languages plus English.
    assert len(notices.Language) == 23


def test_shipped_languages_are_a_strict_subset():
    assert set(notices.Language) > notices.SHIPPED_LANGUAGES


def test_an_unshipped_language_falls_back_and_says_so():
    notice = notices.render_notice(language=notices.Language.TAMIL, notice_id="n")
    assert notice.is_fallback
    assert notice.language is notices.Language.ENGLISH
    assert notice.fallback_line
    assert "not yet available" in notice.fallback_line


def test_a_shipped_language_does_not_fall_back():
    notice = notices.render_notice(language=notices.Language.HINDI, notice_id="n")
    assert not notice.is_fallback
    assert notice.language is notices.Language.HINDI
    assert not notice.fallback_line


def test_choices_and_disclosures_are_separated():
    notice = notices.render_notice(language=notices.Language.ENGLISH, notice_id="n")
    assert all(i.offers_a_choice for i in notice.choices)
    assert not any(i.offers_a_choice for i in notice.disclosures)
    assert notice.choices and notice.disclosures


def test_payroll_appears_as_a_disclosure_never_as_a_choice():
    notice = notices.render_notice(language=notices.Language.ENGLISH, notice_id="n")
    keys = {i.purpose_key for i in notice.choices}
    assert "payroll_processing" not in keys
    assert "statutory_filing" not in keys
    assert "payroll_processing" in {i.purpose_key for i in notice.disclosures}


def test_a_purpose_that_leaves_india_names_its_sub_processor_in_the_notice():
    notice = notices.render_notice(language=notices.Language.ENGLISH, notice_id="n")
    ai = next(i for i in notice.choices if i.purpose_key == "ai_assistance")
    assert ai.sub_processors
    assert "outside India" in ai.body


def test_a_notice_for_an_unknown_purpose_is_refused():
    with pytest.raises(notices.NoticeError):
        notices.render_notice(
            language=notices.Language.ENGLISH,
            notice_id="n",
            purpose_keys=("not_a_purpose",),
        )


# ══════════════════════════════════════════════════════════════════════════════
# Breach
# ══════════════════════════════════════════════════════════════════════════════


def test_the_two_clocks_are_not_the_same():
    clock = breach.clock_from(NOW)
    assert clock.intimation_target_at < clock.board_report_due_at
    assert clock.board_report_due_at == NOW + timedelta(hours=72)


def test_the_clock_runs_from_becoming_aware_not_from_the_record():
    aware = NOW - timedelta(hours=40)
    clock = breach.clock_from(aware)
    assert clock.board_report_due_at == aware + timedelta(hours=72)
    assert not clock.board_report_overdue(NOW)
    assert clock.board_report_overdue(aware + timedelta(hours=73))


def test_a_small_high_harm_leak_outranks_a_large_low_harm_one():
    small_severe = breach.assess_severity(
        category_keys=("statutory_identifier", "financial"),
        affected_principal_count=1,
        data_left_our_control=True,
    )
    large_mild = breach.assess_severity(
        category_keys=("identity",),
        affected_principal_count=500,
        data_left_our_control=True,
    )
    assert small_severe is breach.BreachSeverity.HIGH
    assert large_mild is breach.BreachSeverity.MEDIUM


def test_an_internal_over_exposure_grades_below_a_public_one():
    internal = breach.assess_severity(
        category_keys=("financial",),
        affected_principal_count=200,
        data_left_our_control=False,
    )
    public = breach.assess_severity(
        category_keys=("financial",),
        affected_principal_count=200,
        data_left_our_control=True,
    )
    assert internal is breach.BreachSeverity.HIGH
    assert public is breach.BreachSeverity.SEVERE


def test_nothing_exposed_is_negligible_and_notifies_nobody():
    severity = breach.assess_severity(
        category_keys=(),
        affected_principal_count=0,
        data_left_our_control=False,
    )
    assert severity is breach.BreachSeverity.NEGLIGIBLE
    assert not severity.notify_principals


def test_every_non_negligible_severity_notifies_principals():
    for severity in breach.BreachSeverity:
        assert severity.notify_principals == (severity is not breach.BreachSeverity.NEGLIGIBLE)


def test_an_unclassified_category_cannot_be_graded():
    with pytest.raises(breach.BreachError) as exc:
        breach.assess_severity(
            category_keys=("mystery",),
            affected_principal_count=10,
            data_left_our_control=True,
        )
    assert "guessing low" in str(exc.value)


def test_a_notification_with_no_facts_is_refused():
    with pytest.raises(breach.BreachError) as exc:
        breach.notification_to_principals(
            category_keys=("identity",),
            severity=breach.BreachSeverity.MEDIUM,
            what_happened="   ",
            became_aware_at=NOW,
            remediation="Fixed.",
            grievance_contact="dpo@acme.example",
        )
    assert "an incident occurred" in str(exc.value)


def test_a_notification_with_no_remediation_is_refused():
    with pytest.raises(breach.BreachError):
        breach.notification_to_principals(
            category_keys=("identity",),
            severity=breach.BreachSeverity.MEDIUM,
            what_happened="A bucket was public.",
            became_aware_at=NOW,
            remediation="",
            grievance_contact="dpo@acme.example",
        )


def test_the_notification_gives_concrete_actions_per_category():
    draft = breach.notification_to_principals(
        category_keys=("statutory_identifier", "financial"),
        severity=breach.BreachSeverity.SEVERE,
        what_happened="A payroll export was readable without authentication.",
        became_aware_at=NOW,
        remediation="Access was revoked and the file deleted.",
        grievance_contact="dpo@acme.example",
    )
    joined = " ".join(draft.actions_advised)
    assert "myaadhaar.uidai.gov.in" in joined
    assert "bank" in joined
    assert "dpo@acme.example" in joined


def test_the_intimation_obligation_is_ordered_ahead_of_the_board_report():
    clock = breach.clock_from(NOW - timedelta(hours=30))
    out = breach.outstanding_obligations(
        clock=clock,
        severity=breach.BreachSeverity.SEVERE,
        principals_notified_at=None,
        board_reported_at=None,
        now=NOW,
    )
    # The intimation target is 24 hours, so at +30 it is overdue and the board
    # report at 72 is not.
    assert out[0].key == "intimate_principals"
    assert out[0].is_overdue
    assert not out[1].is_overdue


def test_a_done_obligation_is_still_listed():
    clock = breach.clock_from(NOW)
    out = breach.outstanding_obligations(
        clock=clock,
        severity=breach.BreachSeverity.HIGH,
        principals_notified_at=NOW,
        board_reported_at=None,
        now=NOW,
    )
    done = [o for o in out if o.is_done]
    assert len(done) == 1
    assert done[0].key == "intimate_principals"


def test_the_rehearsal_runs_end_to_end_and_is_labelled():
    result = breach.rehearse(now=NOW, grievance_contact="dpo@acme.example")
    assert result["is_rehearsal"]
    assert result["assessed_severity"] == "severe"
    assert result["notification"]["body"]
    assert "REHEARSAL" in result["notification"]["body"]
    assert result["obligations"]


# ══════════════════════════════════════════════════════════════════════════════
# Cross-border transfers
# ══════════════════════════════════════════════════════════════════════════════


def test_the_primary_database_is_in_india():
    assert not transfers.crosses_border("supabase")


def test_a_model_provider_is_recorded_as_cross_border():
    spec = transfers.sub_processor("anthropic")
    assert spec.is_ai_model_provider
    assert spec.is_cross_border


def test_an_unspecified_region_counts_as_cross_border():
    assert transfers.ProcessingLocation.UNSPECIFIED.is_cross_border


def test_bank_details_may_not_go_to_a_model_provider():
    with pytest.raises(transfers.TransferError) as exc:
        transfers.assert_transfer_allowed("anthropic", ("financial",))
    message = str(exc.value)
    assert "Bank and payment details" in message
    assert "cross-border" in message


def test_statutory_identifiers_may_not_go_to_a_model_provider():
    with pytest.raises(transfers.TransferError):
        transfers.assert_transfer_allowed("anthropic", ("statutory_identifier",))


def test_a_permitted_category_passes():
    transfers.assert_transfer_allowed("anthropic", ("culture_response",))


def test_an_unclassified_payload_cannot_be_cleared_to_leave():
    with pytest.raises(transfers.TransferError) as exc:
        transfers.assert_transfer_allowed("anthropic", ("mystery",))
    assert "cannot be cleared to leave" in str(exc.value)


def test_a_refusal_names_the_country():
    with pytest.raises(transfers.TransferError) as exc:
        transfers.assert_transfer_allowed("google_email", ("compensation",))
    assert "United States" in str(exc.value)


def test_every_purpose_sub_processor_is_registered():
    for spec in consent.PURPOSES.values():
        for key in spec.sub_processors:
            assert key in transfers.SUB_PROCESSORS, (
                f"Purpose {spec.key!r} names sub-processor {key!r}, which the "
                "register does not have."
            )


def test_the_trust_centre_lists_model_providers_first_and_separately():
    disclosure = transfers.trust_centre_disclosure()
    assert disclosure["ai_model_providers"]
    assert all(not s["is_ai_model_provider"] for s in disclosure["other_sub_processors"])
    assert disclosure["when_transfers_happen"]


def test_a_category_a_provider_may_not_receive_is_absent_from_its_permission():
    """The register and the handling flag agree about what may go to a model."""
    anthropic = transfers.sub_processor("anthropic")
    for key in anthropic.categories_permitted:
        assert CATEGORIES[key].may_leave_for_ai or key == "organisational", (
            f"{key} is permitted to Anthropic but is not flagged may_leave_for_ai."
        )


# ══════════════════════════════════════════════════════════════════════════════
# Inventory and the coverage gate
# ══════════════════════════════════════════════════════════════════════════════


def _fixture_schema() -> dict[str, tuple[str, ...]]:
    """A tiny schema standing in for the real one.

    Deliberately built by hand rather than reflected: the engine's job is to
    turn a table→columns mapping into an inventory, and testing it against the
    live schema would make every test in this section fail whenever an unrelated
    slice adds a column.
    """
    return {
        name: tuple(
            {
                "employees": (
                    "id",
                    "tenant_id",
                    "created_at",
                    "first_name",
                    "pan",
                    "grade",
                ),
                "departments": ("id", "tenant_id", "name"),
            }[name]
        )
        for name in ("employees", "departments")
    }


def test_the_inventory_resolves_columns_to_categories():
    inv = inventory.build_inventory(_fixture_schema())
    by_column = {c.column: c.category_key for c in inv.columns}
    assert by_column["pan"] == "statutory_identifier"
    assert by_column["first_name"] == "identity"
    assert by_column["grade"] == "organisational"


def test_structural_columns_are_not_personal_data():
    inv = inventory.build_inventory(_fixture_schema())
    assert "id" not in {c.column for c in inv.columns}
    assert "tenant_id" not in {c.column for c in inv.columns}


def test_a_non_personal_table_contributes_nothing():
    inv = inventory.build_inventory(_fixture_schema())
    assert "departments" not in {c.table for c in inv.columns}
    assert inv.tables_not_personal == 1


def test_an_unclassified_table_fails_the_gate():
    schema = _fixture_schema() | {"brand_new_table": ("id", "tenant_id", "notes")}
    gaps = inventory.coverage_gaps(schema)
    kinds = {g.kind for g in gaps}
    assert inventory.GapKind.UNCLASSIFIED_TABLE in kinds


def test_a_stale_table_fails_the_gate():
    schema = {"employees": _fixture_schema()["employees"]}
    gaps = inventory.coverage_gaps(schema)
    assert any(g.kind is inventory.GapKind.STALE_TABLE and g.table == "departments" for g in gaps)


def test_a_new_direct_identifier_cannot_inherit_the_table_default():
    """The check the whole classification exists for."""
    schema = dict(_fixture_schema())
    schema["employees"] = (*schema["employees"], "aadhaar_vid")
    gaps = inventory.coverage_gaps(schema)
    assert any(
        g.kind is inventory.GapKind.IMPLICIT_DIRECT_IDENTIFIER and g.column == "aadhaar_vid"
        for g in gaps
    )


def test_a_direct_identifier_in_a_non_personal_table_fails_the_gate():
    schema = dict(_fixture_schema())
    schema["departments"] = (*schema["departments"], "head_phone")
    gaps = inventory.coverage_gaps(schema)
    assert any(g.kind is inventory.GapKind.IDENTIFIER_IN_NON_PERSONAL_TABLE for g in gaps)


def test_a_classification_for_a_column_that_does_not_exist_fails_the_gate():
    schema = dict(_fixture_schema())
    schema["employees"] = tuple(c for c in schema["employees"] if c != "pan")
    gaps = inventory.coverage_gaps(schema)
    assert any(
        g.kind is inventory.GapKind.STALE_COLUMN_CLASSIFICATION and g.column == "pan" for g in gaps
    )


def test_direct_identifier_matching_is_on_word_boundaries():
    assert is_direct_identifier("pan")
    assert is_direct_identifier("pan_number")
    assert is_direct_identifier("landlord_pan")
    # The false positives the first substring implementation produced.
    assert not is_direct_identifier("participant_user_ids")
    assert not is_direct_identifier("panelist_id")
    assert not is_direct_identifier("current_company")


def test_the_ropa_is_organised_by_purpose_not_by_table():
    activities = inventory.record_of_processing(_fixture_schema())
    assert {a.purpose.key for a in activities} == set(consent.PURPOSES)


def test_a_purpose_with_no_data_behind_it_still_appears():
    activities = {a.purpose.key: a for a in inventory.record_of_processing(_fixture_schema())}
    assert activities["culture_analytics"].column_count == 0


def test_the_ropa_carries_the_binding_retention():
    activities = {a.purpose.key: a for a in inventory.record_of_processing(_fixture_schema())}
    payroll = activities["payroll_processing"]
    assert payroll.max_retention_months == max(
        category(k).retention.months for k in payroll.purpose.data_categories
    )
    assert payroll.statutory_retentions


def test_consent_does_not_dominate_the_lawful_basis_breakdown():
    """The number worth watching, asserted rather than hoped for."""
    breakdown = inventory.lawful_basis_breakdown(_fixture_schema())
    total = sum(breakdown.values())
    assert breakdown["consent"] < total / 2, (
        "More than half of all processing purposes rest on consent. That is the "
        "signal that consent has become the answer to every question."
    )


def test_every_exemption_carries_a_reason():
    from app.services.dpdp.schema_map import EXEMPT_COLUMNS

    for (table, column), reason in EXEMPT_COLUMNS.items():
        assert reason.strip(), f"{table}.{column} is exempted with no reason."
        assert len(reason) > 30, (
            f"{table}.{column}'s exemption reason is too short to be an argument."
        )


def test_every_non_personal_table_says_why():
    for name, classification in TABLES.items():
        if classification.principal is None:
            assert classification.reason.strip(), (
                f"{name} is declared to hold no personal data with no reason "
                "given. That is a judgement and an unexplained one is not "
                "reviewable."
            )
