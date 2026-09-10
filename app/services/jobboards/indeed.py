"""The Indeed adapter.

Indeed's primary distribution route is an XML feed it crawls, not a per-job API
call, and the practical consequence is that publishing is not synchronous:
`publish_job` puts the job in the feed and Indeed picks it up on its own
schedule. The `PublishResult` says so in its warnings, because "published" that
actually means "queued for a crawler" is the kind of half-truth a recruiter
discovers by refreshing Indeed for two days.

═══════════════════════════════════════════════════════════════════════════════
NOT TESTED AGAINST A REAL INDEED ACCOUNT.
Written against the documented feed and Disposition API shapes and exercised
only against a transport double. A live employer account is a standing human
gate.
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from xml.sax.saxutils import escape as xml_escape

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

__all__ = ["IndeedAdapter", "feed_entry"]

BOARD = "indeed"

#: Indeed's crawl interval for a standard feed. Quoted in the warning rather
#: than promised — it is Indeed's schedule, not ours.
TYPICAL_CRAWL_HOURS = 24


def feed_entry(spec: JobSpec, *, reference: str) -> str:
    """One `<job>` element of the XML feed.

    Every value is XML-escaped. A job description containing an ampersand — which
    is most of them, sooner or later — would otherwise produce a feed Indeed
    rejects wholesale, taking every other job in it down too.
    """
    parts = [
        "<job>",
        f"<title><![CDATA[{spec.title}]]></title>",
        f"<date>{xml_escape(datetime.now(UTC).strftime('%a, %d %b %Y %H:%M:%S GMT'))}</date>",
        f"<referencenumber>{xml_escape(reference)}</referencenumber>",
        f"<url>{xml_escape(spec.apply_url or '')}</url>",
        f"<location><![CDATA[{spec.location}]]></location>",
        f"<description><![CDATA[{spec.description_html}]]></description>",
        f"<jobtype>{xml_escape(spec.employment_type.replace('_', ''))}</jobtype>",
    ]
    if spec.department:
        parts.append(f"<category><![CDATA[{spec.department}]]></category>")
    if spec.show_salary and spec.ctc_min is not None and spec.ctc_max is not None:
        parts.append(f"<salary><![CDATA[INR {spec.ctc_min} - {spec.ctc_max} per year]]></salary>")
    parts.append("</job>")
    return "".join(parts)


class IndeedAdapter(JobBoardAdapter):
    board = BOARD
    rate_limit_per_minute = 30

    #: Indeed re-crawls the feed on its own schedule; there is nothing for
    #: us to boost.
    default_refresh_interval_days = None

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    def _headers(self, credentials: BoardCredentials) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {credentials.secret}",
            "Content-Type": "application/json",
        }

    def _check(self, code: int, body: dict[str, Any]) -> None:
        if 200 <= code < 300:
            return
        message = str(body.get("message") or f"HTTP {code}")
        if code == 429:
            raise RateLimited(f"Indeed rate limit: {message}")
        if code in (400, 401, 403, 404, 422):
            raise BoardPermanentFailure(f"Indeed rejected this: {message}")
        raise BoardTransientFailure(f"Indeed returned {code}: {message}")

    async def publish_job(
        self, spec: JobSpec, credentials: BoardCredentials
    ) -> PublishResult:
        code, body = await self._transport.request(
            "POST", "/feed/jobs",
            json={
                "employerId": credentials.username,
                "job": feed_entry(spec, reference=spec.requisition_code),
            },
            headers=self._headers(credentials),
        )
        self._check(code, body)
        return PublishResult(
            external_job_id=str(body.get("jobKey") or spec.requisition_code),
            published_at=datetime.now(UTC),
            warnings=(
                f"Indeed crawls the feed on its own schedule, typically within "
                f"{TYPICAL_CRAWL_HOURS} hours. This job is in the feed; it is not yet "
                "visible on Indeed, and nothing here can make it appear sooner.",
            ),
        )

    async def update_job(
        self, external_job_id: str, spec: JobSpec, credentials: BoardCredentials
    ) -> PublishResult:
        code, body = await self._transport.request(
            "PUT", f"/feed/jobs/{external_job_id}",
            json={"job": feed_entry(spec, reference=spec.requisition_code)},
            headers=self._headers(credentials),
        )
        self._check(code, body)
        return PublishResult(
            external_job_id=external_job_id,
            published_at=datetime.now(UTC),
            warnings=(
                "The edit is in the feed. Indeed shows the previous version until it "
                "crawls again.",
            ),
        )

    async def close_job(self, external_job_id: str, credentials: BoardCredentials) -> None:
        code, body = await self._transport.request(
            "DELETE", f"/feed/jobs/{external_job_id}", headers=self._headers(credentials)
        )
        self._check(code, body)

    async def fetch_applications(
        self,
        external_job_id: str,
        credentials: BoardCredentials,
        *,
        since: datetime | None = None,
    ) -> list[RawApplication]:
        params = f"&since={since.isoformat()}" if since else ""
        code, body = await self._transport.request(
            "GET", f"/apply/applications?jobKey={external_job_id}{params}",
            headers=self._headers(credentials),
        )
        self._check(code, body)
        return [
            RawApplication(
                external_application_id=str(row.get("id") or ""),
                board=BOARD,
                external_job_id=external_job_id,
                full_name=str(row.get("name") or "").strip(),
                email=str(row.get("email") or "").strip(),
                phone=row.get("phone"),
                resume_url=(row.get("resume") or {}).get("url"),
                applied_at=(
                    datetime.fromtimestamp(row["appliedOn"] / 1000, tz=UTC)
                    if row.get("appliedOn")
                    else None
                ),
                raw=row,
            )
            for row in body.get("applications", [])
        ]

    async def fetch_resume(
        self, application: RawApplication, credentials: BoardCredentials
    ) -> bytes:
        if not application.resume_url:
            raise BoardPermanentFailure(
                "Indeed sent no resume for this applicant. Indeed Apply lets a "
                "candidate submit a profile without one."
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
        raise BoardTransientFailure("Indeed returned an empty resume body.")

