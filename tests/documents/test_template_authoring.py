"""Template authoring aids from slice 03 §8: token checks and preview rendering."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.services.documents.letter_merge import (
    AVAILABLE_FIELDS,
    MergeContext,
    preview_render,
    render_template,
    template_token_problems,
)


def _context(**employee) -> MergeContext:
    return MergeContext(
        as_of=date(2026, 9, 1),
        employee={"first_name": "Anita", "last_name": "Sharma", **employee},
        salary={"ctc_annual": Decimal("1250000"), "currency": "INR"},
        tenant={"name": "Acme"},
        manual={"joining_date": date(2026, 10, 1)},
    )


# ── Token checks at save time ───────────────────────────────────────────────


def test_a_misspelt_employee_field_is_caught_when_saved() -> None:
    problems = template_token_problems("<p>Dear {{employee.frist_name}}</p>")
    assert len(problems) == 1
    assert "frist_name" in problems[0]
    assert "employee.first_name" in problems[0]


def test_an_unknown_source_is_caught() -> None:
    assert "unknown source" in template_token_problems("{{payroll.net}}")[0]


def test_an_unknown_computed_field_is_caught() -> None:
    assert "not a computed field" in template_token_problems("{{computed.magic}}")[0]


def test_manual_fields_are_open_ended() -> None:
    assert template_token_problems("{{manual.anything_the_issuer_types}}") == []


def test_every_available_field_passes_its_own_check() -> None:
    body = " ".join(
        f"{{{{{source}.{field}}}}}"
        for source, fields in AVAILABLE_FIELDS.items()
        for field in fields
    )
    assert template_token_problems(body) == []


def test_the_service_context_uses_exactly_the_declared_employee_fields() -> None:
    """The save-time check and the issue-time context must not drift apart."""
    from app.services.documents.service import _employee_values

    class _Employee:
        def __getattr__(self, name: str):
            return None

    assert set(_employee_values(_Employee())) == set(AVAILABLE_FIELDS["employee"])


# ── Preview ─────────────────────────────────────────────────────────────────


def test_preview_marks_every_hole_instead_of_refusing() -> None:
    html, problems = preview_render(
        body_html="<p>{{employee.first_name}} {{employee.nope}} {{computed.tenure}}</p>",
        context=_context(),
    )
    assert "Anita" in html
    assert html.count("[missing:") == 2
    assert len(problems) == 2


def test_preview_escapes_merged_values_like_issuance_does() -> None:
    html, problems = preview_render(
        body_html="<p>{{employee.first_name}}</p>",
        context=_context(first_name="<script>alert(1)</script>"),
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert problems == []


def test_a_clean_preview_matches_what_issuance_would_produce() -> None:
    body = "<p>Dear {{employee.first_name}}, joining {{manual.joining_date}}.</p>"
    html, problems = preview_render(body_html=body, context=_context())
    assert problems == []
    assert html == render_template(body_html=body, context=_context())


def test_an_empty_required_value_is_a_problem_in_preview() -> None:
    _, problems = preview_render(
        body_html="<p>{{employee.last_name}}</p>",
        context=_context(last_name=""),
        required_tokens={"employee.last_name"},
    )
    assert problems and "resolves to nothing" in problems[0]
