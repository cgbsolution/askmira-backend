"""Slice 03 gate: the merge engine and Indian numbering."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.services.documents.indian_numbers import amount_in_words, group_indian, number_in_words
from app.services.documents.letter_merge import (
    LetterMergeError,
    MergeContext,
    extract_tokens,
    render_template,
)

AS_OF = date(2026, 9, 6)


def context(**overrides) -> MergeContext:
    base = {
        "as_of": AS_OF,
        "employee": {
            "first_name": "Anita",
            "last_name": "Sharma",
            "employee_code": "ACM001",
            "designation": "Senior Engineer",
            "date_of_joining": date(2022, 4, 1),
            "work_email": "anita@acme.test",
        },
        "salary": {"ctc_annual": Decimal("1250000")},
        "tenant": {"name": "Acme Technologies Pvt Ltd"},
        "manual": {"reporting_date": date(2026, 10, 1)},
        "salary_components": [
            {
                "name": "Basic",
                "monthly_amount": Decimal("41667"),
                "annual_amount": Decimal("500000"),
                "part_of_ctc": True,
            },
            {
                "name": "Special allowance",
                "monthly_amount": Decimal("62500"),
                "annual_amount": Decimal("750000"),
                "part_of_ctc": True,
            },
        ],
    }
    base.update(overrides)
    return MergeContext(**base)


# ═══════════════════════════════════════════════════════════════════════════
# Indian numbering — the checklist calls this out explicitly
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, "Rupees Zero Only"),
        (1, "Rupees One Only"),
        (100, "Rupees One Hundred Only"),
        (1000, "Rupees One Thousand Only"),
        (100000, "Rupees One Lakh Only"),
        (1250000, "Rupees Twelve Lakh Fifty Thousand Only"),
        (10000000, "Rupees One Crore Only"),
        (
            12345678,
            "Rupees One Crore Twenty Three Lakh Forty Five Thousand "
            "Six Hundred Seventy Eight Only",
        ),
        (
            999999999,
            "Rupees Ninety Nine Crore Ninety Nine Lakh Ninety Nine Thousand "
            "Nine Hundred Ninety Nine Only",
        ),
    ],
)
def test_amount_in_words_uses_lakh_and_crore(value: int, expected: str) -> None:
    assert amount_in_words(value) == expected


def test_amount_in_words_never_says_million_or_billion() -> None:
    """The whole point: the Western system is wrong for an Indian letter."""
    for value in (1_000_000, 12_500_000, 1_000_000_000):
        words = amount_in_words(value)
        assert "Million" not in words
        assert "Billion" not in words
        assert "Lakh" in words or "Crore" in words


def test_the_checklist_example_renders_correctly() -> None:
    assert amount_in_words(1250000) == "Rupees Twelve Lakh Fifty Thousand Only"


def test_paise_are_spelled_out() -> None:
    assert (
        amount_in_words(Decimal("1250000.50"))
        == "Rupees Twelve Lakh Fifty Thousand and Fifty Paise Only"
    )


def test_beyond_ninety_nine_crore_keeps_counting_in_crore() -> None:
    words = number_in_words(12_000_000_000)  # 1200 crore
    assert "Crore" in words
    assert "Billion" not in words


@pytest.mark.parametrize(
    "value,expected",
    [
        (100, "100"),
        (1000, "1,000"),
        (100000, "1,00,000"),
        (1250000, "12,50,000"),
        (12345678, "1,23,45,678"),
        (Decimal("1250000.50"), "12,50,000.50"),
    ],
)
def test_indian_digit_grouping(value, expected: str) -> None:
    assert group_indian(value) == expected


# ═══════════════════════════════════════════════════════════════════════════
# Required tokens — the "Dear ," guard
# ═══════════════════════════════════════════════════════════════════════════


def test_an_unresolvable_token_blocks_generation_and_names_it() -> None:
    with pytest.raises(LetterMergeError) as exc:
        render_template(
            body_html="<p>Dear {{employee.middle_name}},</p>",
            context=context(),
        )
    assert "employee.middle_name" in str(exc.value)


def test_a_required_token_resolving_to_empty_blocks_generation() -> None:
    """A letter that reads "Dear ," is a real incident, not a cosmetic bug."""
    blank = context(employee={"first_name": "", "date_of_joining": date(2022, 4, 1)})

    with pytest.raises(LetterMergeError) as exc:
        render_template(
            body_html="<p>Dear {{employee.first_name}},</p>",
            context=blank,
            required_tokens={"employee.first_name"},
        )
    message = str(exc.value)
    assert "employee.first_name" in message
    assert "blank" in message


def test_an_unknown_source_names_the_available_ones() -> None:
    with pytest.raises(LetterMergeError) as exc:
        render_template(body_html="{{payroll.something}}", context=context())
    assert "payroll" in str(exc.value)
    assert "employee" in str(exc.value)


def test_an_unknown_computed_field_lists_the_real_ones() -> None:
    with pytest.raises(LetterMergeError) as exc:
        render_template(body_html="{{computed.nonsense}}", context=context())
    assert "computed.ctc_table" in str(exc.value)


def test_a_non_required_empty_token_is_allowed_through() -> None:
    result = render_template(
        body_html="<p>{{manual.note}}</p>",
        context=context(manual={"note": ""}),
        required_tokens=set(),
    )
    assert result == "<p></p>"


# ═══════════════════════════════════════════════════════════════════════════
# Escaping — templates are trusted, merged values are not
# ═══════════════════════════════════════════════════════════════════════════


def test_merged_values_are_html_escaped() -> None:
    """The checklist test: a name containing angle brackets."""
    hostile = context(
        employee={
            "first_name": "<script>alert('xss')</script>",
            "last_name": "Sharma",
            "date_of_joining": date(2022, 4, 1),
        }
    )
    result = render_template(body_html="<p>Dear {{employee.first_name}},</p>", context=hostile)

    assert "<script>" not in result
    assert "&lt;script&gt;" in result


def test_template_markup_itself_is_preserved() -> None:
    """The template is the tenant's own document design. It is trusted."""
    result = render_template(
        body_html='<div class="letterhead"><strong>{{employee.first_name}}</strong></div>',
        context=context(),
    )
    assert '<div class="letterhead">' in result
    assert "<strong>Anita</strong>" in result


