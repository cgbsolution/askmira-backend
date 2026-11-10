"""The Indeed adapter.

Indeed lists a direct employer's jobs by crawling an XML feed the employer
publishes; there is no "post this job" call for us to make. So this adapter
sends nothing anywhere:

- `publish_job` puts the job in the workspace's feed (a posting that is LIVE on
  this board is in the feed; nothing else is),
- `update_job` changes what the feed shows next time Indeed reads it,
- `close_job` takes it out of the feed.

The feed is served at the careers site's public address
(`/public/careers/{slug}/feeds/indeed.xml`), and the employer registers that
address with Indeed once. Candidates who find the job on Indeed apply on the
careers page, so their applications arrive as careers-page applications — there
is nothing to poll and no résumé to fetch, and those two methods say so rather
than returning an empty list that would look like "no applicants".

Publishing is therefore not synchronous: Indeed picks the job up on its own
schedule, and the posting's warning says exactly that.
"""

from __future__ import annotations

from datetime import UTC, datetime
from xml.sax.saxutils import escape as xml_escape

from app.services.jobboards.base import (
    BoardCredentials,
    BoardPermanentFailure,
    JobBoardAdapter,
    JobSpec,
    PublishResult,
    RawApplication,
    Transport,
)

__all__ = ["IndeedAdapter", "feed_entry", "render_feed"]

BOARD = "indeed"

#: Indeed's crawl interval for a standard feed. Quoted in the warning rather
#: than promised — it is Indeed's schedule, not ours.
TYPICAL_CRAWL_HOURS = 24

#: Our employment types in Indeed's jobtype words.
_JOB_TYPES = {
    "full_time": "fulltime",
    "part_time": "parttime",
    "contract": "contract",
    "temporary": "temporary",
    "intern": "internship",
    "internship": "internship",
}


def _cdata(value: str) -> str:
    """A CDATA section that survives a value containing `]]>`."""
    return "<![CDATA[" + (value or "").replace("]]>", "]]]]><![CDATA[>") + "]]>"


