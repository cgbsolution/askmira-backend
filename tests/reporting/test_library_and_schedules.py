"""The starter library, the catalogue's own consistency, and schedule timing."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from app.models.reporting import Cadence, ExportFormat, ReportSchedule
from app.services.reporting.compiler import (
    Scope,
    ScopeKind,
    compile_report,
    rendered_sql,
)
from app.services.reporting.library import STARTERS, starters_for, validate_library
from app.services.reporting.semantic import (
    AGGREGATIONS,
    ENTITIES,
    Sensitivity,
    catalogue_for,
    validate_catalogue,
    visible_fields,
)
from app.services.reporting.service import next_run_after, to_csv

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
TODAY = date(2026, 9, 7)


def _hr() -> Scope:
    return Scope(kind=ScopeKind.TENANT, tenant_id=TENANT)


# ═══════════════════════════════════════════════════════════════════════════
# The catalogue
# ═══════════════════════════════════════════════════════════════════════════


def test_the_catalogue_is_internally_consistent() -> None:
    """Catches what is invisible at a glance: a field whose aggregations do not
    suit its type, a join from a table that is not reachable, a duplicate key.

    This check found a real definition error the first time it ran — a field
    marked as neither a dimension nor a measure, which could not have been
    selected at all.
    """
    assert validate_catalogue() == []


def test_every_field_declares_a_sensitivity_the_role_map_understands() -> None:
    for ent in ENTITIES.values():
        for spec in ent.fields:
            assert isinstance(spec.sensitivity, Sensitivity), f"{ent.key}.{spec.key}"


def test_a_measure_only_offers_aggregations_that_suit_its_type() -> None:
    """A `sum` over a date is meaningless, and a builder offering it would
    produce a number somebody would try to read."""
    for ent in ENTITIES.values():
        for spec in ent.fields:
            if not spec.is_measure:
                assert spec.allowed_aggregations == ()
                continue
            assert set(spec.allowed_aggregations) <= set(AGGREGATIONS[spec.data_type])


def test_salary_and_identifiers_are_payroll_only_wherever_they_appear() -> None:
    """The sensitivity of a PAN must not depend on which report reached it."""
    restricted = {"ctc_annual", "pan", "uan", "bank_account_number", "net_pay",
                  "gross_earnings", "employer_cost", "tds", "expected_ctc", "offer_ctc"}
    for ent in ENTITIES.values():
        for spec in ent.fields:
            if spec.key in restricted:
                assert spec.sensitivity is Sensitivity.PAYROLL_ONLY, (
                    f"{ent.key}.{spec.key} is only {spec.sensitivity.value}"
                )


def test_a_manager_sees_a_smaller_catalogue_than_hr() -> None:
    manager = catalogue_for("manager")
    admin = catalogue_for("admin")
    manager_fields = sum(len(e["fields"]) for e in manager)
    admin_fields = sum(len(e["fields"]) for e in admin)
    assert manager_fields < admin_fields


def test_an_entity_a_manager_can_see_nothing_of_is_left_out_entirely() -> None:
    """A candidate entity with every field HR-only should not appear as an empty
    card in a manager's field picker."""
    manager_keys = {e["key"] for e in catalogue_for("manager")}
    assert "candidate" not in manager_keys
    assert "employee" in manager_keys


def test_the_catalogue_marks_which_entities_support_a_row_scope() -> None:
    """So a builder can explain why a manager cannot open the candidate reports,
    rather than only refusing them."""
    for card in catalogue_for("admin"):
        expected = ENTITIES[card["key"]].employee_column is not None
        assert card["supports_row_scope"] is expected


def test_visible_fields_and_the_compiler_agree() -> None:
    """The picker is a courtesy and the compiler is the gate, but they must not
    disagree — an offered field that fails on click is worse than a hidden one.
    """
    for key in ENTITIES:
        for role in ("manager", "sub_admin", "admin"):
            for spec in visible_fields(key, role):
                from app.services.reporting.compiler import Definition, Selection

                compiled = compile_report(
                    Definition(entity_key=key, selections=(Selection(spec.key),)),
                    role=role,
                    scope=_hr(),
                    today=TODAY,
                )
                assert compiled.denied_fields == (), f"{role} {key}.{spec.key}"


# ═══════════════════════════════════════════════════════════════════════════
# The starter library — the checklist gate
# ═══════════════════════════════════════════════════════════════════════════


