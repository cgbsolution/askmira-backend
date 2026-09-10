"""The properties a report builder has to hold or it is a SQL console.

Every test here reads the *generated SQL*, not a result set. That is deliberate:
the claim is about what the query is, and a query that returns nothing today
because the table happens to be empty proves nothing about tomorrow.
"""

from __future__ import annotations

import re
import uuid
from datetime import date

import pytest

from app.services.reporting.compiler import (
    MAX_PAGE_SIZE,
    CompileError,
    Definition,
    Filter,
    Operator,
    Scope,
    ScopeKind,
    Selection,
    compile_report,
    rendered_sql,
    resolve_scope,
)
from app.services.reporting.library import STARTERS
from app.services.reporting.semantic import ENTITIES, Sensitivity

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT = uuid.UUID("22222222-2222-2222-2222-222222222222")
ME = uuid.UUID("33333333-3333-3333-3333-333333333333")
MY_REPORT = uuid.UUID("44444444-4444-4444-4444-444444444444")
TODAY = date(2026, 9, 7)


def _hr() -> Scope:
    return Scope(kind=ScopeKind.TENANT, tenant_id=TENANT)


def _manager() -> Scope:
    return Scope(
        kind=ScopeKind.REPORTING_TREE,
        tenant_id=TENANT,
        employee_id=ME,
        visible_employee_ids=(ME, MY_REPORT),
    )


def _employee() -> Scope:
    return Scope(kind=ScopeKind.SELF, tenant_id=TENANT, employee_id=ME)


def _headcount() -> Definition:
    return Definition(
        entity_key="employee",
        selections=(Selection("department"), Selection("headcount", "count")),
        group_by=("department",),
    )


# ═══════════════════════════════════════════════════════════════════════════
# The tenant predicate
# ═══════════════════════════════════════════════════════════════════════════


def test_every_starter_report_carries_the_tenant_predicate() -> None:
    """The checklist gate: verified by inspection, on all 25.

    A report is a saved object anybody in the tenant might run, and one that
    could reach another tenant's rows is the worst thing this system could do.
    """
    for starter in STARTERS:
        compiled = compile_report(
            starter.definition, role="tenant_admin", scope=_hr(), today=TODAY
        )
        for statement in (compiled.statement, compiled.count_statement):
            sql = rendered_sql(statement)
            assert "tenant_id = " in sql, (
                f"{starter.key} produced a query with no tenant predicate:\n{sql}"
            )


def test_every_entity_can_be_queried_and_every_one_is_tenant_scoped() -> None:
    for key, ent in ENTITIES.items():
        first = ent.fields[0]
        compiled = compile_report(
            Definition(entity_key=key, selections=(Selection(first.key),)),
            role="tenant_admin",
            scope=_hr(),
            today=TODAY,
        )
        sql = rendered_sql(compiled.statement)
        assert f"{ent.base_table}.tenant_id = " in sql, f"{key}:\n{sql}"


def test_the_tenant_predicate_uses_the_requesters_tenant_not_the_definitions() -> None:
    """There is no field on a definition that could carry a tenant id. This test
    exists to keep it that way: the value is bound from the scope."""
    compiled = compile_report(
        _headcount(), role="tenant_admin",
        scope=Scope(kind=ScopeKind.TENANT, tenant_id=OTHER_TENANT), today=TODAY,
    )
    params = compiled.statement.compile().params
    assert OTHER_TENANT in params.values()
    assert TENANT not in params.values()


def test_joined_tables_are_tenant_scoped_in_the_join_predicate() -> None:
    """Not because ids collide, but because "ids never collide" is not a security
    guarantee — it is an assumption about a UUID generator."""
    compiled = compile_report(_headcount(), role="tenant_admin", scope=_hr(), today=TODAY)
    sql = rendered_sql(compiled.statement)
    assert "departments.tenant_id = employees.tenant_id" in sql


# ═══════════════════════════════════════════════════════════════════════════
# Field-level sensitivity — the checklist gate
# ═══════════════════════════════════════════════════════════════════════════


