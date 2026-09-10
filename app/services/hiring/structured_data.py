"""schema.org JobPosting markup for careers pages.

Google indexes jobs from this markup, not from the visible page. A careers page
without it is a careers page nobody finds by searching, which makes the whole
portal a link recruiters have to send people rather than a source of applicants.

Two rules, both learned from how the validator actually behaves:

**Required properties are required.** `title`, `description`, `datePosted` and
`hiringOrganization` missing means the posting is dropped from the index
entirely — not ranked lower, dropped. So `validate_posting` returns what is
missing, and the caller refuses to publish a page that would not be indexed
rather than serving one that quietly is not.

**`validThrough` in the past de-indexes the job.** An expired posting still on
the page is worse than no posting: Google flags the site for stale jobs, which
affects every other job on it.

Pure. No database, no rendering framework — it returns a dict the caller
serialises, which is what lets the shape be asserted directly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

__all__ = [
    "REQUIRED_PROPERTIES",
    "JobPostingSeo",
    "job_posting_jsonld",
    "render_jsonld_script",
    "validate_posting",
]

#: Google's own required set. Anything absent means the job is not indexed.
REQUIRED_PROPERTIES = ("title", "description", "datePosted", "hiringOrganization")

#: schema.org employment types.
EMPLOYMENT_TYPES = {
    "full_time": "FULL_TIME",
    "part_time": "PART_TIME",
    "contract": "CONTRACTOR",
    "intern": "INTERN",
}


@dataclass(frozen=True, slots=True)
class JobPostingSeo:
    """Everything the markup needs, in our own terms."""

    title: str
    description_html: str
    date_posted: date
    organisation_name: str
    organisation_url: str | None = None
    organisation_logo_url: str | None = None
    valid_through: date | None = None
    employment_type: str = "full_time"
    city: str | None = None
    state: str | None = None
    country: str = "IN"
    postal_code: str | None = None
    ctc_min: Decimal | None = None
    ctc_max: Decimal | None = None
    show_salary: bool = False
    identifier: str | None = None
    direct_apply_url: str | None = None


def job_posting_jsonld(posting: JobPostingSeo) -> dict[str, Any]:
    """The JSON-LD object for one job."""
    data: dict[str, Any] = {
        "@context": "https://schema.org/",
        "@type": "JobPosting",
        "title": posting.title,
        "description": posting.description_html,
        "datePosted": posting.date_posted.isoformat(),
        "employmentType": EMPLOYMENT_TYPES.get(posting.employment_type, "FULL_TIME"),
        "hiringOrganization": {
            "@type": "Organization",
            "name": posting.organisation_name,
        },
        # Google added this in 2023 and reads it to mark listings that apply on
        # the employer's own site. Omitting it does not break indexing but does
        # lose the badge, and the badge is worth applications.
        "directApply": bool(posting.direct_apply_url),
    }

    if posting.organisation_url:
        data["hiringOrganization"]["sameAs"] = posting.organisation_url
    if posting.organisation_logo_url:
        data["hiringOrganization"]["logo"] = posting.organisation_logo_url
    if posting.identifier:
        data["identifier"] = {
            "@type": "PropertyValue",
            "name": posting.organisation_name,
            "value": posting.identifier,
        }
    if posting.valid_through:
        # Serialised with a time, because a bare date is read as midnight and
        # the job disappears a day early.
        data["validThrough"] = datetime.combine(
            posting.valid_through, datetime.min.time()
        ).replace(hour=23, minute=59).isoformat()

    if posting.city or posting.state:
        data["jobLocation"] = {
            "@type": "Place",
            "address": {
                "@type": "PostalAddress",
                "addressLocality": posting.city,
                "addressRegion": posting.state,
                "addressCountry": posting.country,
                **({"postalCode": posting.postal_code} if posting.postal_code else {}),
            },
        }

    # Salary only where the client chose to show it. Emitting a band the client
    # kept off the visible page would publish it to search results, which is
    # worse than publishing it on the page.
    if posting.show_salary and posting.ctc_min is not None and posting.ctc_max is not None:
        data["baseSalary"] = {
            "@type": "MonetaryAmount",
            "currency": "INR",
            "value": {
                "@type": "QuantitativeValue",
                "minValue": float(posting.ctc_min),
                "maxValue": float(posting.ctc_max),
                "unitText": "YEAR",
            },
        }

    return data


def validate_posting(posting: JobPostingSeo, *, as_of: date | None = None) -> list[str]:
    """What would stop this job being indexed. Empty means it is fine.

    Returned as sentences rather than field names: whoever sees this is
    configuring a careers page, not reading Google's specification.
    """
    problems: list[str] = []
    data = job_posting_jsonld(posting)

    for required in REQUIRED_PROPERTIES:
        value = data.get(required)
        # `hiringOrganization` is an object, and an object with an empty name is
        # still truthy. Checking the container rather than what is in it passed
        # a posting with no company name on it, which Google drops.
        if isinstance(value, dict):
            value = value.get("name")
        if not value:
            problems.append(
                f"'{required}' is empty. Google drops a JobPosting missing it from the "
                "index entirely rather than ranking it lower, so this page would not "
                "be findable by search at all."
            )

    today = as_of or date.today()
    if posting.valid_through and posting.valid_through < today:
        problems.append(
            f"This posting expired on {posting.valid_through}. An expired job left on "
            "the page gets the site flagged for stale listings, which affects every "
            "other job on it — take it down rather than leaving it."
        )

    if not posting.city and not posting.state:
        problems.append(
            "No location. The job will still index, but it will not appear in the "
            "location-filtered searches that most candidates actually use."
        )

    if posting.show_salary and (posting.ctc_min is None or posting.ctc_max is None):
        problems.append(
            "The salary is set to show but the band is incomplete, so no salary is "
            "emitted. Either fill in both ends or turn the display off, so the page "
            "and the markup say the same thing."
        )

    return problems


def render_jsonld_script(posting: JobPostingSeo) -> str:
    """The `<script type="application/ld+json">` block, ready to embed.

    `</script>` inside a description would close the block early and leave the
    rest of the JSON rendering as text on the page, so the sequence is escaped.
    `json.dumps` does not do this — it is valid JSON either way, and only a
    problem because the JSON is going inside HTML.
    """
    payload = json.dumps(job_posting_jsonld(posting), ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("</", "<\\/")
    return f'<script type="application/ld+json">{payload}</script>'