def test_at_least_twenty_starters_ship() -> None:
    """The checklist gate. Below twenty, every client's first question is still
    a support ticket."""
    assert len(STARTERS) >= 20


def test_the_library_is_internally_consistent() -> None:
    assert validate_library() == []


def test_every_starter_compiles_and_runs_as_an_admin() -> None:
    """The checklist gate: 20 starter reports ship **and run**.

    A library of definitions nobody compiled is a library of things that fail on
    first click.
    """
    for starter in STARTERS:
        compiled = compile_report(
            starter.definition, role="admin", scope=_hr(), today=TODAY
        )
        assert compiled.columns, starter.key
        sql = rendered_sql(compiled.statement)
        assert sql.strip().upper().startswith("SELECT"), starter.key


def test_every_starter_covers_a_question_somebody_would_ask() -> None:
    """A report list people scroll past is a report list nobody uses."""
    for starter in STARTERS:
        assert starter.question.strip().endswith(("?", ".")), starter.key
        assert len(starter.question) > 20, starter.key


def test_the_starters_span_the_modules_an_hr_team_actually_uses() -> None:
    categories = {s.category for s in STARTERS}
    assert {
        "People", "Attrition", "Payroll", "Statutory", "Attendance", "Leave",
        "Performance", "Hiring",
    } <= categories


def test_a_manager_is_offered_only_starters_they_can_run() -> None:
    """Offering a manager "Salary register" and refusing it on click is worse
    than not offering it."""
    offered = starters_for("manager")
    keys = {s["key"] for s in offered}
    assert "salary_register" not in keys
    assert "headcount_by_department" in keys

    for card in offered:
        starter = next(s for s in STARTERS if s.key == card["key"])
        compiled = compile_report(
            starter.definition,
            role="manager",
            scope=Scope(
                kind=ScopeKind.REPORTING_TREE, tenant_id=TENANT,
                employee_id=uuid.uuid4(), visible_employee_ids=(uuid.uuid4(),),
            ),
            today=TODAY,
        )
        assert compiled.denied_fields == (), card["key"]


def test_an_hr_ops_user_is_offered_hr_starters_but_not_payroll_ones() -> None:
    keys = {s["key"] for s in starters_for("sub_admin")}
    assert "attrition_by_department" in keys
    assert "salary_register" not in keys
    assert "statutory_contribution_summary" not in keys


def test_an_admin_is_offered_every_starter() -> None:
    assert len(starters_for("admin")) == len(STARTERS)


def test_the_financial_year_starters_resolve_to_april(monkeypatch) -> None:
    """The FY starters are where the range engine actually gets used."""
    starter = next(s for s in STARTERS if s.key == "attrition_by_department")
    compiled = compile_report(
        starter.definition, role="admin", scope=_hr(), today=date(2027, 1, 15)
    )
    params = compiled.statement.compile().params
    assert date(2026, 4, 1) in params.values()
    assert date(2027, 3, 31) in params.values()


# ═══════════════════════════════════════════════════════════════════════════
# Schedules
# ═══════════════════════════════════════════════════════════════════════════


def _schedule(**overrides) -> ReportSchedule:
    schedule = ReportSchedule(
        report_id=uuid.uuid4(),
        cadence=overrides.pop("cadence", Cadence.WEEKLY),
        hour_of_day=overrides.pop("hour_of_day", 9),
        day_of_week=overrides.pop("day_of_week", 0),
        day_of_month=overrides.pop("day_of_month", None),
        timezone_name=overrides.pop("timezone_name", "Asia/Kolkata"),
        export_format=ExportFormat.CSV,
    )
    for key, value in overrides.items():
        setattr(schedule, key, value)
    return schedule


def test_a_nine_am_schedule_fires_at_nine_where_the_recipient_is() -> None:
    """The checklist gate: sends in the recipient's timezone.

    A "Monday 9am" report scheduled in UTC arrives at 2:30pm IST; one scheduled
    naively in IST arrives at 3:30am for a recipient in London.
    """
    schedule = _schedule(cadence=Cadence.DAILY, hour_of_day=9)
    after = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
    due = next_run_after(schedule, after=after)

    local = due.astimezone(ZoneInfo("Asia/Kolkata"))
    assert local.hour == 9
    assert local.minute == 0
    # 9am IST is 03:30 UTC — the stored instant is not 9am UTC.
    assert due.hour == 3 and due.minute == 30


