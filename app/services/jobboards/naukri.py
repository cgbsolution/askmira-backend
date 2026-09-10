"""The Naukri adapter. Everything Naukri-shaped is in this file.

═══════════════════════════════════════════════════════════════════════════════
THE TAXONOMY IS THE WORK
═══════════════════════════════════════════════════════════════════════════════

Naukri does not take a job title and a description. It takes a functional area,
an industry, a role category and a role — from its own controlled vocabularies —
and rejects a posting whose combination it does not recognise. None of those
concepts exist on LinkedIn or Indeed, which is exactly why they live here and
not on the requisition.

The mapping is data, in `FUNCTIONAL_AREAS` and `ROLE_CATEGORIES` below, and it
is **explicitly incomplete**. Naukri's real vocabularies run to hundreds of
entries behind a partner agreement. What is here covers the common Indian
mid-market roles; anything unmatched falls to a stated default *and says so in
the warnings*, rather than guessing silently and having the recruiter discover
the job was listed under the wrong function three weeks later.

**Refresh matters more here than on any other board.** A Naukri posting decays
in listing position within days, and Indian clients expect a scheduled refresh —
it is a standard part of what they are paying the board for. A product that
posts once and never boosts looks, from the client's side, like the posting
stopped working.

═══════════════════════════════════════════════════════════════════════════════
NOT TESTED AGAINST A REAL NAUKRI ACCOUNT.
The HTTP layer is written against Naukri's documented recruiter API shape and
is exercised only against a transport double. Field names, the auth handshake
and the error envelope all need verifying on a live account before this can be
called working — that is a standing human gate, not something the test suite
can close.
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.services.jobboards.base import (
    BoardCredentials,
    BoardPermanentFailure,
    BoardTransientFailure,
    JobBoardAdapter,
    JobSpec,
    PublishResult,
    RateLimited,
    RawApplication,
    Transport,
)

__all__ = ["NaukriAdapter", "map_functional_area", "map_role_category"]

BOARD = "naukri"

#: Naukri's functional-area vocabulary, keyed by the words that appear in our
#: job titles and departments. Deliberately partial — see the module docstring.
FUNCTIONAL_AREAS: dict[str, tuple[str, ...]] = {
    "IT Software - Application Programming": (
        "engineer", "developer", "programmer", "backend", "frontend", "fullstack",
        "full stack", "software", "sde", "architect",
    ),
    "IT Software - QA & Testing": ("qa", "test", "sdet", "quality assurance"),
    "IT Software - DBA / Datawarehousing": ("data engineer", "dba", "warehouse", "etl"),
    "Analytics & Business Intelligence": (
        "analyst", "analytics", "data scientist", "business intelligence", "bi ",
    ),
    "Sales, Retail, Business Development": (
        "sales", "business development", "account executive", "bd ", "revenue",
    ),
    "Marketing, Advertising, MR, PR": ("marketing", "brand", "growth", "seo", "content"),
    "HR / Recruitment / Administration / IR": (
        "hr", "human resource", "recruiter", "talent", "people ops", "admin",
    ),
    "Accounts, Finance, Tax, Company Secretary, Audit": (
        "finance", "accounts", "accountant", "audit", "tax", "controller", "cfo",
    ),
    "Design, Creative, User Experience": ("design", "ux", "ui ", "creative", "product design"),
    "ITES, BPO, KPO, LPO, Customer Service, Operations": (
        "support", "customer success", "operations", "bpo", "service desk",
    ),
}

#: Role category, one level below functional area.
ROLE_CATEGORIES: dict[str, tuple[str, ...]] = {
    "Programming & Design": ("engineer", "developer", "architect", "sde"),
    "Quality Assurance": ("qa", "test", "sdet"),
    "Business Analyst": ("analyst", "business analysis"),
    "Corporate Sales": ("sales", "account executive", "business development"),
    "Digital Marketing": ("marketing", "growth", "seo", "performance"),
    "HR/ Recruitment / IR": ("hr", "recruiter", "talent", "human resource"),
    "Finance/Audit": ("finance", "accounts", "audit", "tax"),
    "UI / UX Design": ("ux", "ui ", "product design"),
}

#: What an unmatched title is posted under. Named, so the warning can say it.
DEFAULT_FUNCTIONAL_AREA = "Other"
DEFAULT_ROLE_CATEGORY = "Other"

#: Naukri caps the key-skills field. Sending more is rejected, not truncated.
MAX_KEY_SKILLS = 15
MAX_DESCRIPTION_CHARS = 25000

#: A Naukri posting decays in listing position within days. Indian clients
#: expect a scheduled boost as part of what they pay the board for.
DEFAULT_REFRESH_INTERVAL_DAYS = 7


def _haystack(spec: JobSpec) -> str:
    return " ".join(
        part.lower()
        for part in (spec.title, spec.department or "", " ".join(spec.skills))
        if part
    )


def _match(table: dict[str, tuple[str, ...]], text: str, default: str) -> tuple[str, bool]:
    """First vocabulary entry whose keywords appear. Returns whether it matched.

    Iteration order is the dict's insertion order, which is the declaration
    order above — so the mapping is a decision somebody made rather than
    whichever key happened to hash first.
    """
    for value, keywords in table.items():
        if any(keyword in text for keyword in keywords):
            return value, True
    return default, False


def map_functional_area(spec: JobSpec) -> tuple[str, bool]:
    return _match(FUNCTIONAL_AREAS, _haystack(spec), DEFAULT_FUNCTIONAL_AREA)


def map_role_category(spec: JobSpec) -> tuple[str, bool]:
    return _match(ROLE_CATEGORIES, _haystack(spec), DEFAULT_ROLE_CATEGORY)


class NaukriAdapter(JobBoardAdapter):
    board = BOARD
    #: Naukri's documented recruiter-API ceiling. Conservative on purpose: an
    #: account throttled by Naukri is throttled for everyone in the tenant.
    rate_limit_per_minute = 20

    #: Naukri postings decay in listing position within days, and Indian
    #: clients expect a scheduled boost as part of what they pay the board
    #: for. Declared here so nothing outside this file has to know it.
    default_refresh_interval_days = DEFAULT_REFRESH_INTERVAL_DAYS

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    # ── Payload construction ────────────────────────────────────────────────

    def build_payload(self, spec: JobSpec) -> tuple[dict[str, Any], list[str]]:
        """Our job in Naukri's vocabulary, plus everything worth saying about it.

        The warnings are not decoration. A posting that landed under "Other"
        because nothing matched will get a fraction of the views, and the
        recruiter needs to know that today rather than after three quiet weeks.
        """
        warnings: list[str] = []

        functional_area, matched_area = map_functional_area(spec)
        if not matched_area:
            warnings.append(
                f"Nothing in '{spec.title}' matched a Naukri functional area, so it is "
                f"posted under '{DEFAULT_FUNCTIONAL_AREA}'. Candidates browsing by "
                "function will not see it; set the functional area by hand on Naukri if "
                "this role belongs to one."
            )
        role_category, matched_role = map_role_category(spec)
        if not matched_role:
            warnings.append(
                f"No Naukri role category matched, so it is posted under "
                f"'{DEFAULT_ROLE_CATEGORY}'."
            )

        skills = list(dict.fromkeys(s.strip() for s in spec.skills if s.strip()))
        if len(skills) > MAX_KEY_SKILLS:
            warnings.append(
                f"Naukri accepts {MAX_KEY_SKILLS} key skills and this job listed "
                f"{len(skills)}. The first {MAX_KEY_SKILLS} were sent, in the order they "
                "appear on the requisition."
            )
            skills = skills[:MAX_KEY_SKILLS]

        description = spec.description_html
        if len(description) > MAX_DESCRIPTION_CHARS:
            warnings.append(
                f"The description was {len(description)} characters and Naukri accepts "
                f"{MAX_DESCRIPTION_CHARS}. It was cut at the limit — check the end of "
                "the posting reads as a whole sentence."
            )
            description = description[:MAX_DESCRIPTION_CHARS]

        payload: dict[str, Any] = {
            "jobTitle": spec.title,
            "jobDescription": description,
            "functionalArea": functional_area,
            "roleCategory": role_category,
            "keySkills": skills,
            "location": spec.location,
            "vacancies": spec.openings,
            "employmentType": (
                "Full Time, Permanent" if spec.employment_type == "full_time" else "Contract"
            ),
            "referenceId": spec.requisition_code,
        }

        if spec.experience_min_years is not None or spec.experience_max_years is not None:
            payload["experience"] = {
                "min": spec.experience_min_years or 0,
                "max": spec.experience_max_years or (spec.experience_min_years or 0) + 3,
            }

        # Salary is off the advert unless the client asked for it. A board
        # default that shows it would publish a number the client deliberately
        # kept off the page, and it cannot be unpublished from a candidate's
        # memory.
        if spec.show_salary and spec.ctc_min is not None and spec.ctc_max is not None:
            payload["salary"] = {
                "min": str(spec.ctc_min),
                "max": str(spec.ctc_max),
                "currency": "INR",
                "hideSalary": False,
            }
        else:
            payload["salary"] = {"hideSalary": True}
            if spec.ctc_max is not None and not spec.show_salary:
                warnings.append(
                    "The salary band is not shown on this posting. Naukri applications "
                    "run noticeably lower without one; this is the requisition's own "
                    "setting, not a default."
                )

        if spec.apply_url:
            payload["externalApplyUrl"] = spec.apply_url
        if spec.expires_on:
            payload["expiryDate"] = spec.expires_on.isoformat()

        return payload, warnings

    # ── Calls ───────────────────────────────────────────────────────────────

    def _headers(self, credentials: BoardCredentials) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {credentials.secret}",
            "X-Recruiter-Id": str(credentials.extras.get("recruiter_id", "")),
            "Content-Type": "application/json",
        }

    def _check(self, code: int, body: dict[str, Any]) -> None:
        """Turn Naukri's response into the typed failure it actually is.

        The distinction is the whole point. A 429 is a posting that will succeed
        in ninety seconds; a 422 is a posting that will never succeed until
        somebody changes a field. Collapsing them loses either the job or the
        rate limit.
        """
        if 200 <= code < 300:
            return

        message = str(body.get("message") or body.get("error") or f"HTTP {code}")

        if code == 429:
            retry_after = body.get("retryAfter") or body.get("retry_after")
            raise RateLimited(
                f"Naukri rate limit: {message}",
                retry_after_seconds=int(retry_after) if retry_after else None,
            )
        if code in (400, 401, 403, 404, 422):
            raise BoardPermanentFailure(
                f"Naukri rejected this: {message}. Sending it again will be rejected "
                "the same way."
            )
        raise BoardTransientFailure(f"Naukri returned {code}: {message}")

    async def publish_job(
        self, spec: JobSpec, credentials: BoardCredentials
    ) -> PublishResult:
        payload, warnings = self.build_payload(spec)
        code, body = await self._transport.request(
            "POST", "/v1/jobs", json=payload, headers=self._headers(credentials)
        )
        self._check(code, body)

        external_id = str(body.get("jobId") or body.get("id") or "")
        if not external_id:
            raise BoardPermanentFailure(
                "Naukri accepted the posting but returned no job id, so nothing here "
                "can refresh, update or close it. Treated as a failure rather than a "
                "posting we cannot manage."
            )

        expires = body.get("expiryDate")
        return PublishResult(
            external_job_id=external_id,
            published_at=datetime.now(UTC),
            expires_at=(
                datetime.fromisoformat(expires).replace(tzinfo=UTC) if expires else None
            ),
            accepted_as={
                "functionalArea": payload["functionalArea"],
                "roleCategory": payload["roleCategory"],
                "location": body.get("location", payload["location"]),
                "keySkills": payload["keySkills"],
            },
            warnings=tuple(warnings),
        )

    async def update_job(
        self, external_job_id: str, spec: JobSpec, credentials: BoardCredentials
    ) -> PublishResult:
        payload, warnings = self.build_payload(spec)
        code, body = await self._transport.request(
            "PUT", f"/v1/jobs/{external_job_id}",
            json=payload, headers=self._headers(credentials),
        )
        self._check(code, body)
        return PublishResult(
            external_job_id=external_job_id,
            published_at=datetime.now(UTC),
            accepted_as={"functionalArea": payload["functionalArea"]},
            warnings=tuple(warnings),
        )

    async def close_job(self, external_job_id: str, credentials: BoardCredentials) -> None:
        code, body = await self._transport.request(
            "POST", f"/v1/jobs/{external_job_id}/close", headers=self._headers(credentials)
        )
        self._check(code, body)

    async def refresh_job(
        self, external_job_id: str, credentials: BoardCredentials
    ) -> datetime:
        """Boost the posting back up the listing.

        The one board operation clients ask about by name.
        """
        code, body = await self._transport.request(
            "POST", f"/v1/jobs/{external_job_id}/refresh", headers=self._headers(credentials)
        )
        self._check(code, body)
        return datetime.now(UTC) + timedelta(days=DEFAULT_REFRESH_INTERVAL_DAYS)

    async def fetch_applications(
        self,
        external_job_id: str,
        credentials: BoardCredentials,
        *,
        since: datetime | None = None,
    ) -> list[RawApplication]:
        params = f"?since={since.isoformat()}" if since else ""
        code, body = await self._transport.request(
            "GET", f"/v1/jobs/{external_job_id}/applications{params}",
            headers=self._headers(credentials),
        )
        self._check(code, body)
        return [
            self._to_application(row, external_job_id)
            for row in body.get("applications", [])
        ]

    def _to_application(self, row: dict[str, Any], external_job_id: str) -> RawApplication:
        """Naukri's shape into ours.

        Everything unmapped is kept in `raw`. A field nobody thought to map is
        still evidence when a recruiter is asking why a candidate looks wrong.
        """
        applied = row.get("appliedOn") or row.get("applied_at")
        return RawApplication(
            external_application_id=str(row.get("applicationId") or row.get("id") or ""),
            board=BOARD,
            external_job_id=external_job_id,
            full_name=str(row.get("candidateName") or "").strip(),
            email=str(row.get("email") or "").strip(),
            phone=row.get("mobile") or row.get("phone"),
            resume_url=row.get("resumeUrl"),
            current_company=row.get("currentCompany"),
            current_ctc=_decimal(row.get("currentCtc")),
            expected_ctc=_decimal(row.get("expectedCtc")),
            total_experience_years=_decimal(row.get("totalExperience")),
            notice_period_days=_notice_days(row.get("noticePeriod")),
            applied_at=(
                datetime.fromisoformat(applied).replace(tzinfo=UTC) if applied else None
            ),
            raw=row,
        )

    async def fetch_resume(
        self, application: RawApplication, credentials: BoardCredentials
    ) -> bytes:
        if not application.resume_url:
            raise BoardPermanentFailure(
                f"Naukri sent no resume for {application.full_name or 'this applicant'}. "
                "Some candidates apply without one; there is nothing to fetch."
            )
        code, body = await self._transport.request(
            "GET", application.resume_url, headers=self._headers(credentials)
        )
        self._check(code, body)
        content = body.get("content")
        if isinstance(content, bytes):
            return content
        if isinstance(content, str):
            return content.encode("utf-8")
        raise BoardTransientFailure("Naukri returned an empty resume body.")

    async def search_resumes(
        self, query: str, credentials: BoardCredentials, *, limit: int = 25
    ) -> list[RawApplication]:
        """Resume database search, where the subscription permits it.

        A 403 here means the tier, not the query, so it is reported as exactly
        that rather than as no results.
        """
        if not credentials.extras.get("resdex_enabled"):
            raise BoardPermanentFailure(
                "This Naukri account does not have Resdex (resume database) enabled, so "
                "nothing was searched. It is a separate subscription — an empty result "
                "here would read as 'no matching candidates', which is not what "
                "happened."
            )
        code, body = await self._transport.request(
            "POST", "/v1/resdex/search",
            json={"query": query, "limit": limit}, headers=self._headers(credentials),
        )
        self._check(code, body)
        return [self._to_application(row, external_job_id="") for row in body.get("results", [])]


def _decimal(value: Any) -> Decimal | None:
    if value in (None, "", "null"):
        return None
    try:
        return Decimal(str(value))
    except (ValueError, ArithmeticError):
        return None


_NOTICE = re.compile(r"(\d+)")


def _notice_days(value: Any) -> int | None:
    """Naukri sends notice period as free text: "30 Days", "2 Months", "Immediate".

    Parsed rather than stored raw, because offer-drop rate by notice period
    (slice 36) needs a number. Anything unparseable stays `None` — a wrong
    number in a report is worse than a gap in one.
    """
    if value in (None, ""):
        return None
    text = str(value).strip().lower()
    if "immediate" in text:
        return 0
    match = _NOTICE.search(text)
    if not match:
        return None
    number = int(match.group(1))
    if "month" in text:
        return number * 30
    if "day" in text or "week" not in text:
        return number
    return number * 7

