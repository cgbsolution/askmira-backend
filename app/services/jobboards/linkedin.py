"""The LinkedIn adapter — LinkedIn's Job Posting API (`/v2/simpleJobPostings`).

Written to LinkedIn's documented contract (learn.microsoft.com/linkedin/talent/
job-postings/api, checked 2026-11-09):

- **Sign-in is the client-credentials grant.** The partner app's client id and
  secret are exchanged at `www.linkedin.com/oauth/v2/accessToken` for a token
  that lives 30 minutes; LinkedIn asks partners to reuse it until it expires, so
  it is cached per client id. A pasted token would stop working half an hour
  after it was pasted.
- **Every operation is a `batch_create`** on the same endpoint, with the job in
  an `elements` array and `x-restli-method: batch_create`: CREATE to post,
  UPDATE to change, CLOSE to take it down (re-posting the job with that
  operation). There is no DELETE.
- **The job is ours to name.** LinkedIn keys a job by our `externalJobPostingId`
  (the requisition code); the call returns a *task* id, valid for a day, which
  is kept for reference.
- **A refusal comes back as HTTP 200** with the error inside the element, so
  each element's own `status` is read before anything is believed.

Applications are pushed (Apply Connect) or stay in LinkedIn's inbox; there is no
polling endpoint, and `fetch_applications` says so rather than returning `[]`.

Exercised against a local stand-in of the documented API only: a partner
account (LinkedIn approves these) is needed for a live run.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
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

TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
POSTINGS_PATH = "/v2/simpleJobPostings"
#: LinkedIn's cap on a description, HTML included.
DESCRIPTION_LIMIT = 25000

#: LinkedIn's own employment-type vocabulary.
EMPLOYMENT_TYPES = {
    "full_time": "FULL_TIME",
    "part_time": "PART_TIME",
    "contract": "CONTRACT",
    "intern": "INTERNSHIP",
    "internship": "INTERNSHIP",
    "temporary": "TEMPORARY",
}

#: client id → (access token, monotonic time it stops being reused).
_TOKENS: dict[str, tuple[str, float]] = {}


class LinkedInAdapter(JobBoardAdapter):
    board = BOARD
    rate_limit_per_minute = 60
    BASE_URL = "https://api.linkedin.com"
    #: The API, the OAuth server (www.linkedin.com) and the media host résumé links use.
    API_HOSTS = ("api.linkedin.com", "linkedin.com", "licdn.com")
    LABEL = "LinkedIn"
    CONNECTION_NOTE = (
        "Posts through LinkedIn's Job Posting API, which LinkedIn opens to approved partners "
        "only. Username: your LinkedIn app's client id. Secret: its client secret. Settings: "
        "organisation_urn=urn:li:organization:… (your company page). Without partner "
        "access LinkedIn refuses the post and the posting shows LinkedIn's reason."
    )

    #: LinkedIn has no boost operation, so there is nothing to schedule.
    default_refresh_interval_days = None

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    # ── Sign-in ─────────────────────────────────────────────────────────────

    async def _token(self, credentials: BoardCredentials) -> str:
        client_id = (credentials.username or "").strip()
        if not client_id or not credentials.secret:
            raise BoardPermanentFailure(
                "LinkedIn needs the partner app's client id (Username) and client secret "
                "(Secret). Nothing was sent."
            )
        cached = _TOKENS.get(client_id)
        if cached and cached[1] > time.monotonic():
            return cached[0]
        code, body = await self._transport.request(
            "POST",
            TOKEN_URL,
            form={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": credentials.secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        token = body.get("access_token") if 200 <= code < 300 else None
        if not token:
            reason = body.get("error_description") or body.get("error") or f"HTTP {code}"
            if code >= 500:
                raise BoardTransientFailure(f"LinkedIn's sign-in failed ({reason}); will retry.")
            raise BoardPermanentFailure(
                f"LinkedIn did not accept the app's client id and secret ({reason})."
            )
        lifetime = float(body.get("expires_in") or 1800)
        # Reuse it, as LinkedIn asks, until a minute before it lapses.
        _TOKENS[client_id] = (str(token), time.monotonic() + max(lifetime - 60, 30))
        return str(token)

    async def _headers(self, credentials: BoardCredentials) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {await self._token(credentials)}",
            "x-restli-method": "batch_create",
            "Content-Type": "application/json",
        }

    # ── The job ─────────────────────────────────────────────────────────────

    def build_payload(
        self, spec: JobSpec, credentials: BoardCredentials, *, operation: str = "CREATE"
    ) -> tuple[dict[str, Any], list[str]]:
        """One element of a batch_create, and everything worth saying about it."""
        warnings: list[str] = []
        organisation = str(credentials.extras.get("organisation_urn") or "").strip()
        if not organisation:
            raise BoardPermanentFailure(
                "This LinkedIn account has no organisation URN configured. LinkedIn "
                "posts jobs on behalf of a company page, not a person, so there is "
                "nowhere for this to go."
            )
        if not spec.apply_url:
            raise BoardPermanentFailure(
                "LinkedIn needs the address where candidates apply. Publish the careers page "
                "or give an apply link."
            )
        description = spec.description_html or spec.title
        if len(description) > DESCRIPTION_LIMIT:
            description = description[:DESCRIPTION_LIMIT]
            warnings.append(
                f"The description was cut to LinkedIn's limit of {DESCRIPTION_LIMIT} characters."
            )
        warnings.append(
            "Posted as on-site: nothing on the requisition says whether this role is remote "
            "or hybrid. Correct it on LinkedIn if it is not on-site."
        )
        element: dict[str, Any] = {
            "integrationContext": organisation,
            "companyApplyUrl": spec.apply_url,
            "description": description,
            "employmentStatus": EMPLOYMENT_TYPES.get(spec.employment_type, "FULL_TIME"),
            "externalJobPostingId": spec.requisition_code,
            # Milliseconds, and never in the future: LinkedIn refuses a future listedAt.
            "listedAt": int((datetime.now(UTC) - timedelta(seconds=60)).timestamp() * 1000),
            "jobPostingOperationType": operation,
            "title": spec.title,
            "location": spec.city or spec.location or "India",
            "listingType": "BASIC",
        }
        return element, warnings

    @staticmethod
    def _element(code: int, body: dict[str, Any]) -> dict[str, Any]:
        """The one element LinkedIn answered for, or the typed failure it is."""
        if code == 429:
            raise RateLimited(f"LinkedIn rate limit: {body.get('message') or 'too many calls'}")
        if code in (401, 403):
            raise BoardPermanentFailure(
                f"LinkedIn refused this: {body.get('message') or f'HTTP {code}'}. Usually the "
                "app has no Job Posting API access, or the header contract changed."
            )
        if code >= 500:
            raise BoardTransientFailure(f"LinkedIn returned {code}: {body.get('message') or ''}")
        if code >= 400:
            raise BoardPermanentFailure(f"LinkedIn rejected this: {body.get('message') or code}")
        elements = body.get("elements") or []
        if not elements or not isinstance(elements[0], dict):
            raise BoardTransientFailure(
                "LinkedIn answered without a result for the job, so nothing is known about it."
            )
        element = elements[0]
        status = int(element.get("status") or 0)
        error = element.get("error") or {}
        if error or status >= 400:
            message = str(error.get("message") or f"status {status}").strip()
            if status == 429:
                raise RateLimited(f"LinkedIn rate limit: {message}")
            if status >= 500:
                raise BoardTransientFailure(f"LinkedIn could not take the job: {message}")
            raise BoardPermanentFailure(f"LinkedIn rejected the job: {message}")
        return element

    async def _submit(self, element: dict[str, Any], credentials: BoardCredentials) -> dict:
        code, body = await self._transport.request(
            "POST",
            POSTINGS_PATH,
            json={"elements": [element]},
            headers=await self._headers(credentials),
        )
        return self._element(code, body)

    async def publish_job(self, spec: JobSpec, credentials: BoardCredentials) -> PublishResult:
        element, warnings = self.build_payload(spec, credentials)
        answer = await self._submit(element, credentials)
        return PublishResult(
            # LinkedIn keys the job by our externalJobPostingId; the task id is a receipt.
            external_job_id=spec.requisition_code,
            published_at=datetime.now(UTC),
            accepted_as={"task": answer.get("id"), "apply_url": spec.apply_url},
            warnings=(
                *warnings,
                "LinkedIn processes a posting as a task: it appears once LinkedIn has run it, "
                "usually within minutes.",
            ),
        )

    async def update_job(
        self, external_job_id: str, spec: JobSpec, credentials: BoardCredentials
    ) -> PublishResult:
        element, warnings = self.build_payload(spec, credentials, operation="UPDATE")
        element["externalJobPostingId"] = external_job_id
        answer = await self._submit(element, credentials)
        return PublishResult(
            external_job_id=external_job_id,
            published_at=datetime.now(UTC),
            accepted_as={"task": answer.get("id")},
            warnings=tuple(warnings),
        )

    async def close_job(
        self,
        external_job_id: str,
        credentials: BoardCredentials,
        *,
        spec: JobSpec | None = None,
    ) -> None:
        """Re-post the job with CLOSE, as LinkedIn documents. With the job's details when known."""
        if spec is not None:
            element, _ = self.build_payload(spec, credentials, operation="CLOSE")
        else:
            organisation = str(credentials.extras.get("organisation_urn") or "").strip()
            element = {"integrationContext": organisation, "jobPostingOperationType": "CLOSE"}
        element["externalJobPostingId"] = external_job_id
        try:
            await self._submit(element, credentials)
        except BoardPermanentFailure as exc:
            # LinkedIn answers 409 for a job already closed or never there: either
            # way it is not live, which is what closing was for.
            if "already closed" in str(exc).lower() or "does not exist" in str(exc).lower():
                return
            raise

    async def fetch_applications(
        self,
        external_job_id: str,
        credentials: BoardCredentials,
        *,
        since: datetime | None = None,
    ) -> list[RawApplication]:
        """LinkedIn has no polling endpoint.

        Refusing loudly rather than returning `[]`: an empty list here would be
        read as a quiet week, and the applications would sit unseen in
        LinkedIn's own inbox for as long as it took somebody to check.
        """
        raise BoardPermanentFailure(
            "LinkedIn does not support polling for applications. They arrive through "
            "LinkedIn Apply Connect, or they sit in LinkedIn's inbox. Nothing was fetched — "
            "this is not an empty result."
        )

    async def fetch_resume(
        self, application: RawApplication, credentials: BoardCredentials
    ) -> bytes:
        if not application.resume_url:
            raise BoardPermanentFailure("LinkedIn sent no resume for this applicant.")
        headers = {"Authorization": f"Bearer {await self._token(credentials)}"}
        code, body = await self._transport.request("GET", application.resume_url, headers=headers)
        if code >= 400:
            self._element(code, body)
        content = body.get("content")
        if isinstance(content, bytes):
            return content
        if isinstance(content, str):
            return content.encode("utf-8")
        raise BoardTransientFailure("LinkedIn returned an empty resume body.")

    def parse_webhook(self, event: dict[str, Any]) -> RawApplication:
        """One LinkedIn application webhook into our shape."""
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
