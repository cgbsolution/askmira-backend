"""Slice 37 — exposed schema, incremental export, and benchmark suppression.

Organised by the way each control fails: an internal rename that breaks a
client's dashboard, a second copy of the sensitivity rules that drifts, a
nightly full dump, an audit row with a count and no field list, a benchmark cell
that names three companies, and an average with no distribution.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.services.bi import benchmarks, export, exposed_schema
from app.services.reporting.semantic import ENTITIES, Sensitivity, may_see

D = Decimal
W = datetime(2026, 9, 1, tzinfo=UTC)


# ══════════════════════════════════════════════════════════════════════════════
# The exposed schema
# ══════════════════════════════════════════════════════════════════════════════


def test_every_exposed_field_resolves_to_a_semantic_field():
    """An exposed column with no semantic field behind it has no sensitivity,
    and "no sensitivity" defaults to visible."""
    for entity in exposed_schema.EXPOSED_SCHEMA.values():
        for field in entity.fields:
            assert isinstance(field.sensitivity, Sensitivity), (
                f"{entity.name}.{field.name} does not resolve."
            )


def test_a_renamed_internal_field_is_caught_rather_than_silently_exposed():
    """This is exactly the rename that breaks a client's dashboard."""
    broken = exposed_schema.ExposedField(
        name="full_name",
        entity_key="employee",
        source="a_field_that_does_not_exist",
        description="x",
    )
    with pytest.raises(exposed_schema.SchemaError) as exc:
        _ = broken.sensitivity
    assert "repoint the mapping" in str(exc.value)


def test_the_exposed_name_is_independent_of_the_internal_name():
    """`full_name` is the client's contract; `employee_name` is ours."""
    field = next(
        f
        for f in exposed_schema.exposed_entity("dim_employee").fields
        if f.name == "full_name"
    )
    assert field.source == "employee_name"
    assert field.name != field.source


def test_sensitivity_is_not_restated_in_the_exposed_schema():
    """A second copy of "which fields are payroll-only" is a second copy that
    drifts, and the one that drifts is the one nobody is testing."""
    for entity in exposed_schema.EXPOSED_SCHEMA.values():
        semantic = ENTITIES[entity.entity_key]
        for field in entity.fields:
            source_field = next(f for f in semantic.fields if f.key == field.source)
            assert field.sensitivity is source_field.sensitivity


def test_a_role_sees_the_same_fields_it_would_see_in_a_report():
    for entity_name, entity in exposed_schema.EXPOSED_SCHEMA.items():
        for role in ("employee", "manager", "sub_admin", "admin"):
            visible = exposed_schema.visible_columns(entity_name, role)
            for field in visible:
                assert may_see(role, field.sensitivity)
            hidden = set(entity.fields) - set(visible)
            for field in hidden:
                assert not may_see(role, field.sensitivity)


def test_an_unknown_role_gets_the_narrowest_view():
    """A new role added elsewhere should reduce what a connector can reach,
    never widen it by falling through a lookup."""
    visible = exposed_schema.visible_columns("fact_payslip", "some_new_role")
    assert all(f.sensitivity is Sensitivity.PUBLIC for f in visible)


def test_a_raw_table_is_not_reachable():
    """Raw tables are ours to rename, and a client cannot build on them."""
    with pytest.raises(exposed_schema.SchemaError) as exc:
        exposed_schema.exposed_entity("employees")
    assert "published schema only" in str(exc.value)


def test_the_schema_document_does_not_publish_internal_names():
    """A client who learns the internal name starts depending on it, and the
    indirection stops working."""
    document = exposed_schema.schema_document()
    for entity in document["entities"]:
        for column in entity["columns"]:
            assert "source" not in column


def test_the_schema_document_states_its_stability_guarantee():
    document = exposed_schema.schema_document()
    assert document["schema_version"] == exposed_schema.SCHEMA_VERSION
    assert "internal rename repoints a mapping" in document["stability"]


def test_the_document_is_filtered_to_the_role_that_asks():
    """A client with a reporting-only credential is documented the surface they
    actually have, rather than one that half-fails at export time."""
    narrow = exposed_schema.schema_document(role="manager")
    payslip = next(e for e in narrow["entities"] if e["name"] == "fact_payslip")
    assert "net_pay" not in {c["name"] for c in payslip["columns"]}


