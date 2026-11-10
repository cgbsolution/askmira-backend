"""Ask Miraa's data tool: the chart that fits, and only safe totals for the model.

What these hold:

- a trend is a line along the months asked, with a gap (never a zero) for a
  month with nothing; groups compare as bars, shares as a pie, a breakdown over
  time as stacked bars; past the top N the rest are added up as "Others";
- the model reads a group's figures only when the group covers five or more
  people; smaller groups are counted, never shown, and the model is told not to
  work them out; datasets not about people have no such limit;
- the catalogue the model is shown never offers PAN, UAN, bank account or email,
  and hides pay fields from a role that may not see them;
- the compiler groups dates by month, quarter or financial year, matches enum
  filters by name in any case, and always applies an entity's own filters.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.companion import data_tools, views
from app.services.companion.data_tools import MIN_GROUP, catalogue_text
from app.services.reporting import compiler
from app.services.reporting.compiler import (
    CompileError,
    Definition,
    Filter,
    Operator,
    Scope,
    ScopeKind,
    Selection,
)
from app.services.reporting.semantic import ENTITIES

HR = SimpleNamespace(role=None, has_reports=False, employee_id=None)


@pytest.fixture(autouse=True)
def _as_hr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(data_tools.permissions, "is_hr", lambda user: True)
    monkeypatch.setattr(data_tools, "current_scope", lambda: None)


def _present(
    rows,
    *,
    group_by,
    bucket=None,
    measures=(("employer_cost", "sum"),),
    dataset="payslip",
    chart="auto",
    top=8,
    period=None,
    people_at=1,
):
    return data_tools._present(
        ENTITIES[dataset],
        HR,
        rows=rows,
        group_by=list(group_by),
        bucket=bucket,
        measures=list(measures),
        people_at=people_at,
        chart=chart,
        top=top,
        period=period,
        notes=[],
    )


# ── Charts ───────────────────────────────────────────────────────────────────


def test_a_trend_is_a_line_with_gaps_not_zeros() -> None:
    rows = [
        ["2026-04-01", "300000.00", 6],
        ["2026-06-01", "330000.00", 6],
    ]
    chart, table = _present(
        rows,
        group_by=["pay_month"],
        bucket="month",
        period=(date(2026, 4, 1), date(2026, 6, 30)),
    )
    assert chart["chart"] == "line" and chart["unit"] == "inr"
    assert chart["x"] == ["Apr 2026", "May 2026", "Jun 2026"]
    assert chart["series"][0]["values"] == ["300000.00", "", "330000.00"]
    assert table["columns"] == ["Month", "Employer cost", "People"]
    assert table["rows"][0] == ["Apr 2026", "₹3,00,000.00", "6"]


def test_groups_compare_as_bars_and_the_rest_are_others() -> None:
    rows = [[f"Team {i}", str(1000 * (10 - i)), 6] for i in range(10)]
    chart, _ = _present(rows, group_by=["department"], top=3)
    assert chart["chart"] == "horizontal_bar" or chart["chart"] == "bar"
    assert chart["x"] == ["Team 0", "Team 1", "Team 2", "Others (7)"]
    assert chart["series"][0]["values"][-1] == str(sum(1000 * (10 - i) for i in range(3, 10)))


def test_a_pie_shows_shares_and_never_a_negative() -> None:
    rows = [["Sales", "500", 6], ["Engineering", "1500", 9]]
    chart, _ = _present(rows, group_by=["department"], chart="pie")
    assert chart["chart"] == "pie" and chart["x"] == ["Engineering", "Sales"]
    negative = [["Sales", "-500", 6], ["Engineering", "1500", 9]]
    chart, _ = _present(negative, group_by=["department"], chart="pie")
    assert chart["chart"] == "bar"


def test_a_breakdown_over_time_is_stacked_or_a_line_per_group() -> None:
    rows = [
        ["2026-04-01", "Sales", "100", 6],
        ["2026-04-01", "Engineering", "300", 7],
        ["2026-05-01", "Sales", "120", 6],
    ]
    chart, _ = _present(
        rows,
        group_by=["pay_month", "department"],
        bucket="month",
        chart="stacked_bar",
        people_at=1,
        measures=(("employer_cost", "sum"),),
    )
    assert chart["chart"] == "stacked_bar"
    assert chart["x"] == ["Apr 2026", "May 2026"]
    assert {s["name"]: s["values"] for s in chart["series"]} == {
        "Engineering": ["300", ""],
        "Sales": ["100", "120"],
    }


def test_one_figure_is_a_number() -> None:
    [card] = _present([["1250000.00", 40]], group_by=[])
    assert card["type"] == "record"
    assert {f["label"]: f["value"] for f in card["figures"]} == {
        "Employer cost": "₹12,50,000.00",
        "People": "40",
    }


def test_enum_values_read_as_words() -> None:
    chart, _ = _present([["pending_review", "10", 6]], group_by=["run_status"])
    assert chart["x"] == ["Pending review"]


# ── What the model reads ─────────────────────────────────────────────────────


def test_small_groups_are_never_read_by_the_model() -> None:
    rows = [
        ["Engineering", "900000.00", 12],
        ["Founders", "800000.00", 2],
        ["Legal", "120000.00", 1],
    ]
    cards = _present(rows, group_by=["department"])
    seen = views.view_of(cards, allowed=True)
    assert "Engineering | 900000.00 | 12" in seen
    assert "800000" not in seen and "Founders" not in seen
    assert "120000" not in seen and "Legal" not in seen
    assert f"2 group(s) cover fewer than {MIN_GROUP} people" in seen
    # The person still sees every group.
    assert [row[0] for row in cards[-1]["rows"]] == ["Engineering", "Founders", "Legal"]


def test_a_single_persons_total_is_not_read() -> None:
    cards = _present([["61000.00", 1]], group_by=[])
    seen = views.view_of(cards, allowed=True)
    assert "61000" not in seen and "too small" in seen


def test_data_not_about_people_has_no_limit() -> None:
    rows = [["Laptops", "3"], ["Phones", "1"]]
    cards = _present(
        rows,
        group_by=["asset_category"],
        dataset="asset",
        measures=(("asset_count", "count"),),
        people_at=None,
    )
    seen = views.view_of(cards, allowed=True)
    assert "Phones | 1" in seen and "fewer than" not in seen


def test_a_trend_says_how_it_moved_from_the_readable_months() -> None:
    rows = [["2026-04-01", "100.00", 6], ["2026-05-01", "150.00", 6], ["2026-06-01", "125.00", 6]]
    cards = _present(rows, group_by=["pay_month"], bucket="month")
    seen = views.view_of(cards, allowed=True)
    assert "changed by +25.0%" in seen and "Highest: May 2026" in seen


def test_reading_switched_off_hides_even_safe_totals() -> None:
    cards = _present([["Engineering", "900000.00", 12]], group_by=["department"])
    assert "900000" not in views.view_of(cards, allowed=False)


# ── The catalogue the model is shown ─────────────────────────────────────────


def test_identifiers_are_never_offered_to_the_model() -> None:
    for role in ("admin", "sub_admin", "employee"):
        text = catalogue_text(role)
        for hidden in ("pan", "uan", "bank_account_number", "work_email", "candidate_email"):
            assert f" {hidden}," not in text and f" {hidden}." not in text, (role, hidden)


def test_pay_fields_are_offered_only_to_roles_that_may_see_them() -> None:
    # Offered as a measure (with its unit) only where the role may see pay.
    assert "employer_cost(inr)" in catalogue_text("admin")
    assert "employer_cost(inr)" not in catalogue_text("employee")
    assert "employer_cost(inr)" not in catalogue_text("sub_admin")


# ── The compiler ─────────────────────────────────────────────────────────────

SCOPE = Scope(kind=ScopeKind.TENANT, tenant_id=uuid.uuid4())


def _sql(definition: Definition, role: str = "admin") -> str:
    compiled = compiler.compile_report(definition, role=role, scope=SCOPE)
    return compiler.rendered_sql(compiled.statement)


def _trend(bucket: str, field: str = "pay_month", entity: str = "payslip") -> Definition:
    return Definition(
        entity_key=entity,
        selections=(Selection(field), Selection("headcount", "count_distinct")),
        group_by=(field,),
        buckets=((field, bucket),),
    )


def test_dates_group_by_month_quarter_or_financial_year() -> None:
    assert "date_trunc" in _sql(_trend("month"))
    assert "make_interval" in _sql(_trend("financial_year"))
    # A timestamp is grouped by the day it was in India.
    assert "timezone" in _sql(_trend("month", "raised_on", "helpdesk_ticket"))


def test_a_bucket_must_be_known_grouped_and_on_a_date() -> None:
    with pytest.raises(CompileError, match="not a way to group dates"):
        _sql(_trend("fortnight"))
    with pytest.raises(CompileError, match="not in group_by"):
        _sql(
            Definition(
                entity_key="payslip",
                selections=(Selection("headcount", "count_distinct"),),
                buckets=(("pay_month", "month"),),
            )
        )
    with pytest.raises(CompileError, match="not a date"):
        _sql(_trend("month", "department"))


def test_an_entitys_own_filters_always_apply() -> None:
    sql = _sql(
        Definition(entity_key="payslip", selections=(Selection("headcount", "count_distinct"),))
    )
    assert "payroll_runs.status !=" in sql
    tickets = _sql(
        Definition(entity_key="helpdesk_ticket", selections=(Selection("ticket_count", "count"),))
    )
    assert "is_confidential = false" in tickets


def test_enum_filters_match_by_name_in_any_case() -> None:
    for value in ("approved", "Approved", "APPROVED"):
        definition = Definition(
            entity_key="leave_request",
            selections=(Selection("leave_count", "count"),),
            filters=(Filter("leave_status", Operator.EQUALS, value),),
        )
        compiler.compile_report(definition, role="admin", scope=SCOPE)
    with pytest.raises(CompileError, match="Choose from"):
        compiler.compile_report(
            Definition(
                entity_key="leave_request",
                selections=(Selection("leave_count", "count"),),
                filters=(Filter("leave_status", Operator.EQUALS, "maybe"),),
            ),
            role="admin",
            scope=SCOPE,
        )


def test_pay_is_still_refused_to_roles_that_may_not_see_it() -> None:
    definition = Definition(
        entity_key="payslip",
        selections=(Selection("department"), Selection("employer_cost", "sum")),
        group_by=("department",),
    )
    compiled = compiler.compile_report(definition, role="sub_admin", scope=SCOPE)
    assert [c["key"] for c in compiled.columns] == ["department"]
    assert compiled.denied_fields == ("employer_cost",)


def test_every_dataset_has_a_trend_date_and_counts_its_people() -> None:
    for key, ent in ENTITIES.items():
        assert ent.default_date, key
        if ent.personal:
            assert ent.people_field, key
        compiler.compile_report(
            Definition(
                entity_key=key,
                selections=(Selection(ent.default_date),),
                group_by=(ent.default_date,),
                buckets=((ent.default_date, "quarter"),),
            ),
            role="admin",
            scope=SCOPE,
        )


def test_amounts_read_back_as_decimals() -> None:
    assert data_tools._number("12.50") == Decimal("12.50")
    assert data_tools._number("") is None


def test_a_headcount_by_department_is_the_companys_shape_not_a_record() -> None:
    org = data_tools._organisation_counts
    employee = ENTITIES["employee"]
    status = Filter("status", Operator.NOT_IN, None, ("exited",))
    assert org(employee, [("headcount", "count_distinct")], ["department"], [status])
    # By a name or a gender, or with pay, the five-person floor holds.
    assert not org(employee, [("headcount", "count_distinct")], ["employee_name"], [])
    assert not org(employee, [("headcount", "count_distinct")], ["gender"], [])
    assert not org(employee, [("ctc_annual", "sum")], ["department"], [])
    assert not org(ENTITIES["leave_request"], [("leave_count", "count")], ["department"], [])
    rows = [["Engineering", 8], ["Finance", 2]]
    cards = data_tools._present(
        employee,
        HR,
        rows=rows,
        group_by=["department"],
        bucket=None,
        measures=[("headcount", "count_distinct")],
        people_at=0,
        chart="bar",
        top=8,
        period=None,
        notes=[],
        floor=0,
    )
    seen = views.view_of(cards, allowed=True)
    assert "Finance | 2" in seen and "fewer than" not in seen