def test_a_manager_cannot_reach_salary_through_the_employee_entity() -> None:
    """The checklist gate: provably, through any join path.

    `ctc_annual` on the employee entity is reached by a declared join to
    `employee_salaries`. A manager asking for it gets a report without it, and
    the join is not built either — so the column never leaves the database.
    """
    definition = Definition(
        entity_key="employee",
        selections=(Selection("employee_name"), Selection("ctc_annual", "sum")),
    )
    compiled = compile_report(definition, role="manager", scope=_manager(), today=TODAY)

    assert compiled.denied_fields == ("ctc_annual",)
    sql = rendered_sql(compiled.statement)
    assert "ctc_annual" not in sql
    assert "employee_salaries" not in sql, "the join was not even built"


def test_a_manager_cannot_reach_salary_through_the_payslip_entity_either() -> None:
    """The same field is restricted whichever report reaches it. This is why the
    employee dimensions are defined once and reused."""
    definition = Definition(
        entity_key="payslip",
        selections=(Selection("employee_name"), Selection("net_pay", "sum")),
    )
    compiled = compile_report(definition, role="manager", scope=_manager(), today=TODAY)
    assert "net_pay" in compiled.denied_fields
    assert "net_pay" not in rendered_sql(compiled.statement)


@pytest.mark.parametrize(
    "entity_key,field_key",
    [
        ("employee", "ctc_annual"),
        ("employee", "pan"),
        ("employee", "bank_account_number"),
        ("payslip", "net_pay"),
        ("payslip", "gross_earnings"),
        ("payslip", "tds"),
        ("candidate", "expected_ctc"),
        ("offer", "offer_ctc"),
    ],
)
def test_no_payroll_field_is_reachable_by_a_manager_from_any_entity(
    entity_key, field_key
) -> None:
    """The gate stated exhaustively: every PAYROLL_ONLY field, from every entity
    that exposes it."""
    definition = Definition(
        entity_key=entity_key, selections=(Selection(field_key),)
    )
    with pytest.raises(CompileError, match="visible to your role"):
        compile_report(definition, role="manager", scope=_manager(), today=TODAY)


def test_every_restricted_field_in_the_catalogue_is_hidden_from_a_manager() -> None:
    """Swept rather than sampled. A field added later with the wrong sensitivity
    is caught here even if nobody writes a test for it."""
    for key, ent in ENTITIES.items():
        for spec in ent.fields:
            if spec.sensitivity is Sensitivity.PUBLIC:
                continue
            definition = Definition(entity_key=key, selections=(Selection(spec.key),))
            with pytest.raises(CompileError):
                compile_report(definition, role="manager", scope=_manager(), today=TODAY)


def test_filtering_on_a_restricted_field_is_refused_rather_than_dropped() -> None:
    """Dropping it would silently change what the report means: "employees
    earning over 20 lakh" would quietly become "all employees"."""
    definition = Definition(
        entity_key="employee",
        selections=(Selection("employee_name"),),
        filters=(Filter("ctc_annual", Operator.GREATER_THAN, 2_000_000),),
    )
    with pytest.raises(CompileError) as exc:
        compile_report(definition, role="manager", scope=_manager(), today=TODAY)
    assert "row count" in str(exc.value)


def test_sorting_by_a_restricted_field_is_refused() -> None:
    """`ORDER BY ctc_annual` leaks the ordering of salaries without returning
    one of them."""
    definition = Definition(
        entity_key="employee",
        selections=(Selection("employee_name"),),
        sort_by=(("ctc_annual", "desc"),),
    )
    with pytest.raises(CompileError):
        compile_report(definition, role="manager", scope=_manager(), today=TODAY)


def test_grouping_by_a_restricted_field_is_refused() -> None:
    """Grouping by salary and counting gives you the salary distribution."""
    definition = Definition(
        entity_key="employee",
        selections=(Selection("headcount", "count"),),
        group_by=("ctc_annual",),
    )
    with pytest.raises(CompileError):
        compile_report(definition, role="manager", scope=_manager(), today=TODAY)


def test_hr_ops_sees_hr_fields_but_not_payroll_fields() -> None:
    """The middle tier is where a two-level model would have been wrong."""
    hr_only = Definition(
        entity_key="employee",
        selections=(Selection("employee_name"), Selection("date_of_exit")),
    )
    compiled = compile_report(hr_only, role="hr_ops", scope=_hr(), today=TODAY)
    assert compiled.denied_fields == ()

    payroll = Definition(entity_key="employee", selections=(Selection("pan"),))
    with pytest.raises(CompileError):
        compile_report(payroll, role="hr_ops", scope=_hr(), today=TODAY)