def test_leave_reason_is_not_exposed_at_all():
    """Free text an employee wrote about why they were away, routinely medical.
    A BI extract is the last place it should end up."""
    document = exposed_schema.schema_document()
    names = {
        column["name"]
        for entity in document["entities"]
        for column in entity["columns"]
    }
    assert "leave_reason" not in names


# ══════════════════════════════════════════════════════════════════════════════
# Export planning
# ══════════════════════════════════════════════════════════════════════════════


def plan(**kw):
    body = {
        "entity_name": "fact_payslip",
        "role": "admin",
        "kind": export.ExportKind.INCREMENTAL,
        "destination": export.Destination.OBJECT_STORAGE,
        "destination_ref": "s3://acme-bi/",
        "since": W,
    }
    body.update(kw)
    return export.plan_export(**body)


def test_an_incremental_export_needs_a_watermark():
    """Without one it is a full export wearing an incremental label."""
    with pytest.raises(export.ExportError) as exc:
        plan(since=None)
    assert "full export wearing an incremental label" in str(exc.value)


def test_a_full_export_does_not_take_a_watermark():
    with pytest.raises(export.ExportError):
        plan(kind=export.ExportKind.FULL, since=W)


def test_an_export_needs_a_destination_reference():
    """"To where" is half of what the audit row is for."""
    with pytest.raises(export.ExportError):
        plan(destination_ref="   ")


def test_a_field_the_role_cannot_see_is_withheld_and_named():
    """A client whose dashboard has an empty column will ask, and "you do not
    have access" beats a column that quietly stopped populating."""
    result = plan(role="manager", requested_fields=["employee_code", "net_pay"])
    assert result.field_names == ("employee_code",)
    assert result.withheld_fields == ("net_pay",)


def test_a_field_outside_the_published_schema_is_refused():
    with pytest.raises(export.ExportError) as exc:
        plan(requested_fields=["employee_code", "aadhaar"])
    assert "never becomes part of a query" in str(exc.value)


def test_an_export_of_no_visible_fields_is_refused():
    """A scheduled job producing an empty file every night looks like it is
    working."""
    with pytest.raises(export.ExportError) as exc:
        plan(role="employee", requested_fields=["net_pay"])
    assert "looks like it is working" in str(exc.value)


def test_the_plan_flags_whether_payroll_data_leaves():
    """"Did salary leave the building" is answered by a boolean at the top of
    the audit row, not by reading forty field names."""
    assert plan(role="admin").carries_payroll_data is True
    assert plan(role="manager").carries_payroll_data is False


# ══════════════════════════════════════════════════════════════════════════════
# Extraction
# ══════════════════════════════════════════════════════════════════════════════


ROWS = [
    {
        "employee_code": "E1",
        "net_pay": 100,
        "gross_pay": 120,
        "updated_at": datetime(2026, 9, 5, tzinfo=UTC),
    },
    {
        "employee_code": "E2",
        "net_pay": 200,
        "gross_pay": 240,
        "updated_at": datetime(2026, 8, 20, tzinfo=UTC),
    },
    {
        "employee_code": "E3",
        "net_pay": 300,
        "gross_pay": 360,
        "updated_at": datetime(2026, 9, 9, tzinfo=UTC),
    },
]


def test_an_incremental_export_carries_only_changed_rows():
    """Re-exporting every employee every night multiplies the personal data in
    flight by the number of nights, for no benefit."""
    result = export.extract_rows(plan(), source_rows=ROWS)
    assert result.row_count == 2


def test_a_full_export_carries_everything_and_says_so():
    result = export.extract_rows(
        plan(kind=export.ExportKind.FULL, since=None), source_rows=ROWS
    )
    assert result.row_count == 3
    assert "**Full export**" in result.note


def test_the_rows_are_projected_onto_the_planned_fields():
    """A source row carrying a salary cannot leak through an export that was
    not entitled to it."""
    result = export.extract_rows(
        plan(role="manager", requested_fields=["employee_code", "net_pay"]),
        source_rows=ROWS,
    )
    assert all(set(row) == {"employee_code"} for row in result.rows)


