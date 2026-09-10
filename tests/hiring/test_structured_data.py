"""A careers page nobody finds by searching is a link recruiters have to send."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from app.services.hiring.structured_data import (
    REQUIRED_PROPERTIES,
    JobPostingSeo,
    job_posting_jsonld,
    render_jsonld_script,
    validate_posting,
)

TODAY = date(2026, 9, 7)


def _posting(**overrides) -> JobPostingSeo:
    base = {
        "title": "Senior Backend Engineer",
        "description_html": "<p>Build things that matter.</p>",
        "date_posted": date(2026, 9, 1),
        "organisation_name": "Acme Technologies",
        "organisation_logo_url": "https://acme.test/logo.png",
        "valid_through": date(2026, 12, 31),
        "city": "Bengaluru",
        "state": "KA",
        "identifier": "REQ-001",
        "direct_apply_url": "https://acme.test/careers/acme/jobs/REQ-001/apply",
    }
    base.update(overrides)
    return JobPostingSeo(**base)


def test_the_markup_carries_every_property_google_requires() -> None:
    """Anything missing means the posting is dropped from the index entirely —
    not ranked lower, dropped."""
    data = job_posting_jsonld(_posting())
    for required in REQUIRED_PROPERTIES:
        assert data.get(required), required
    assert data["@type"] == "JobPosting"
    assert data["@context"] == "https://schema.org/"


def test_the_location_is_a_postal_address_google_can_read() -> None:
    data = job_posting_jsonld(_posting())
    address = data["jobLocation"]["address"]
    assert address["addressLocality"] == "Bengaluru"
    assert address["addressRegion"] == "KA"
    assert address["addressCountry"] == "IN"


def test_valid_through_carries_a_time_so_the_job_does_not_expire_a_day_early() -> None:
    """A bare date is read as midnight, which takes the job down at the start of
    its last day rather than the end."""
    data = job_posting_jsonld(_posting(valid_through=date(2026, 12, 31)))
    assert data["validThrough"].startswith("2026-12-31T23:59")


def test_direct_apply_is_set_when_the_application_happens_on_our_page() -> None:
    assert job_posting_jsonld(_posting())["directApply"] is True
    assert job_posting_jsonld(_posting(direct_apply_url=None))["directApply"] is False


def test_no_salary_is_emitted_unless_the_client_chose_to_show_one() -> None:
    """Emitting a band the client kept off the visible page would publish it to
    search results, which is worse than publishing it on the page."""
    data = job_posting_jsonld(
        _posting(ctc_min=Decimal("1500000"), ctc_max=Decimal("2500000"), show_salary=False)
    )
    assert "baseSalary" not in data


def test_a_salary_the_client_chose_to_show_is_emitted_as_an_annual_range() -> None:
    data = job_posting_jsonld(
        _posting(ctc_min=Decimal("1500000"), ctc_max=Decimal("2500000"), show_salary=True)
    )
    assert data["baseSalary"]["currency"] == "INR"
    assert data["baseSalary"]["value"]["unitText"] == "YEAR"
    assert data["baseSalary"]["value"]["minValue"] == 1500000.0


# ═══════════════════════════════════════════════════════════════════════════
# Validation
# ═══════════════════════════════════════════════════════════════════════════


def test_a_complete_posting_validates_clean() -> None:
    assert validate_posting(_posting(), as_of=TODAY) == []


@pytest.mark.parametrize(
    "field,value", [("title", ""), ("description_html", ""), ("organisation_name", "")]
)
def test_a_missing_required_property_is_reported_as_not_indexable(field, value) -> None:
    problems = validate_posting(_posting(**{field: value}), as_of=TODAY)
    assert problems
    assert any("not be findable by search" in p for p in problems)


def test_an_expired_posting_is_reported_because_it_harms_the_whole_site() -> None:
    """Google flags a site for stale jobs, and that affects every other job on
    it — so an expired posting is worse than no posting."""
    problems = validate_posting(_posting(valid_through=date(2026, 8, 1)), as_of=TODAY)
    assert any("stale listings" in p for p in problems)


def test_a_posting_with_no_location_still_indexes_but_is_flagged() -> None:
    problems = validate_posting(_posting(city=None, state=None), as_of=TODAY)
    assert any("location-filtered searches" in p for p in problems)
    assert not any("not be findable by search" in p for p in problems)


def test_showing_a_salary_with_half_a_band_is_reported() -> None:
    """Either fill in both ends or turn the display off, so the page and the
    markup say the same thing."""
    problems = validate_posting(
        _posting(show_salary=True, ctc_min=Decimal("1500000"), ctc_max=None), as_of=TODAY
    )
    assert any("incomplete" in p for p in problems)


# ═══════════════════════════════════════════════════════════════════════════
# Rendering
# ═══════════════════════════════════════════════════════════════════════════


def test_the_script_block_is_valid_json_inside_a_script_tag() -> None:
    rendered = render_jsonld_script(_posting())
    assert rendered.startswith('<script type="application/ld+json">')
    assert rendered.endswith("</script>")
    payload = rendered[len('<script type="application/ld+json">'): -len("</script>")]
    assert json.loads(payload.replace("<\\/", "</"))["@type"] == "JobPosting"


def test_a_closing_script_tag_in_a_description_cannot_escape_the_block() -> None:
    """Otherwise the block closes early and the rest of the JSON renders as text
    on the page — and, worse, whatever followed it runs."""
    rendered = render_jsonld_script(
        _posting(description_html="<p>Nice try</p></script><script>alert(1)</script>")
    )
    assert "</script><script>alert(1)" not in rendered
    assert rendered.count("</script>") == 1


def test_unicode_survives_rendering() -> None:
    """An Indian careers page has names and places that are not ASCII, and
    escaping them into \\u sequences makes the markup unreadable when somebody
    is debugging why a job did not index."""
    rendered = render_jsonld_script(_posting(city="Bengaluru — Koramangala"))
    assert "Bengaluru — Koramangala" in rendered