def test_an_unknown_role_gets_the_least_access_not_the_most() -> None:
    """A role added elsewhere in the system should narrow what a report reaches,
    never widen it by falling through a lookup."""
    definition = Definition(
        entity_key="employee",
        selections=(Selection("employee_name"), Selection("date_of_exit")),
    )
    compiled = compile_report(
        definition, role="some_new_role_nobody_told_us_about",
        scope=_employee(), today=TODAY,
    )
    assert "date_of_exit" in compiled.denied_fields
    assert resolve_scope("some_new_role_nobody_told_us_about") is ScopeKind.SELF


# ═══════════════════════════════════════════════════════════════════════════
# Row-level scope
# ═══════════════════════════════════════════════════════════════════════════


def test_a_manager_is_limited_to_their_reporting_tree() -> None:
    """The checklist gate."""
    compiled = compile_report(
        Definition(entity_key="employee", selections=(Selection("employee_name"),)),
        role="manager", scope=_manager(), today=TODAY,
    )
    sql = rendered_sql(compiled.statement)
    assert "employees.id IN" in sql
    # An IN binds the whole list under one parameter name, rendered as a
    # POSTCOMPILE placeholder — the ids are in the parameter, not in the SQL,
    # which is the point.
    bound = compiled.statement.compile().params["id_1"]
    assert set(bound) == {ME, MY_REPORT}
    assert str(ME) not in sql


def test_an_employee_sees_only_themselves() -> None:
    compiled = compile_report(
        Definition(entity_key="employee", selections=(Selection("employee_name"),)),
        role="employee", scope=_employee(), today=TODAY,
    )
    sql = rendered_sql(compiled.statement)
    assert "employees.id = " in sql
    assert ME in compiled.statement.compile().params.values()


def test_hr_gets_no_row_restriction_but_still_gets_the_tenant_one() -> None:
    compiled = compile_report(
        Definition(entity_key="employee", selections=(Selection("employee_name"),)),
        role="hr_admin", scope=_hr(), today=TODAY,
    )
    sql = rendered_sql(compiled.statement)
    assert "employees.id IN" not in sql
    assert "employees.tenant_id = " in sql


def test_a_manager_with_an_empty_tree_sees_nothing_rather_than_everything() -> None:
    """The failure mode: an unfiltered query returns the tenant."""
    compiled = compile_report(
        Definition(entity_key="employee", selections=(Selection("employee_name"),)),
        role="manager",
        scope=Scope(kind=ScopeKind.REPORTING_TREE, tenant_id=TENANT),
        today=TODAY,
    )
    assert "false" in rendered_sql(compiled.statement).lower()


def test_an_employee_with_no_employee_record_sees_nothing() -> None:
    compiled = compile_report(
        Definition(entity_key="employee", selections=(Selection("employee_name"),)),
        role="employee",
        scope=Scope(kind=ScopeKind.SELF, tenant_id=TENANT),
        today=TODAY,
    )
    assert "false" in rendered_sql(compiled.statement).lower()


def test_an_entity_with_no_employee_column_refuses_scoped_roles() -> None:
    """A candidate is not an employee, so "my team" has nothing to attach to.
    Refused rather than returning everything, which is what an unfiltered query
    would do."""
    definition = Definition(
        entity_key="candidate", selections=(Selection("candidate_stage"),)
    )
    with pytest.raises(CompileError) as exc:
        compile_report(definition, role="hr_ops", scope=_manager(), today=TODAY)
    assert "not the safe fallback" in str(exc.value)


# ═══════════════════════════════════════════════════════════════════════════
# Injection — the checklist gate
# ═══════════════════════════════════════════════════════════════════════════

ADVERSARIAL = [
    "'; DROP TABLE employees; --",
    "' OR '1'='1",
    "1; DELETE FROM payslips WHERE 1=1; --",
    "\\'; DROP TABLE employees; --",
    "' UNION SELECT ctc_annual FROM employee_salaries --",
    "%' OR tenant_id IS NOT NULL --",
    "'||(SELECT bank_account_number FROM employees LIMIT 1)||'",
    "\x00'; DROP TABLE employees; --",
    "') OR 1=1 --",
    "admin'--",
]