def test_the_watermark_advances_to_the_largest_key_seen_not_to_now():
    """Advancing to now skips any row written during the export, and that row is
    then never exported again."""
    result = export.extract_rows(plan(), source_rows=ROWS)
    assert result.next_watermark == datetime(2026, 9, 9, tzinfo=UTC)


def test_an_empty_delta_leaves_the_watermark_where_it_was():
    result = export.extract_rows(
        plan(since=datetime(2026, 12, 1, tzinfo=UTC)), source_rows=ROWS
    )
    assert result.row_count == 0
    assert result.next_watermark == datetime(2026, 12, 1, tzinfo=UTC)


def test_a_change_key_that_does_not_move_on_update_is_refused():
    with pytest.raises(export.ExportError):
        export.extract_rows(plan(), source_rows=ROWS, change_key="employee_code")


# ══════════════════════════════════════════════════════════════════════════════
# The audit row
# ══════════════════════════════════════════════════════════════════════════════


def test_the_audit_row_carries_the_field_list():
    """The question after an incident is what left, not how much."""
    result = export.extract_rows(plan(), source_rows=ROWS)
    audit = export.audit_payload(result, at=datetime(2026, 9, 10, tzinfo=UTC))
    assert audit["fields"]
    assert audit["field_count"] == len(audit["fields"])
    assert audit["row_count"] == 2
    assert audit["carries_payroll_data"] is True


def test_the_audit_row_names_the_destination_and_the_role():
    result = export.extract_rows(plan(), source_rows=ROWS)
    audit = export.audit_payload(result, at=datetime(2026, 9, 10, tzinfo=UTC))
    assert audit["destination_ref"] == "s3://acme-bi/"
    assert audit["role"] == "admin"
    assert audit["kind"] == "incremental"


def test_the_audit_row_distinguishes_a_full_export():
    """"The nightly delta" and "somebody took everything" must not look the
    same afterwards."""
    result = export.extract_rows(
        plan(kind=export.ExportKind.FULL, since=None), source_rows=ROWS
    )
    audit = export.audit_payload(result, at=datetime(2026, 9, 10, tzinfo=UTC))
    assert audit["kind"] == "full"


def test_the_audit_row_records_the_schema_version():
    """A client debugging a broken dashboard needs to know which version the
    export was produced under."""
    result = export.extract_rows(plan(), source_rows=ROWS)
    audit = export.audit_payload(result, at=datetime(2026, 9, 10, tzinfo=UTC))
    assert audit["schema_version"] == exposed_schema.SCHEMA_VERSION


# ══════════════════════════════════════════════════════════════════════════════
# Benchmark aggregation
# ══════════════════════════════════════════════════════════════════════════════


def contribution(tenant, value, heads=20):
    return benchmarks.Contribution(
        tenant_id=tenant,
        metric="ctc_m3",
        dimension_key="saas|blr|200-500",
        value=D(str(value)),
        employee_count=heads,
    )


TWELVE = [contribution(f"t{i}", 1500000 + i * 100000) for i in range(12)]
CONSENT = {f"t{i}" for i in range(12)}


def cell(rows=None, consent=None, **kw):
    return benchmarks.aggregate(
        rows if rows is not None else TWELVE,
        consenting_tenant_ids=consent if consent is not None else CONSENT,
        metric="ctc_m3",
        dimension_key="saas|blr|200-500",
        **kw,
    )


def test_a_cell_with_enough_contributors_publishes_quartiles():
    result = cell()
    assert result.published is True
    assert result.contributors == 12
    assert result.p25 < result.p50 < result.p75


def test_a_cell_below_the_contributor_floor_is_not_published():
    """"SaaS, 200-500 employees, Bengaluru" with a handful of contributors is a
    few named companies, and anybody in that market can name them."""
    result = cell(rows=TWELVE[:5])
    assert result.published is False
    assert result.p50 is None
    assert "anybody in that market can name them" in result.note


def test_the_floor_counts_companies_not_employees():
    """A cell drawn from one company's four hundred people is that company's
    pay band with a market label on it."""
    assert benchmarks.MIN_CONTRIBUTORS >= 8
    one_big = [contribution("solo", 1800000, heads=400)]
    result = cell(rows=one_big, consent={"solo"})
    assert result.published is False
    assert result.contributors == 1