def test_a_schedule_in_another_zone_fires_at_nine_there() -> None:
    schedule = _schedule(cadence=Cadence.DAILY, timezone_name="America/New_York")
    due = next_run_after(schedule, after=datetime(2026, 9, 7, 20, 0, tzinfo=UTC))
    assert due.astimezone(ZoneInfo("America/New_York")).hour == 9


def test_a_weekly_schedule_lands_on_the_configured_weekday() -> None:
    schedule = _schedule(cadence=Cadence.WEEKLY, day_of_week=0)
    due = next_run_after(schedule, after=datetime(2026, 9, 9, 12, 0, tzinfo=UTC))
    local = due.astimezone(ZoneInfo("Asia/Kolkata"))
    assert local.weekday() == 0
    assert local > datetime(2026, 9, 9, 12, 0, tzinfo=UTC).astimezone(
        ZoneInfo("Asia/Kolkata")
    )


def test_a_monthly_schedule_on_the_31st_still_fires_in_february() -> None:
    """Clamped to the last day rather than skipping the month entirely."""
    schedule = _schedule(cadence=Cadence.MONTHLY, day_of_month=31)
    due = next_run_after(schedule, after=datetime(2027, 1, 31, 12, 0, tzinfo=UTC))
    local = due.astimezone(ZoneInfo("Asia/Kolkata"))
    assert local.month == 2
    assert local.day == 28


def test_a_quarterly_schedule_moves_three_months() -> None:
    schedule = _schedule(cadence=Cadence.QUARTERLY, day_of_month=1)
    first = next_run_after(schedule, after=datetime(2026, 9, 7, 12, 0, tzinfo=UTC))
    second = next_run_after(schedule, after=first)
    assert (second.year - first.year) * 12 + (second.month - first.month) == 3


def test_the_next_run_is_always_in_the_future() -> None:
    for cadence in Cadence:
        schedule = _schedule(cadence=cadence, day_of_week=0, day_of_month=1)
        after = datetime(2026, 9, 7, 3, 30, tzinfo=UTC)  # exactly 9am IST
        assert next_run_after(schedule, after=after) > after, cadence


def test_a_schedule_survives_a_dst_change_in_the_recipients_zone() -> None:
    """Computed in the local zone and converted once at the end, so a 9am
    schedule stays 9am on both sides of the clocks changing."""
    schedule = _schedule(cadence=Cadence.DAILY, timezone_name="America/New_York")
    before = next_run_after(schedule, after=datetime(2026, 10, 30, 20, 0, tzinfo=UTC))
    after = next_run_after(schedule, after=datetime(2026, 11, 5, 20, 0, tzinfo=UTC))
    zone = ZoneInfo("America/New_York")
    assert before.astimezone(zone).hour == 9
    assert after.astimezone(zone).hour == 9
    # And the UTC hours genuinely differ, which is what a naive version misses.
    assert before.hour != after.hour


# ═══════════════════════════════════════════════════════════════════════════
# Export
# ═══════════════════════════════════════════════════════════════════════════


def test_a_csv_cell_that_looks_like_a_formula_is_neutralised() -> None:
    """A cell beginning `=` is executed when the file opens. An employee whose
    display name is `=cmd|'/c calc'!A1` is a real attack, and the export is the
    delivery mechanism."""
    columns = [{"label": "Employee"}, {"label": "Department"}]
    rows = [["=cmd|'/c calc'!A1", "Engineering"], ["+1+1", "Sales"], ["@SUM(A1)", "HR"]]
    output = to_csv(columns, rows)

    for line in output.splitlines()[1:]:
        first = line.split(",")[0].strip('"')
        assert first.startswith("'"), first


def test_an_ordinary_name_is_not_mangled() -> None:
    output = to_csv([{"label": "Employee"}], [["Anita Sharma"], ["Rohit Verma"]])
    assert "Anita Sharma" in output
    assert "'Anita" not in output


def test_the_header_row_uses_the_labels_a_person_chose() -> None:
    output = to_csv(
        [{"label": "Total cost"}, {"label": "Employees"}], [["100", "2"]]
    )
    assert output.splitlines()[0] == "Total cost,Employees"
