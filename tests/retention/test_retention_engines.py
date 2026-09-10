"""Slice 26 — retention, anonymisation and the planner.

Organised by the thing that would go wrong: a floor configured below a statute,
an "anonymised" record that names somebody, a legal hold that missed the records
a filter had already dropped, a job that deleted four thousand rows unattended.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.services.dpdp.categories import CATEGORIES
from app.services.retention import anonymise, planner, policy, taxonomy

TODAY = date(2026, 9, 7)
SECRET = "a" * 48


# ══════════════════════════════════════════════════════════════════════════════
# The taxonomy
# ══════════════════════════════════════════════════════════════════════════════


def test_the_taxonomy_does_not_contradict_itself():
    assert taxonomy.contradictions() == []


def test_every_category_has_an_expiry_action():
    for key in CATEGORIES:
        assert taxonomy.action_for(key) is not None


def test_a_category_naming_a_statute_is_in_the_statutory_tier():
    """The bug the first version of the taxonomy shipped with."""
    for key, cat in CATEGORIES.items():
        if cat.retention.is_statutory:
            assert taxonomy.tier_for(key) is taxonomy.RetentionTier.STATUTORY_RETAINED, (
                f"{key} cites {cat.retention.statutory_reference} and is not in "
                "the statutory tier, so the job could act on it while an "
                "obligation was live."
            )


def test_a_statutory_attendance_register_is_still_anonymised_at_the_end():
    """The tier and the action are different questions, and this is why."""
    assert taxonomy.tier_for("attendance") is taxonomy.RetentionTier.STATUTORY_RETAINED
    assert taxonomy.action_for("attendance") is taxonomy.RetentionAction.ANONYMISE


def test_a_consent_based_category_is_always_deleted_never_anonymised():
    for key in CATEGORIES:
        if taxonomy.tier_for(key) is taxonomy.RetentionTier.CONSENT_BASED:
            assert taxonomy.action_for(key) is taxonomy.RetentionAction.DELETE, (
                f"{key} is held on consent and is {taxonomy.action_for(key).value}d "
                "at expiry. Anonymising keeps the value of data somebody told us "
                "to stop holding."
            )


def test_only_the_statutory_tier_survives_an_erasure_request():
    for tier in taxonomy.RetentionTier:
        assert tier.survives_an_erasure_request == (
            tier is taxonomy.RetentionTier.STATUTORY_RETAINED
        )


def test_the_trust_centre_states_the_conflict_and_the_backup_position():
    page = taxonomy.trust_centre_taxonomy()
    assert "Both are binding" in page["the_conflict"]
    assert "not deletion" in page["anonymisation_is_not_deletion"]
    assert page["backups"]["retention_window_days"] > 0
    assert "not_yet_built" in page["backups"]
    assert {t["tier"] for t in page["tiers"]} == {
        t.value for t in taxonomy.RetentionTier
    }


def test_an_unknown_category_has_no_action_and_says_so():
    with pytest.raises(taxonomy.TaxonomyError) as exc:
        taxonomy.action_for("not_a_category")
    assert "forgot to classify" in str(exc.value)


# ══════════════════════════════════════════════════════════════════════════════
# The statutory floor
# ══════════════════════════════════════════════════════════════════════════════


def test_a_tenant_cannot_configure_below_the_statutory_minimum():
    with pytest.raises(policy.PolicyError) as exc:
        policy.validate_policy(
            data_category_key="compensation", retention_period_months=24
        )
    message = str(exc.value)
    assert "96 months" in message
    assert "Payment of Wages Act" in message
    assert "24" in message


def test_the_refusal_says_it_is_not_ours_to_unlock():
    with pytest.raises(policy.PolicyError) as exc:
        policy.validate_policy(
            data_category_key="tax", retention_period_months=12
        )
    assert "not a setting AskMira can unlock" in str(exc.value)


def test_a_tenant_can_configure_above_the_minimum():
    written = policy.validate_policy(
        data_category_key="compensation", retention_period_months=120
    )
    assert written.retention_period_months == 120
    assert written.statutory_minimum_months == 96


def test_a_category_with_no_statute_has_a_floor_of_zero():
    assert policy.statutory_minimum_months("culture_response") == 0
    written = policy.validate_policy(
        data_category_key="culture_response", retention_period_months=1
    )
    assert written.retention_period_months == 1


def test_a_consent_category_cannot_be_configured_to_anonymise():
    with pytest.raises(policy.PolicyError) as exc:
        policy.validate_policy(
            data_category_key="culture_response",
            retention_period_months=24,
            action=taxonomy.RetentionAction.ANONYMISE,
        )
    assert "only lawful action at expiry is deletion" in str(exc.value)


def test_zero_months_is_refused():
    with pytest.raises(policy.PolicyError):
        policy.validate_policy(
            data_category_key="culture_response", retention_period_months=0
        )


def test_the_system_defaults_are_the_floor_exactly():
    for default in policy.default_policies():
        cat = CATEGORIES[default.data_category_key]
        assert default.retention_period_months == cat.retention.months


def test_every_category_gets_a_default_policy():
    keys = {p.data_category_key for p in policy.default_policies()}
    assert keys == set(CATEGORIES)


# ══════════════════════════════════════════════════════════════════════════════
# Anchors
# ══════════════════════════════════════════════════════════════════════════════


def test_an_employment_anchored_policy_has_no_expiry_for_a_current_employee():
    """The return that matters. A date here would schedule a live record."""
    written = policy.validate_policy(
        data_category_key="identity", retention_period_months=96
    )
    assert written.anchor_event is policy.AnchorEvent.EMPLOYMENT_ENDED
    assert policy.due_on(written, record_created=date(2020, 5, 1)) is None


def test_an_employment_anchored_policy_expires_from_the_last_working_day():
    written = policy.validate_policy(
        data_category_key="identity", retention_period_months=96
    )
    assert policy.due_on(
        written,
        record_created=date(2020, 5, 1),
        employment_ended=date(2024, 1, 31),
    ) == date(2032, 1, 31)


def test_a_financial_year_anchor_runs_from_31_march():
    written = policy.validate_policy(
        data_category_key="compensation", retention_period_months=96
    )
    assert written.anchor_event is policy.AnchorEvent.FINANCIAL_YEAR_END
    # A record created in January 2024 falls in FY 2023-24, ending 31 March 2024.
    assert policy.due_on(written, record_created=date(2024, 1, 15)) == date(
        2032, 3, 31
    )


def test_a_january_record_is_not_credited_to_the_next_financial_year():
    # Built directly rather than through validate_policy: a twelve-month period
    # on a category with a 96-month statutory floor is refused, which is the
    # correct behaviour and gets in the way of testing the arithmetic.
    written = policy.RetentionPolicy(
        data_category_key="compensation",
        retention_period_months=12,
        anchor_event=policy.AnchorEvent.FINANCIAL_YEAR_END,
        action=taxonomy.RetentionAction.DELETE,
        statutory_minimum_months=96,
        legal_basis="test",
    )
    january = policy.due_on(written, record_created=date(2024, 1, 15))
    april = policy.due_on(written, record_created=date(2024, 4, 15))
    assert january == date(2025, 3, 31)
    assert april == date(2026, 3, 31)


def test_a_last_accessed_anchor_has_no_expiry_until_something_reads_it():
    written = policy.RetentionPolicy(
        data_category_key="communication",
        retention_period_months=6,
        anchor_event=policy.AnchorEvent.LAST_ACCESSED,
        action=taxonomy.RetentionAction.ANONYMISE,
        statutory_minimum_months=0,
        legal_basis="policy",
    )
    assert policy.due_on(written, record_created=date(2024, 1, 1)) is None
    assert policy.due_on(
        written, record_created=date(2024, 1, 1), last_accessed=date(2025, 1, 31)
    ) == date(2025, 7, 31)


def test_a_month_end_anchor_does_not_overflow_into_the_next_month():
    written = policy.RetentionPolicy(
        data_category_key="leave",
        retention_period_months=1,
        anchor_event=policy.AnchorEvent.RECORD_CREATED,
        action=taxonomy.RetentionAction.ANONYMISE,
        statutory_minimum_months=0,
        legal_basis="policy",
    )
    assert policy.due_on(written, record_created=date(2024, 1, 31)) == date(
        2024, 2, 29
    )


# ══════════════════════════════════════════════════════════════════════════════
# Anonymisation
# ══════════════════════════════════════════════════════════════════════════════


def _person(**overrides):
    base = {
        "full_name": "Priya Ramesh",
        "work_email": "priya.ramesh@acme.test",
        "pan": "ABCDE1234F",
        "date_of_birth": date(1991, 3, 14),
        "date_of_joining": date(2019, 7, 1),
        "designation": "Senior Data Engineer",
        "city": "Bengaluru",
        "ctc_annual": 4200000,
        "department": "Data",
        "grade": "M3",
    }
    return base | overrides


def test_direct_identifiers_are_removed():
    result = anonymise.anonymise_record(
        _person(), subject_id="emp-1", tenant_secret=SECRET
    )
    for field in ("full_name", "work_email", "pan"):
        assert field not in result.record
        assert field in result.direct_identifiers_removed


def test_quasi_identifiers_are_generalised_not_kept():
    result = anonymise.anonymise_record(
        _person(), subject_id="emp-1", tenant_secret=SECRET
    )
    assert result.record["date_of_birth"] == 1991
    assert result.record["date_of_joining"] == "2015-2019"
    assert result.record["designation"] == "Engineering"
    assert result.record["ctc_annual"] == "40L-60L"


def test_the_surrogate_is_stable_across_records():
    a = anonymise.anonymise_record(
        _person(), subject_id="emp-1", tenant_secret=SECRET
    )
    b = anonymise.anonymise_record(
        _person(department="Platform"), subject_id="emp-1", tenant_secret=SECRET
    )
    assert a.surrogate == b.surrogate


def test_the_surrogate_differs_between_tenants():
    a = anonymise.surrogate_key("emp-1", tenant_secret=SECRET)
    b = anonymise.surrogate_key("emp-1", tenant_secret="b" * 48)
    assert a != b


def test_a_weak_secret_is_refused():
    with pytest.raises(anonymise.AnonymisationError) as exc:
        anonymise.surrogate_key("emp-1", tenant_secret="short")
    assert "reversible" in str(exc.value)


def test_verification_passes_a_properly_anonymised_record():
    record = _person()
    result = anonymise.anonymise_record(
        record, subject_id="emp-1", tenant_secret=SECRET
    )
    anonymise.verify_irreversible(
        original=record, result=result, subject_id="emp-1"
    )


def test_verification_catches_an_identifier_that_survived():
    record = _person()
    leaky = anonymise.AnonymisationResult(
        record={"subject": "anon-x", "note": "priya.ramesh@acme.test"},
        surrogate="anon-x",
        direct_identifiers_removed=(),
        fields_generalised=(),
        fields_suppressed=(),
    )
    with pytest.raises(anonymise.AnonymisationError) as exc:
        anonymise.verify_irreversible(
            original=record, result=leaky, subject_id="emp-1"
        )
    assert "work_email" in str(exc.value)


def test_verification_catches_a_surrogate_derived_from_something_readable():
    """The realistic mistake: 'stable, so derive it from the email'."""
    record = _person()
    leaky = anonymise.AnonymisationResult(
        record={"subject": "anon-priya.ramesh@acme.test"},
        surrogate="anon-priya.ramesh@acme.test",
        direct_identifiers_removed=("work_email",),
        fields_generalised=(),
        fields_suppressed=(),
    )
    with pytest.raises(anonymise.AnonymisationError):
        anonymise.verify_irreversible(
            original=record, result=leaky, subject_id="emp-1"
        )


def test_verification_catches_the_subject_id_surviving():
    record = _person()
    leaky = anonymise.AnonymisationResult(
        record={"subject": "anon-x", "employee_ref": "employee-4471"},
        surrogate="anon-x",
        direct_identifiers_removed=(),
        fields_generalised=(),
        fields_suppressed=(),
    )
    with pytest.raises(anonymise.AnonymisationError) as exc:
        anonymise.verify_irreversible(
            original=record, result=leaky, subject_id="employee-4471"
        )
    assert "joins straight back" in str(exc.value)


def test_verification_catches_a_full_date_of_birth_surviving():
    record = _person()
    leaky = anonymise.AnonymisationResult(
        record={"subject": "anon-x", "date_of_birth": date(1991, 3, 14)},
        surrogate="anon-x",
        direct_identifiers_removed=(),
        fields_generalised=(),
        fields_suppressed=(),
    )
    with pytest.raises(anonymise.AnonymisationError) as exc:
        anonymise.verify_irreversible(
            original=record, result=leaky, subject_id="emp-1"
        )
    assert "full date" in str(exc.value)


def test_a_suppressed_field_is_dropped_not_blanked():
    result = anonymise.anonymise_record(
        _person(gender="female", postal_code="560001"),
        subject_id="emp-1",
        tenant_secret=SECRET,
    )
    assert "gender" not in result.record
    assert "postal_code" not in result.record
    assert "gender" in result.fields_suppressed


def test_an_unparseable_salary_does_not_survive_as_itself():
    result = anonymise.anonymise_record(
        _person(ctc_annual="forty-two lakh"),
        subject_id="emp-1",
        tenant_secret=SECRET,
    )
    assert result.record["ctc_annual"] == "unknown"


# ══════════════════════════════════════════════════════════════════════════════
# k-anonymity
# ══════════════════════════════════════════════════════════════════════════════


def test_a_singleton_fails_the_k_check():
    rows = [
        {"department": "Data", "grade": "M3", "city": "Bengaluru"},
        *[
            {"department": "Eng", "grade": "M1", "city": "Pune"}
            for _ in range(6)
        ],
    ]
    report = anonymise.check_k_anonymity(rows, k=5)
    assert not report.is_anonymous
    assert report.singletons == 1
    assert report.records_at_risk == 1
    assert "identify somebody" in report.as_dict()["recommendation"]


def test_a_set_where_every_group_is_large_enough_passes():
    rows = [
        {"department": "Eng", "grade": "M1", "city": "Pune"} for _ in range(6)
    ]
    report = anonymise.check_k_anonymity(rows, k=5)
    assert report.is_anonymous
    assert report.singletons == 0


def test_a_group_below_k_but_above_one_still_fails():
    rows = [
        *[{"department": "Eng", "grade": "M1"} for _ in range(6)],
        *[{"department": "Data", "grade": "M3"} for _ in range(3)],
    ]
    report = anonymise.check_k_anonymity(rows, k=5)
    assert not report.is_anonymous
    assert report.singletons == 0
    assert report.records_at_risk == 3


def test_k_of_one_is_refused():
    with pytest.raises(anonymise.AnonymisationError) as exc:
        anonymise.check_k_anonymity([{"a": 1}], k=1)
    assert "definition of identifiable" in str(exc.value)


def test_suppression_drops_only_the_failing_groups():
    rows = [
        {"department": "Data", "grade": "M3"},
        *[{"department": "Eng", "grade": "M1"} for _ in range(6)],
    ]
    kept, report = anonymise.suppress_failing_groups(rows, k=5)
    assert len(kept) == 6
    assert all(r["department"] == "Eng" for r in kept)
    assert report.singletons == 1


def test_suppression_leaves_an_already_anonymous_set_alone():
    rows = [{"department": "Eng", "grade": "M1"} for _ in range(6)]
    kept, report = anonymise.suppress_failing_groups(rows, k=5)
    assert len(kept) == len(rows)
    assert report.is_anonymous


# ══════════════════════════════════════════════════════════════════════════════
# Planning
# ══════════════════════════════════════════════════════════════════════════════


def _policies():
    return {p.data_category_key: p for p in policy.default_policies()}


def _record(**overrides):
    base = {
        "record_id": "r1",
        "table": "leave_requests",
        "data_category_key": "leave",
        "subject_id": "e1",
        "record_created": date(2018, 1, 1),
        "department": "Eng",
    }
    return planner.CandidateRecord(**(base | overrides))


def test_every_record_reaches_exactly_one_disposition():
    records = [
        _record(record_id="due"),
        _record(record_id="future", record_created=date(2026, 1, 1)),
        _record(record_id="nopolicy", data_category_key="performance"),
    ]
    result = planner.plan(
        records,
        policies=_policies(),
        legal_holds=[],
        today=TODAY,
    )
    assert len(result.actions) == len(records)
    assert len({a.record.record_id for a in result.actions}) == 3


def test_a_legal_hold_exempts_a_record_from_a_statutory_expiry():
    """Held is held, even against a policy that has already expired."""
    records = [_record(record_id="due", subject_id="e1")]
    held = planner.plan(
        records,
        policies=_policies(),
        legal_holds=[planner.LegalHoldScope(employee_ids=frozenset({"e1"}))],
        today=TODAY,
    )
    assert held.actions[0].disposition is planner.Disposition.HELD
    assert held.to_act == ()

    unheld = planner.plan(
        records, policies=_policies(), legal_holds=[], today=TODAY
    )
    assert unheld.actions[0].disposition is planner.Disposition.ACT


def test_a_hold_with_an_empty_scope_covers_everything():
    """The broadest possible hold must not read as no hold at all."""
    result = planner.plan(
        [_record(), _record(record_id="r2", subject_id="e9", department="Sales")],
        policies=_policies(),
        legal_holds=[planner.LegalHoldScope()],
        today=TODAY,
    )
    assert all(a.disposition is planner.Disposition.HELD for a in result.actions)


def test_a_department_scoped_hold_covers_only_that_department():
    result = planner.plan(
        [
            _record(record_id="eng", department="Eng"),
            _record(record_id="sales", department="Sales"),
        ],
        policies=_policies(),
        legal_holds=[planner.LegalHoldScope(departments=frozenset({"Eng"}))],
        today=TODAY,
    )
    by_id = {a.record.record_id: a for a in result.actions}
    assert by_id["eng"].disposition is planner.Disposition.HELD
    assert by_id["sales"].disposition is planner.Disposition.ACT


def test_a_category_with_no_policy_is_blocked_not_defaulted():
    result = planner.plan(
        [_record(data_category_key="performance")],
        policies={},
        legal_holds=[],
        today=TODAY,
    )
    action = result.actions[0]
    assert action.disposition is planner.Disposition.BLOCKED
    assert "kept indefinitely" in action.reason


def test_an_inactive_policy_blocks_rather_than_acting():
    policies = _policies()
    live = policies["leave"]
    policies["leave"] = policy.RetentionPolicy(
        data_category_key=live.data_category_key,
        retention_period_months=live.retention_period_months,
        anchor_event=live.anchor_event,
        action=live.action,
        statutory_minimum_months=live.statutory_minimum_months,
        legal_basis=live.legal_basis,
        active=False,
    )
    result = planner.plan(
        [_record()], policies=policies, legal_holds=[], today=TODAY
    )
    assert result.actions[0].disposition is planner.Disposition.BLOCKED


def test_a_clock_that_has_not_started_is_blocked_and_named():
    result = planner.plan(
        [_record(data_category_key="identity", employment_ended=None)],
        policies=_policies(),
        legal_holds=[],
        today=TODAY,
    )
    action = result.actions[0]
    assert action.disposition is planner.Disposition.BLOCKED
    assert "last working day" in action.reason


def test_a_not_yet_due_record_reports_its_date():
    result = planner.plan(
        [_record(record_created=date(2026, 1, 1))],
        policies=_policies(),
        legal_holds=[],
        today=TODAY,
    )
    action = result.actions[0]
    assert action.disposition is planner.Disposition.NOT_YET_DUE
    assert action.due_on is not None


def test_a_dry_run_is_never_execute_ready():
    result = planner.plan(
        [_record()],
        policies=_policies(),
        legal_holds=[],
        today=TODAY,
        is_dry_run=True,
    )
    assert not result.execute_ready
    assert result.as_dict()["is_dry_run"]


def test_a_dry_run_cannot_be_approved():
    result = planner.plan(
        [_record()],
        policies=_policies(),
        legal_holds=[],
        today=TODAY,
        is_dry_run=True,
    )
    with pytest.raises(ValueError) as exc:
        planner.approve(result, user_id="u1")
    assert "produced to be read" in str(exc.value)


def test_a_plan_over_the_threshold_needs_approval():
    records = [_record(record_id=f"r{i}", subject_id=f"e{i}") for i in range(10)]
    result = planner.plan(
        records,
        policies=_policies(),
        legal_holds=[],
        today=TODAY,
        approval_threshold=5,
        is_dry_run=False,
    )
    assert result.needs_approval
    assert not result.execute_ready

    approved = planner.approve(result, user_id="u1")
    assert approved.execute_ready
    assert approved.approved_by_user_id == "u1"


def test_a_plan_under_the_threshold_runs_without_approval():
    result = planner.plan(
        [_record()],
        policies=_policies(),
        legal_holds=[],
        today=TODAY,
        approval_threshold=5,
        is_dry_run=False,
    )
    assert not result.needs_approval
    assert result.execute_ready


def test_approving_returns_a_new_plan_and_leaves_the_original_intact():
    result = planner.plan(
        [_record(record_id=f"r{i}", subject_id=f"e{i}") for i in range(10)],
        policies=_policies(),
        legal_holds=[],
        today=TODAY,
        approval_threshold=5,
        is_dry_run=False,
    )
    approved = planner.approve(result, user_id="u1")
    assert result.approved_by_user_id is None
    assert approved.approved_by_user_id == "u1"


def test_the_plan_summary_samples_rather_than_dumping():
    records = [_record(record_id=f"r{i}", subject_id=f"e{i}") for i in range(60)]
    result = planner.plan(
        records, policies=_policies(), legal_holds=[], today=TODAY
    )
    summary = result.as_dict(sample_size=20)
    assert summary["to_act"] == 60
    assert len(summary["sample"]) == 20


def test_held_and_blocked_are_counted_separately():
    result = planner.plan(
        [
            _record(record_id="held", subject_id="e1"),
            _record(
                record_id="blocked",
                subject_id="e3",
                data_category_key="performance",
            ),
            _record(record_id="due", subject_id="e2"),
        ],
        policies={"leave": _policies()["leave"]},
        legal_holds=[planner.LegalHoldScope(employee_ids=frozenset({"e1"}))],
        today=TODAY,
    )
    summary = result.as_dict()
    assert summary["held_under_legal_hold"] == 1
    assert summary["blocked"] == 1
    assert summary["to_act"] == 1