def test_a_cell_dominated_by_one_company_is_not_published():
    """The contributor count clears the threshold and the number would still be
    substantially that one company's, published to its competitors."""
    rows = [contribution("big", 1800000, heads=500)] + [
        contribution(f"t{i}", 1500000, heads=10) for i in range(10)
    ]
    result = cell(rows=rows, consent=CONSENT | {"big"})
    assert result.published is False
    assert "83%" in result.note or "of the employees in this cell" in result.note


def test_a_non_consenting_tenant_raises_rather_than_being_filtered():
    """A silent filter cannot be told apart from one that was never applied, and
    this is precisely what a client will ask us to demonstrate."""
    with pytest.raises(benchmarks.BenchmarkError) as exc:
        cell(rows=[*TWELVE, contribution("optout", 9999999)])
    assert "refused rather than filtered" in str(exc.value)


def test_an_empty_consent_set_raises():
    with pytest.raises(benchmarks.BenchmarkError):
        cell(consent=set())


def test_one_value_per_company_enters_the_distribution():
    """A large contributor must not weight the distribution by its headcount -
    that is the same identification problem arriving through the arithmetic."""
    # Small headcounts on the extra rows, so this isolates the averaging rule
    # rather than tripping the separate dominance guard.
    doubled = [*TWELVE, *[contribution("t0", 9000000, heads=1) for _ in range(50)]]
    result = cell(rows=doubled)
    assert result.contributors == 12
    assert result.published is True
    # t0's fifty rows average into one company value, so the median barely moves.
    assert result.p50 < D("3000000")


def test_the_source_is_always_carried():
    """Presenting our own client base and a licensed dataset identically hides
    the limitations of both."""
    own = cell()
    licensed = cell(source=benchmarks.BenchmarkSource.LICENSED)
    assert own.as_dict()["source"] == "own_client_base"
    assert licensed.as_dict()["source"] == "licensed"


# ══════════════════════════════════════════════════════════════════════════════
# Position against the benchmark
# ══════════════════════════════════════════════════════════════════════════════


def test_the_position_is_a_band_with_the_sample_size_attached():
    """"The market pays 18 lakh" causes a company at 16 to raise everybody and a
    company at 22 to conclude they are overpaying."""
    position = benchmarks.position_against(own_value=D("1400000"), cell=cell())
    assert position.percentile == 25
    assert "12 contributing companies" in position.reading


def test_a_high_value_lands_in_the_top_band():
    position = benchmarks.position_against(own_value=D("2400000"), cell=cell())
    assert position.percentile == 100
    assert "upper quartile" in position.reading


def test_the_band_is_coarse_on_purpose():
    """An exact percentile from this many companies implies a precision the
    sample does not support, and somebody would quote it."""
    position = benchmarks.position_against(own_value=D("2100000"), cell=cell())
    assert position.percentile in {25, 50, 75, 100}
    assert "precision the sample does not support" in position.reading


def test_an_unpublished_cell_yields_no_position():
    position = benchmarks.position_against(
        own_value=D("1600000"), cell=cell(rows=TWELVE[:4])
    )
    assert position.percentile is None
    assert "nothing to compare against" in position.reading


def test_the_reading_names_which_source_it_came_from():
    own = benchmarks.position_against(own_value=D("1600000"), cell=cell())
    licensed = benchmarks.position_against(
        own_value=D("1600000"),
        cell=cell(source=benchmarks.BenchmarkSource.LICENSED),
    )
    assert "our own client base" in own.reading
    assert "a licensed dataset" in licensed.reading


# ══════════════════════════════════════════════════════════════════════════════
# Access
# ══════════════════════════════════════════════════════════════════════════════


def test_a_non_contributor_sees_licensed_data_only_and_is_told_why():
    """It stops being fair the moment somebody discovers it as a surprise while
    trying to open a chart."""
    statement = benchmarks.access_statement(is_contributor=False)
    assert statement["available_sources"] == ["licensed"]
    assert "stated here rather than discovered" in statement["statement"]
    assert "reversible" in statement["statement"]


def test_a_contributor_sees_both_and_is_told_what_is_protected():
    statement = benchmarks.access_statement(is_contributor=True)
    assert set(statement["available_sources"]) == {"own_client_base", "licensed"}
    assert "never shown to anybody" in statement["statement"]