def test_a_quote_in_a_value_cannot_break_out_of_an_attribute() -> None:
    tricky = context(
        employee={
            "first_name": 'x" onmouseover="alert(1)',
            "date_of_joining": date(2022, 4, 1),
        }
    )
    result = render_template(
        body_html='<span title="{{employee.first_name}}">hi</span>', context=tricky
    )
    assert 'onmouseover="alert(1)"' not in result
    assert "&quot;" in result


# ═══════════════════════════════════════════════════════════════════════════
# Computed fields
# ═══════════════════════════════════════════════════════════════════════════


def test_the_ctc_table_renders_every_in_ctc_component_with_a_total() -> None:
    result = render_template(body_html="{{computed.ctc_table}}", context=context())

    assert "Basic" in result
    assert "Special allowance" in result
    assert "5,00,000" in result
    assert "12,50,000" in result
    assert "Total CTC" in result


def test_the_ctc_table_excludes_components_outside_ctc() -> None:
    ctx = context(
        salary_components=[
            {
                "name": "Basic",
                "monthly_amount": Decimal("41667"),
                "annual_amount": Decimal("500000"),
                "part_of_ctc": True,
            },
            {
                "name": "Fuel reimbursement",
                "monthly_amount": Decimal("2000"),
                "annual_amount": Decimal("24000"),
                "part_of_ctc": False,
            },
        ]
    )
    result = render_template(body_html="{{computed.ctc_table}}", context=ctx)
    assert "Basic" in result
    assert "Fuel reimbursement" not in result


def test_the_ctc_table_refuses_when_no_salary_is_assigned() -> None:
    with pytest.raises(LetterMergeError, match="no salary assigned"):
        render_template(body_html="{{computed.ctc_table}}", context=context(salary_components=[]))


def test_a_component_name_in_the_ctc_table_is_escaped() -> None:
    ctx = context(
        salary_components=[
            {
                "name": "<b>Basic</b>",
                "monthly_amount": Decimal("1"),
                "annual_amount": Decimal("12"),
                "part_of_ctc": True,
            }
        ]
    )
    result = render_template(body_html="{{computed.ctc_table}}", context=ctx)
    assert "&lt;b&gt;Basic&lt;/b&gt;" in result


def test_tenure_is_years_and_months() -> None:
    result = render_template(body_html="{{computed.tenure}}", context=context())
    # 2022-04-01 to 2026-09-06 is 4 years 5 months.
    assert result == "4 years and 5 months"


def test_tenure_uses_the_exit_date_when_there_is_one() -> None:
    ctx = context(
        employee={
            "first_name": "Anita",
            "date_of_joining": date(2022, 4, 1),
            "date_of_exit": date(2024, 4, 1),
        }
    )
    assert render_template(body_html="{{computed.tenure}}", context=ctx) == "2 years"


def test_tenure_refuses_without_a_joining_date() -> None:
    ctx = context(employee={"first_name": "Anita"})
    with pytest.raises(LetterMergeError, match="date of joining"):
        render_template(body_html="{{computed.tenure}}", context=ctx)


def test_ctc_in_words_uses_indian_numbering() -> None:
    result = render_template(body_html="{{computed.ctc_in_words}}", context=context())
    assert result == "Rupees Twelve Lakh Fifty Thousand Only"


# ═══════════════════════════════════════════════════════════════════════════
# Formatting and structure
# ═══════════════════════════════════════════════════════════════════════════


def test_money_is_grouped_indian_style_automatically() -> None:
    result = render_template(body_html="{{salary.ctc_annual}}", context=context())
    assert result == "12,50,000"


def test_dates_render_in_a_letter_friendly_format() -> None:
    result = render_template(body_html="{{manual.reporting_date}}", context=context())
    assert result == "01 October 2026"


def test_header_body_and_footer_are_concatenated_in_order() -> None:
    result = render_template(
        header_html="<header>{{tenant.name}}</header>",
        body_html="<main>{{employee.first_name}}</main>",
        footer_html="<footer>ref</footer>",
        context=context(),
    )
    assert result.index("<header>") < result.index("<main>") < result.index("<footer>")


def test_extract_tokens_finds_tokens_across_all_three_fragments() -> None:
    tokens = extract_tokens(
        "<p>{{tenant.name}}</p>", "<p>{{employee.first_name}}</p>", "<p>{{computed.tenure}}</p>"
    )
    assert tokens == {"tenant.name", "employee.first_name", "computed.tenure"}


def test_whitespace_inside_braces_is_tolerated() -> None:
    result = render_template(body_html="{{  employee.first_name  }}", context=context())
    assert result == "Anita"


def test_the_same_token_used_twice_resolves_both_times() -> None:
    result = render_template(
        body_html="{{employee.first_name}} ... {{employee.first_name}}", context=context()
    )
    assert result == "Anita ... Anita"


def test_rendering_is_deterministic() -> None:
    """Needed for the stored-hash verification an issued letter relies on."""
    body = "<p>Dear {{computed.full_name}}, {{computed.ctc_table}}</p>"
    assert render_template(body_html=body, context=context()) == render_template(
        body_html=body, context=context()
    )
