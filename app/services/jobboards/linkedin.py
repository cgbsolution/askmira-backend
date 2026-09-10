"""The LinkedIn adapter.

LinkedIn's job posting API works on a *push* model with no polling endpoint for
applications on the basic tier: applications arrive by webhook, or through
LinkedIn's own inbox, and there is no "give me everything since Tuesday" call.
`fetch_applications` says so rather than returning an empty list — an empty list
is indistinguishable from a quiet week, and a recruiter would conclude nobody
had applied.

═══════════════════════════════════════════════════════════════════════════════
NOT TESTED AGAINST A REAL LINKEDIN ACCOUNT.
Written against the documented Job Posting API shape and exercised only against
a transport double. A live partner account is a standing human gate.
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from datetime import UTC, datetime
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

__all__ = ["LinkedInAdapter"]

BOARD = "linkedin"

#: LinkedIn's own employment-type vocabulary.
EMPLOYMENT_TYPES = {
    "full_time": "FULL_TIME",
    "part_time": "PART_TIME",
    "contract": "CONTRACT",
    "intern": "INTERNSHIP",
}

#: LinkedIn's workplace types. Nothing on our requisition distinguishes these
#: yet, so every posting goes out as on-site and the warning says so — a remote
#: role listed as on-site gets the wrong applicants, not fewer of them.
DEFAULT_WORKPLACE_TYPE = "On-site"


class LinkedInAdapter(JobBoardAdapter):
    board = BOARD
    rate_limit_per_minute = 60

    #: LinkedIn has no boost operation, so there is nothing to schedule.
    #: None rather than a number: a refresh timer against a board that
    #: cannot refresh would fail forever on a schedule.
    default_refresh_interval_days = None

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    def _headers(self, credentials: BoardCredentials) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {credentials.secret}",
            "X-Restli-Protocol-Version": "2.0.0",
            "Content-Type": "application/json",
        }

    def _check(self, code: int, body: dict[str, Any]) -> None:
        if 200 <= code < 300:
            return
        message = str(body.get("message") or f"HTTP {code}")
        if code == 429:
            raise RateLimited(f"LinkedIn rate limit: {message}")
        if code in (400, 401, 403, 404, 422):
            raise BoardPermanentFailure(f"LinkedIn rejected this: {message}")
        raise BoardTransientFailure(f"LinkedIn returned {code}: {message}")

    def build_payload(self, spec: JobSpec, credentials: BoardCredentials) -> tuple[dict, list[str]]:
        warnings: list[str] = []
        organisation = credentials.extras.get("organisation_urn")
        if not organisation:
            raise BoardPermanentFailure(
                "This LinkedIn account has no organisation URN configured. LinkedIn "
                "posts jobs on behalf of a company page, not a person, so there is "
                "nowhere for this to go."
            )

        warnings.append(
            f"Posted as {DEFAULT_WORKPLACE_TYPE}. Nothing on the requisition says "
            "whether this role is remote or hybrid, and LinkedIn requires a value — "
            "correct it on LinkedIn if this role is not on-site."
        )

        payload = {
            "integrationContext": organisation,
            "companyApplyUrl": spec.apply_url,
            "description": {"text": spec.description_html},
            "employmentStatus": EMPLOYMENT_TYPES.get(spec.employment_type, "FULL_TIME"),
            "externalJobPostingId": spec.requisition_code,
            "listedAt": int(datetime.now(UTC).timestamp() * 1000),
            "jobPostingOperationType": "CREATE",
            "title": spec.title,
            "location": spec.location,
            "workplaceTypes": [DEFAULT_WORKPLACE_TYPE],
            "availability": "PUBLIC",
        }
        return payload, warnings

    async def publish_job(
        self, spec: JobSpec, credentials: BoardCredentials
    ) -> PublishResult:
        payload, warnings = self.build_payload(spec, credentials)
        code, body = await self._transport.request(
            "POST", "/v2/simpleJobPostings", json=payload, headers=self._headers(credentials)
        )
        self._check(code, body)
        external_id = str(body.get("id") or "")
        if not external_id:
            raise BoardPermanentFailure(
                "LinkedIn accepted the posting but returned no id, so nothing here can "
                "update or close it."
            )
        return PublishResult(
            external_job_id=external_id,
            published_at=datetime.now(UTC),
            accepted_as={"workplaceType": DEFAULT_WORKPLACE_TYPE},
            warnings=tuple(warnings),
        )

    async def update_job(
        self, external_job_id: str, spec: JobSpec, credentials: BoardCredentials
    ) -> PublishResult:
        payload, warnings = self.build_payload(spec, credentials)
        payload["jobPostingOperationType"] = "UPDATE"
        code, body = await self._transport.request(
            "POST", f"/v2/simpleJobPostings/{external_job_id}",
            json=payload, headers=self._headers(credentials),
        )
        self._check(code, body)
        return PublishResult(
            external_job_id=external_job_id,
            published_at=datetime.now(UTC),
            warnings=tuple(warnings),
        )

    async def close_job(self, external_job_id: str, credentials: BoardCredentials) -> None:
        code, body = await self._transport.request(
            "DELETE", f"/v2/simpleJobPostings/{external_job_id}",
            headers=self._headers(credentials),
        )
        self._check(code, body)

    async def fetch_applications(
        self,
        external_job_id: str,
        credentials: BoardCredentials,
        *,
        since: datetime | None = None,
    ) -> list[RawApplication]:
        """LinkedIn has no polling endpoint on the standard tier.

        Refusing loudly rather than returning `[]`: an empty list here would be
        read as a quiet week, and the applications would sit unseen in
        LinkedIn's own inbox for as long as it took somebody to check.
        """
        raise BoardPermanentFailure(
            "LinkedIn does not support polling for applications on this tier. They "
            "arrive by webhook, or they sit in LinkedIn's inbox. Nothing was fetched — "
            "this is not an empty result."
        )

    async def fetch_resume(
        self, application: RawApplication, credentials: BoardCredentials
    ) -> bytes:
        if not application.resume_url:
            raise BoardPermanentFailure("LinkedIn sent no resume for this applicant.")
        code, body = await self._transport.request(
            "GET", application.resume_url, headers=self._headers(credentials)
        )
        self._check(code, body)
        content = body.get("content")
        if isinstance(content, bytes):
            return content
        if isinstance(content, str):
            return content.encode("utf-8")
        raise BoardTransientFailure("LinkedIn returned an empty resume body.")

    def parse_webhook(self, event: dict[str, Any]) -> RawApplication:
        """One LinkedIn application webhook into our shape.

        This is how applications actually arrive from LinkedIn, which is why it
        exists alongside a `fetch_applications` that refuses.
        """
        applicant = event.get("applicant", {})
        return RawApplication(
            external_application_id=str(event.get("applicationId") or ""),
            board=BOARD,
            external_job_id=str(event.get("jobPostingId") or ""),
            full_name=f"{applicant.get('firstName', '')} {applicant.get('lastName', '')}".strip(),
            email=str(applicant.get("emailAddress") or "").strip(),
            phone=applicant.get("phoneNumber"),
            resume_url=event.get("resumeUrl"),
            raw=event,
        )
