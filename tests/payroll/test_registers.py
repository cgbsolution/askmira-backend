"""Slice 09 gate: statutory registers match their prescribed formats.

An inspector reads column headings. A register carrying the right numbers under
headings a developer found clearer is a defective register, so these tests
assert the headings and their order — not just the arithmetic.
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal

import pytest

from app.services.payroll.registers import (
    BONUS_FORM_C,
    ESI_CONTRIBUTION_REGISTER,
    LWF_REGISTER,
    PF_CONTRIBUTION_REGISTER,
    PROFESSIONAL_TAX_REGISTER,
    REGISTERS,
    RegisterError,
    render_register,
    to_csv,
)


def D(value: str | int) -> Decimal:  # noqa: N802 - a Decimal alias, used throughout
    return Decimal(str(value))


# ═══════════════════════════════════════════════════════════════════════════
# The prescribed formats
# ═══════════════════════════════════════════════════════════════════════════


def test_form_c_carries_the_prescribed_columns_in_the_prescribed_order() -> None:
    """Payment of Bonus Rules Form C.

    The order is the rule's order. A register with the deduction columns before
    the bonus payable column is not Form C.
    """
    headings = [column.heading for column in BONUS_FORM_C.columns]
    assert headings[0] == "S. No."
    assert headings[1] == "Name of the employee"
    assert "Number of days worked in the year" in headings
    assert "Amount of bonus payable" in headings

    # The deductions block sits between the bonus payable and the net.
    payable = headings.index("Amount of bonus payable")
    net = headings.index("Net amount payable")
    deductions = [i for i, h in enumerate(headings) if h.startswith("Deductions - ")]
    assert deductions, "Form C has a deductions block"
    assert all(payable < i < net for i in deductions)

    # The signature column is last, because it is signed after everything else
    # on the row has been read.
    assert headings[-1] == "Signature or thumb impression of the employee"


def test_every_register_names_the_rule_that_prescribes_it() -> None:
    """A register that cannot cite its own format cannot be checked."""
    for spec in REGISTERS.values():
        assert spec.prescribed_by, spec.code
        assert spec.columns, spec.code


def test_every_register_starts_with_a_serial_number() -> None:
    for spec in REGISTERS.values():
        assert spec.columns[0].heading == "S. No.", spec.code


def test_every_totalled_column_exists_in_the_spec() -> None:
    """A totals row over a column the register does not have is a bug."""
    for spec in REGISTERS.values():
        sources = {column.source for column in spec.columns}
        assert set(spec.total_columns) <= sources, spec.code


def test_the_pf_register_separates_voluntary_from_statutory_contributions() -> None:
    """VPF is a distinct column. Folded into the worker's contribution it
    would misstate what was statutorily due."""
    headings = [column.heading for column in PF_CONTRIBUTION_REGISTER.columns]
    assert "Worker contribution" in headings
    assert "Voluntary contribution" in headings


def test_the_esi_register_records_the_contribution_period() -> None:
    """Because an employee can appear in it while above the ceiling."""
    sources = {column.source for column in ESI_CONTRIBUTION_REGISTER.columns}
    assert "contribution_period" in sources


def test_the_pt_register_records_the_state() -> None:
    sources = {column.source for column in PROFESSIONAL_TAX_REGISTER.columns}
    assert "state" in sources


# ═══════════════════════════════════════════════════════════════════════════
# Rendering
# ═══════════════════════════════════════════════════════════════════════════


LWF_ROWS = [
    {
        "employee_name": "Asha Menon",
        "designation": "Analyst",
        "wages": D(42_000),
        "employee_contribution": D(20),
        "employer_contribution": D(40),
        "total_contribution": D(60),
        "period": "12/2025",
    },
    {
        "employee_name": "Rahul Iyer",
        "designation": "Engineer",
        "wages": D(58_000),
        "employee_contribution": D(20),
        "employer_contribution": D(40),
        "total_contribution": D(60),
        "period": "12/2025",
    },
]


def test_every_prescribed_column_is_rendered_even_when_empty() -> None:
    """A missing prescribed column makes the register defective; a blank cell
    does not."""
    rendered = render_register(BONUS_FORM_C, [{"employee_name": "Asha Menon"}])
    assert len(rendered.headings) == len(BONUS_FORM_C.columns)
    assert len(rendered.rows[0]) == len(BONUS_FORM_C.columns)
    assert rendered.rows[0][-1] == "", "the signature cell is blank until signed"


def test_serial_numbers_are_generated_when_the_data_has_none() -> None:
    rendered = render_register(LWF_REGISTER, LWF_ROWS)
    assert [row[0] for row in rendered.rows] == ["1", "2"]


def test_totals_are_footed_over_the_prescribed_columns() -> None:
    rendered = render_register(LWF_REGISTER, LWF_ROWS)
    assert rendered.totals["employee_contribution"] == D(40)
    assert rendered.totals["employer_contribution"] == D(80)
    assert rendered.totals["total_contribution"] == D(120)


def test_a_float_in_a_totalled_column_is_refused() -> None:
    """A float total and a hand-footed total can disagree in the last paisa."""
    rows = [{**LWF_ROWS[0], "employee_contribution": 20.0}]
    with pytest.raises(RegisterError, match="carries a float"):
        render_register(LWF_REGISTER, rows)


def test_filtered_rows_are_reported_rather_than_silently_dropped() -> None:
    rendered = render_register(
        LWF_REGISTER, LWF_ROWS, row_filter=lambda row: row["employee_name"] != "Rahul Iyer"
    )
    assert rendered.row_count == 1
    assert rendered.excluded == ["Rahul Iyer"]


def test_booleans_render_as_yes_and_no() -> None:
    """A register is read by a person, and "True" is not an answer to
    "whether he has completed 15 years of age"."""
    rendered = render_register(
        BONUS_FORM_C, [{"employee_name": "Asha Menon", "completed_fifteen": True}]
    )
    index = [c.source for c in BONUS_FORM_C.columns].index("completed_fifteen")
    assert rendered.rows[0][index] == "Yes"


def test_an_unknown_value_stays_blank_rather_than_asserting_a_fact() -> None:
    rendered = render_register(
        BONUS_FORM_C, [{"employee_name": "Asha Menon", "completed_fifteen": None}]
    )
    index = [c.source for c in BONUS_FORM_C.columns].index("completed_fifteen")
    assert rendered.rows[0][index] == ""


# ═══════════════════════════════════════════════════════════════════════════
# CSV
# ═══════════════════════════════════════════════════════════════════════════


def test_the_csv_carries_the_heading_block_the_rule_and_the_totals() -> None:
    rendered = render_register(
        LWF_REGISTER, LWF_ROWS, heading_lines=["Period: 12/2025", "Establishment: Bengaluru"]
    )
    output = to_csv(rendered)
    rows = list(csv.reader(io.StringIO(output)))

    assert rows[0] == [LWF_REGISTER.title]
    assert rows[1][0].startswith("Prescribed by:")
    assert rows[2] == ["Period: 12/2025"]
    assert rows[3] == ["Establishment: Bengaluru"]

    headings = [column.heading for column in LWF_REGISTER.columns]
    assert headings in rows

    totals_row = rows[rows.index(headings) + 3]
    assert totals_row[0] == "Total"
    assert "120" in totals_row


def test_the_csv_column_order_is_the_spec_order() -> None:
    rendered = render_register(BONUS_FORM_C, [{"employee_name": "Asha Menon"}])
    rows = list(csv.reader(io.StringIO(to_csv(rendered))))
    headings = [column.heading for column in BONUS_FORM_C.columns]
    assert headings in rows


def test_notes_travel_with_the_register() -> None:
    rendered = render_register(ESI_CONTRIBUTION_REGISTER, [])
    output = to_csv(rendered)
    assert "crossed the ceiling mid-period" in output


def test_an_empty_register_still_renders_its_headings() -> None:
    """An establishment with no LWF liability this month files a nil register,
    not nothing."""
    rendered = render_register(LWF_REGISTER, [])
    assert rendered.row_count == 0
    assert len(rendered.headings) == len(LWF_REGISTER.columns)
    assert to_csv(rendered)


def test_the_rendered_form_is_json_serialisable() -> None:
    import json

    rendered = render_register(LWF_REGISTER, LWF_ROWS, heading_lines=["Period: 12/2025"])
    payload = json.loads(json.dumps(rendered.as_dict()))
    assert payload["code"] == "LWF_REGISTER"
    assert payload["totals"]["total_contribution"] == "120"