@pytest.mark.parametrize("payload", ADVERSARIAL)
def test_an_adversarial_filter_value_becomes_a_bound_parameter(payload) -> None:
    """The checklist gate, tested explicitly.

    Every one of these becomes a string compared against a column. Nothing about
    it is parsed as SQL, because it never reaches the SQL — the statement carries
    a placeholder and the value travels beside it.
    """
    definition = Definition(
        entity_key="employee",
        selections=(Selection("employee_name"),),
        filters=(Filter("grade", Operator.EQUALS, payload),),
    )
    compiled = compile_report(definition, role="hr_admin", scope=_hr(), today=TODAY)
    sql = rendered_sql(compiled.statement)

    # Word boundaries, because "deleted_at IS NULL" legitimately contains
    # "DELETE" and a substring check would fail on the compiler's own soft-delete
    # predicate while proving nothing about injection.
    for keyword in ("DROP", "DELETE", "UNION", "INSERT", "UPDATE", "TRUNCATE"):
        assert not re.search(rf"\b{keyword}\b", sql.upper()), (
            f"'{keyword}' appears as a statement in: {sql}"
        )
    # The real property: the payload is nowhere in the SQL, and there is exactly
    # one statement — no separator smuggled a second one in.
    assert payload not in sql
    assert ";" not in sql
    # It is present as a parameter, which is exactly where it should be.
    assert payload in compiled.statement.compile().params.values()


@pytest.mark.parametrize("payload", ADVERSARIAL)
def test_an_adversarial_field_key_is_refused_by_the_catalogue(payload) -> None:
    """Identifiers cannot come from user input at all. The only way to name a
    column is to match a key that already exists."""
    definition = Definition(entity_key="employee", selections=(Selection(payload),))
    with pytest.raises(CompileError, match="not a field"):
        compile_report(definition, role="hr_admin", scope=_hr(), today=TODAY)


@pytest.mark.parametrize("payload", ADVERSARIAL)
def test_an_adversarial_entity_key_is_refused(payload) -> None:
    definition = Definition(entity_key=payload, selections=(Selection("x"),))
    with pytest.raises(CompileError, match="not a reportable entity"):
        compile_report(definition, role="hr_admin", scope=_hr(), today=TODAY)


@pytest.mark.parametrize("payload", ADVERSARIAL)
def test_an_adversarial_group_by_is_refused(payload) -> None:
    definition = Definition(
        entity_key="employee",
        selections=(Selection("headcount", "count"),),
        group_by=(payload,),
    )
    with pytest.raises(CompileError):
        compile_report(definition, role="hr_admin", scope=_hr(), today=TODAY)


@pytest.mark.parametrize("payload", ADVERSARIAL)
def test_an_adversarial_sort_key_is_refused(payload) -> None:
    definition = Definition(
        entity_key="employee",
        selections=(Selection("employee_name"),),
        sort_by=((payload, "asc"),),
    )
    with pytest.raises(CompileError):
        compile_report(definition, role="hr_admin", scope=_hr(), today=TODAY)


def test_an_adversarial_sort_direction_cannot_inject() -> None:
    """The direction is compared against one literal string; anything else is
    ascending. There is no branch that puts it into the SQL."""
    definition = Definition(
        entity_key="employee",
        selections=(Selection("employee_name"),),
        sort_by=(("employee_name", "asc; DROP TABLE employees; --"),),
    )
    compiled = compile_report(definition, role="hr_admin", scope=_hr(), today=TODAY)
    sql = rendered_sql(compiled.statement)
    assert not re.search(r"\bDROP\b", sql.upper())
    assert ";" not in sql
    assert sql.rstrip().count("ORDER BY") == 1


@pytest.mark.parametrize("payload", ADVERSARIAL)
def test_an_adversarial_aggregation_is_refused(payload) -> None:
    definition = Definition(
        entity_key="employee", selections=(Selection("headcount", payload),)
    )
    with pytest.raises(CompileError, match="not available"):
        compile_report(definition, role="hr_admin", scope=_hr(), today=TODAY)


def test_a_wildcard_in_a_contains_filter_is_escaped() -> None:
    """A value of "100%" should look for the literal string, not for "100
    followed by anything"."""
    definition = Definition(
        entity_key="employee",
        selections=(Selection("employee_name"),),
        filters=(Filter("designation", Operator.CONTAINS, "100%"),),
    )
    compiled = compile_report(definition, role="hr_admin", scope=_hr(), today=TODAY)
    assert "ESCAPE" in rendered_sql(compiled.statement).upper()