def _rfc822(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%a, %d %b %Y %H:%M:%S GMT")


def feed_entry(
    spec: JobSpec, *, reference: str, company: str = "", posted_at: datetime | None = None
) -> str:
    """One `<job>` element of the XML feed.

    Every value is escaped or wrapped in CDATA. A job description containing an
    ampersand — which is most of them, sooner or later — would otherwise produce a
    feed Indeed rejects wholesale, taking every other job in it down too.
    """
    job_type = _JOB_TYPES.get(spec.employment_type, spec.employment_type.replace("_", ""))
    experience = ""
    if spec.experience_min_years is not None and spec.experience_max_years is not None:
        experience = f"{spec.experience_min_years}-{spec.experience_max_years} years"
    elif spec.experience_min_years is not None:
        experience = f"{spec.experience_min_years}+ years"
    parts = [
        "<job>",
        f"<title>{_cdata(spec.title)}</title>",
        f"<date>{xml_escape(_rfc822(posted_at or datetime.now(UTC)))}</date>",
        f"<referencenumber>{xml_escape(reference)}</referencenumber>",
        f"<requisitionid>{xml_escape(spec.requisition_code)}</requisitionid>",
        f"<url>{xml_escape(spec.apply_url or '')}</url>",
    ]
    if company:
        parts.append(f"<company>{_cdata(company)}</company>")
    parts += [
        f"<city>{_cdata(spec.city or spec.location)}</city>",
        f"<location>{_cdata(spec.location)}</location>",
        f"<country>{_cdata('IN')}</country>",
        f"<description>{_cdata(spec.description_html)}</description>",
        f"<jobtype>{_cdata(job_type)}</jobtype>",
    ]
    if spec.state_code:
        parts.append(f"<state>{_cdata(spec.state_code)}</state>")
    if spec.department:
        parts.append(f"<category>{_cdata(spec.department)}</category>")
    if experience:
        parts.append(f"<experience>{_cdata(experience)}</experience>")
    if spec.show_salary and spec.ctc_min is not None and spec.ctc_max is not None:
        parts.append(f"<salary>{_cdata(f'INR {spec.ctc_min} - {spec.ctc_max} per year')}</salary>")
    parts.append("</job>")
    return "".join(parts)


def render_feed(
    jobs: list[tuple[JobSpec, datetime | None]],
    *,
    publisher: str,
    publisher_url: str,
    now: datetime | None = None,
) -> str:
    """The whole feed: the workspace as publisher, then one `<job>` per live posting."""
    body = "".join(
        feed_entry(spec, reference=spec.requisition_code, company=publisher, posted_at=posted)
        for spec, posted in jobs
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        "<source>"
        f"<publisher>{_cdata(publisher)}</publisher>"
        f"<publisherurl>{_cdata(publisher_url)}</publisherurl>"
        f"<lastBuildDate>{xml_escape(_rfc822(now or datetime.now(UTC)))}</lastBuildDate>"
        f"{body}"
        "</source>"
    )


class IndeedAdapter(JobBoardAdapter):
    board = BOARD
    rate_limit_per_minute = 30

    #: Indeed re-crawls the feed on its own schedule; there is nothing for
    #: us to boost.
    default_refresh_interval_days = None

    LABEL = "Indeed"
    #: Nothing is sent to Indeed, so no login is stored for it.
    NEEDS_SECRET = False
    #: This board reads a feed from us; `render_feed` builds it.
    FEED = True
    CONNECTION_NOTE = (
        "Indeed lists your jobs by reading your job feed. Add Indeed here (no password "
        "needed), post jobs to it, then send the feed link below to Indeed once. "
        "Applicants apply on your careers page."
    )

    def __init__(self, transport: Transport | None = None) -> None:
        # Kept for the common constructor; the feed needs no network.
        self._transport = transport

    @staticmethod
    def render_feed(
        jobs: list[tuple[JobSpec, datetime | None]], *, publisher: str, publisher_url: str
    ) -> str:
        return render_feed(jobs, publisher=publisher, publisher_url=publisher_url)

    async def publish_job(self, spec: JobSpec, credentials: BoardCredentials) -> PublishResult:
        if not spec.apply_url:
            raise BoardPermanentFailure(
                "Indeed needs an address where candidates apply. Publish the careers page "
                "first; the feed points each job at its careers-page apply link."
            )
        return PublishResult(
            external_job_id=spec.requisition_code,
            published_at=datetime.now(UTC),
            accepted_as={"in_feed": True, "apply_url": spec.apply_url},
            warnings=(
                f"The job is in your Indeed feed. Indeed reads the feed on its own schedule, "
                f"typically within {TYPICAL_CRAWL_HOURS} hours, and only once the feed link "
                "is registered with Indeed. Nothing here can make it appear sooner.",
            ),
        )

    async def update_job(
        self, external_job_id: str, spec: JobSpec, credentials: BoardCredentials
    ) -> PublishResult:
        return PublishResult(
            external_job_id=external_job_id,
            published_at=datetime.now(UTC),
            accepted_as={"in_feed": True, "apply_url": spec.apply_url},
            warnings=(
                "The edit is in the feed. Indeed shows the previous version until it reads "
                "the feed again.",
            ),
        )

    async def close_job(
        self,
        external_job_id: str,
        credentials: BoardCredentials,
        *,
        spec: JobSpec | None = None,
    ) -> None:
        # A closed posting is no longer LIVE, so the feed leaves it out. Indeed
        # removes the job the next time it reads the feed.
        return None

    async def fetch_applications(
        self,
        external_job_id: str,
        credentials: BoardCredentials,
        *,
        since: datetime | None = None,
    ) -> list[RawApplication]:
        raise BoardPermanentFailure(
            "Candidates who find this job on Indeed apply on your careers page, so their "
            "applications are already under Applications. Nothing was fetched from Indeed — "
            "this is not an empty result."
        )

    async def fetch_resume(
        self, application: RawApplication, credentials: BoardCredentials
    ) -> bytes:
        raise BoardPermanentFailure(
            "Indeed applicants upload their résumé on your careers page; there is nothing to "
            "fetch from Indeed."
        )
