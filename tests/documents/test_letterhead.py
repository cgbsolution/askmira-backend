"""The drawn letterhead, the letter's date style and the annexure's wording."""

from __future__ import annotations

import io
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest
from pypdf import PdfReader

from app.services.documents.annexure import letter_label
from app.services.documents.letter_merge import (
    LetterMergeError,
    MergeContext,
    long_date,
    period_in_words,
    render_template,
)
from app.services.documents.letterhead_pdf import LetterheadArt, body_area, draw_letterhead, mix
from app.services.documents.pdf import render_pdf, wrap_letter

ART = LetterheadArt(
    design="wave",
    company_name="CGBiz Solutions Private Limited",
    address="2nd Floor, AKR Tech Park, Bengaluru 560068",
    email="info@cgbindia.com",
    website="www.cgbsolution.com",
)


@pytest.mark.parametrize("design", ["wave", "classic"])
def test_each_design_draws_one_page_with_the_company_details(design: str) -> None:
    data = draw_letterhead(replace(ART, design=design))
    reader = PdfReader(io.BytesIO(data))
    assert len(reader.pages) == 1
    text = reader.pages[0].extract_text()
    assert "info@cgbindia.com" in text
    assert "www.cgbsolution.com" in text


def test_plain_paper_prints_nothing_and_leaves_ordinary_margins() -> None:
    data = render_pdf(
        merged_html="<p>Hello</p>",
        reference_number="R/1",
        art=LetterheadArt(design="none", company_name="Hidden Co"),
    )
    assert "Hidden Co" not in PdfReader(io.BytesIO(data)).pages[0].extract_text()
    assert body_area("none").top < body_area("wave").top


def test_a_letter_is_printed_on_the_letterhead_on_every_page() -> None:
    body = "<p>One</p><div class='page-break'></div><p>Two</p>"
    reader = PdfReader(io.BytesIO(render_pdf(merged_html=body, reference_number="R/2", art=ART)))
    assert len(reader.pages) == 2
    for page in reader.pages:
        assert "CGBiz Solutions Private Limited" in page.extract_text()


def test_the_same_letter_on_the_same_letterhead_is_the_same_bytes() -> None:
    kwargs = {"merged_html": "<p>Same</p>", "reference_number": "R/3", "art": ART}
    assert render_pdf(**kwargs) == render_pdf(**kwargs)


def test_the_reference_line_can_be_left_off() -> None:
    shown = wrap_letter(merged_html="<p>x</p>", reference_number="R/4", art=ART)
    hidden = wrap_letter(
        merged_html="<p>x</p>", reference_number="R/4", art=ART, show_reference=False
    )
    assert "Ref. R/4" in shown and "pdf:pagenumber" in shown
    assert "Ref. R/4" not in hidden


def test_a_broken_logo_does_not_stop_the_letter() -> None:
    data = draw_letterhead(replace(ART, logo=b"not an image"))
    assert data.startswith(b"%PDF-")


def test_colours_mix_towards_white_and_black() -> None:
    assert mix("#000000", "#FFFFFF", 0.5) == "#808080"
    assert mix("#1B5A92", "#1B5A92", 0.3) == "#1B5A92"


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 3, 9), "09th March 2026"),
        (date(2026, 4, 1), "01st April 2026"),
        (date(2026, 4, 2), "02nd April 2026"),
        (date(2026, 4, 3), "03rd April 2026"),
        (date(2026, 4, 11), "11th April 2026"),
        (date(2026, 4, 12), "12th April 2026"),
        (date(2026, 4, 13), "13th April 2026"),
        (date(2026, 4, 21), "21st April 2026"),
        (date(2026, 4, 22), "22nd April 2026"),
        (date(2026, 3, 31), "31st March 2026"),
    ],
)
def test_letter_dates_carry_their_ordinal(day: date, expected: str) -> None:
    assert long_date(day, markup=False) == expected


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        (30, "one month"),
        (60, "two months"),
        (90, "three months"),
        (180, "six months"),
        (45, "45 days"),
        (1, "1 day"),
        (0, ""),
        (None, ""),
    ],
)
def test_periods_read_as_words(days: int | None, expected: str) -> None:
    assert period_in_words(days) == expected


@pytest.mark.parametrize(
    ("code", "name", "expected"),
    [
        ("BASIC", "BASIC SALARY", "Basic Salary"),
        ("HRA", "HOUSE RENT ALLOWANCE", "House Rent Allowance (HRA)"),
        ("INSURANCE", "INSURANCE", "Medical Insurance Premium"),
        ("LTA", "LEAVE TRAVEL ALLOWANCE", "Leave Travel Allowance"),
        ("X_MANUAL", "COST OF THE CAB MANUAL", "Cost of the Cab"),
        ("FOOD", "Food coupons", "Food coupons"),
    ],
)
def test_wage_types_read_as_a_letter_prints_them(code: str, name: str, expected: str) -> None:
    assert letter_label(code, name) == expected


def _annexure() -> dict:
    return {
        "name": "Deepak Rawat",
        "location": "Delhi",
        "designation": "Senior Software Engineer",
        "effective_label": "Appraisal W.E.F.",
        "effective_from": date(2026, 4, 1),
        "earnings": [("Basic Salary", Decimal("25833"), Decimal("310000"))],
        "gross": (Decimal("25833"), Decimal("310000")),
        "employee": [("Provident Fund", Decimal("1800"), Decimal("21600"))],
        "employee_total": (Decimal("1800"), Decimal("21600")),
        "net": (Decimal("24033"), Decimal("288400")),
        "employer": [("ESIC Contribution", Decimal(0), Decimal(0))],
        "employer_total": (Decimal(0), Decimal(0)),
        "ctc": (Decimal("25833"), Decimal("310000")),
    }


def test_the_annexure_prints_the_rows_in_whole_rupees_with_dashes_for_nothing() -> None:
    context = MergeContext(as_of=date(2026, 4, 1), annexure=_annexure())
    html = render_template(body_html="{{computed.salary_annexure}}", context=context)
    assert "Appraisal W.E.F." in html and "01<sup>st</sup> April 2026" in html
    assert "3,10,000" in html and "25,833" in html
    assert '<tr class="total"><td>Gross Salary</td>' in html
    assert '<td class="num">-</td>' in html


def test_the_annexure_says_why_it_could_not_be_worked_out() -> None:
    context = MergeContext(as_of=date(2026, 4, 1), annexure={"error": "No PF rules."})
    with pytest.raises(LetterMergeError, match="No PF rules"):
        render_template(body_html="{{computed.salary_annexure}}", context=context)


def test_an_empty_required_value_is_named_for_the_caller() -> None:
    context = MergeContext(as_of=date(2026, 4, 1), employee={"date_of_exit": None})
    with pytest.raises(LetterMergeError) as caught:
        render_template(body_html="{{employee.date_of_exit}}", context=context)
    assert caught.value.empty_tokens == ["employee.date_of_exit"]


def test_a_whole_rupee_amount_has_no_paise() -> None:
    context = MergeContext(as_of=date(2026, 4, 1), salary={"ctc_annual": Decimal("620000.00")})
    assert render_template(body_html="{{salary.ctc_annual}}", context=context) == "6,20,000"
    context.salary["ctc_annual"] = Decimal("620000.50")
    assert render_template(body_html="{{salary.ctc_annual}}", context=context) == "6,20,000.50"