# ═══════════════════════════════════════════════════════════════════════════
# Limits and shape
# ═══════════════════════════════════════════════════════════════════════════


def test_the_page_size_is_capped_whatever_the_definition_asks_for() -> None:
    definition = Definition(
        entity_key="employee",
        selections=(Selection("employee_name"),),
        limit=10_000_000,
    )
    compiled = compile_report(definition, role="hr_admin", scope=_hr(), today=TODAY)
    params = compiled.statement.compile().params
    assert MAX_PAGE_SIZE in params.values()


def test_a_negative_offset_becomes_zero_rather_than_an_error() -> None:
    definition = Definition(
        entity_key="employee", selections=(Selection("employee_name"),), offset=-500
    )
    compiled = compile_report(definition, role="hr_admin", scope=_hr(), today=TODAY)
    assert 0 in compiled.statement.compile().params.values()


def test_soft_deleted_rows_are_excluded() -> None:
    """A headcount that counts deleted employees is a wrong number nobody would
    think to query."""
    compiled = compile_report(_headcount(), role="hr_admin", scope=_hr(), today=TODAY)
    assert "deleted_at IS NULL" in rendered_sql(compiled.statement)


def test_a_column_that_is_neither_grouped_nor_aggregated_is_refused() -> None:
    """Otherwise the database picks an arbitrary row's value and nobody can tell
    which."""
    definition = Definition(
        entity_key="employee",
        selections=(Selection("department"), Selection("employee_name")),
        group_by=("department",),
    )
    with pytest.raises(CompileError, match="neither grouped nor aggregated"):
        compile_report(definition, role="hr_admin", scope=_hr(), today=TODAY)


def test_a_report_with_no_columns_is_refused() -> None:
    with pytest.raises(CompileError, match="at least one column"):
        compile_report(
            Definition(entity_key="employee", selections=()),
            role="hr_admin", scope=_hr(), today=TODAY,
        )


def test_an_empty_in_filter_is_refused_rather_than_guessed_at() -> None:
    """It would match everything or nothing depending on the database."""
    definition = Definition(
        entity_key="employee",
        selections=(Selection("employee_name"),),
        filters=(Filter("grade", Operator.IN, None, ()),),
    )
    with pytest.raises(CompileError, match="no values"):
        compile_report(definition, role="hr_admin", scope=_hr(), today=TODAY)


def test_an_operator_that_makes_no_sense_for_the_type_is_refused() -> None:
    definition = Definition(
        entity_key="employee",
        selections=(Selection("employee_name"),),
        filters=(Filter("date_of_joining", Operator.CONTAINS, "April"),),
    )
    with pytest.raises(CompileError, match="cannot be used on"):
        compile_report(definition, role="hr_admin", scope=_hr(), today=TODAY)


def test_aggregating_a_dimension_is_refused() -> None:
    definition = Definition(
        entity_key="employee", selections=(Selection("grade", "sum"),)
    )
    with pytest.raises(CompileError, match="not available"):
        compile_report(definition, role="hr_admin", scope=_hr(), today=TODAY)


def test_only_the_joins_a_report_needs_are_built() -> None:
    """A query that joined everything the entity could reach would give a manager
    a route to salary through a report about attendance."""
    plain = compile_report(
        Definition(entity_key="employee", selections=(Selection("employee_name"),)),
        role="hr_admin", scope=_hr(), today=TODAY,
    )
    sql = rendered_sql(plain.statement)
    assert "departments" not in sql
    assert "locations" not in sql
    assert "employee_salaries" not in sql


def test_joins_are_left_so_a_row_without_a_department_still_counts() -> None:
    compiled = compile_report(_headcount(), role="hr_admin", scope=_hr(), today=TODAY)
    assert "LEFT OUTER JOIN" in rendered_sql(compiled.statement)


def test_the_manager_join_is_aliased_so_it_does_not_collide() -> None:
    """Two joins to `employees` in one query need different names."""
    definition = Definition(
        entity_key="employee",
        selections=(Selection("employee_name"), Selection("manager_name")),
    )
    sql = rendered_sql(
        compile_report(definition, role="hr_admin", scope=_hr(), today=TODAY).statement
    )
    assert "AS manager" in sql or "manager." in sql
