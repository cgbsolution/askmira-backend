"""Slice 20 gates that need a real database.

The adapters and the engines are proved in `tests/hiring/`. What only a database
settles: that a repeat applicant really does link to the existing row with their
history intact, that a rate-limited posting stays visible as a row instead of
vanishing, that the careers page renders indexable markup, and that an erasure
request actually removes the personal data while leaving the statistics alone.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_secret
from app.models.culture import Club, Shoutout, ShoutoutVisibility
from app.models.employee import Employee
from app.models.hiring import Candidate, Requisition, RequisitionStatus
from app.models.jobboard import (
    Application,
    ApplicationStatus,
    CandidateConsent,
    CandidateDocument,
    CareersPageConfig,
    ConsentPurpose,
    JobBoard,
    JobBoardAccount,
    JobPosting,
    PostingStatus,
)
from app.models.org import LegalEntity, Location
from app.models.tenant import Tenant
from app.models.user import User
from app.services.hiring import careers as careers_service
from app.services.hiring import ingestion
from app.services.jobboards import backoff, distribution, registry
from app.services.jobboards.base import (
    BoardPermanentFailure,
    JobBoardAdapter,
    PublishResult,
    RateLimited,
    RawApplication,
)
from tests.hiring.db_fixture import engine  # noqa: F401 - shadows the shared fixture
from tests.integration.conftest import requires_db

pytestmark = requires_db

NOW = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)


# ═══════════════════════════════════════════════════════════════════════════
# Doubles
# ═══════════════════════════════════════════════════════════════════════════


class ScriptedAdapter(JobBoardAdapter):
    """An adapter that does exactly what the test tells it to.

    Registered through the same registry the real ones use, which is what makes
    these tests exercise the actual dispatch path rather than a parallel one.
    """

    board = "naukri"
    rate_limit_per_minute = 20

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.raises = raises
        self.published: list = []
        self.refreshed: list[str] = []
        self.closed: list[str] = []

    async def publish_job(self, spec, credentials):
        if self.raises:
            raise self.raises
        self.published.append(spec)
        return PublishResult(
            external_job_id="NKR-1",
            published_at=NOW,
            accepted_as={"functionalArea": "IT Software - Application Programming"},
            warnings=("Salary is not shown on this posting.",),
        )

    async def update_job(self, external_job_id, spec, credentials):
        return PublishResult(external_job_id=external_job_id, published_at=NOW)

    async def close_job(self, external_job_id, credentials):
        if self.raises:
            raise self.raises
        self.closed.append(external_job_id)

    async def refresh_job(self, external_job_id, credentials):
        if self.raises:
            raise self.raises
        self.refreshed.append(external_job_id)
        return NOW

    async def fetch_applications(self, external_job_id, credentials, *, since=None):
        return []

    async def fetch_resume(self, application, credentials):
        return b""


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    registry._OVERRIDES.clear()


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════


async def _account(session: AsyncSession, tenant: Tenant) -> JobBoardAccount:
    account = JobBoardAccount(
        tenant_id=tenant.id,
        board=JobBoard.NAUKRI,
        username="acme-recruiter",
        credentials_encrypted=encrypt_secret("naukri-token", tenant.id),
        settings={"recruiter_id": "R1"},
    )
    session.add(account)
    await session.commit()
    return account


async def _requisition(
    session: AsyncSession, tenant: Tenant, *, code: str = "REQ-001",
    status: RequisitionStatus = RequisitionStatus.OPEN, location_id=None,
) -> Requisition:
    requisition = Requisition(
        tenant_id=tenant.id, code=code, title="Senior Backend Engineer",
        openings=2, grade="L5", status=status, location_id=location_id,
        description="<p>Build things.</p>", skills=["Python", "PostgreSQL"],
        justification="Team growth.", experience_min_years=4, experience_max_years=8,
        budget_ctc_min=Decimal("1500000"), budget_ctc_max=Decimal("2500000"),
    )
    session.add(requisition)
    await session.commit()
    await session.refresh(requisition)
    return requisition


async def _careers_config(
    session: AsyncSession, tenant: Tenant, *, slug: str = "acme", retention_days: int = 730,
) -> CareersPageConfig:
    config = CareersPageConfig(
        tenant_id=tenant.id, slug=slug, is_published=True,
        hero_heading="Build with us",
        privacy_notice_html="<p>We keep your application for two years.</p>",
        privacy_notice_version="2026-01",
        resume_retention_days=retention_days,
    )
    session.add(config)
    await session.commit()
    await session.refresh(config)
    return config


def _raw(**overrides) -> RawApplication:
    base = {
        "external_application_id": "NKR-A1",
        "board": "naukri",
        "external_job_id": "NKR-1",
        "full_name": "Rohit Verma",
        "email": "rohit@example.test",
        "phone": "+91 98765 43210",
        "current_company": "Acme Labs",
        "current_ctc": Decimal("1400000"),
        "notice_period_days": 90,
        "applied_at": NOW,
    }
    base.update(overrides)
    return RawApplication(**base)


# ═══════════════════════════════════════════════════════════════════════════
# Distribution — a failure is a row, never an absence
# ═══════════════════════════════════════════════════════════════════════════


async def test_publishing_stores_the_board_id_and_the_adapters_warnings(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    await _account(session, tenant)
    requisition = await _requisition(session, tenant)
    registry.register_adapter(JobBoard.NAUKRI, ScriptedAdapter())

    result = await distribution.publish_posting(
        session, hr_user, requisition_id=requisition.id, board=JobBoard.NAUKRI, now=NOW
    )
    await session.commit()

    assert result["status"] == PostingStatus.LIVE.value
    assert result["external_job_id"] == "NKR-1"
    assert result["warnings"]
    posting = await session.get(JobPosting, result["posting_id"])
    assert posting.accepted_as["functionalArea"]
    # Naukri postings decay in listing position, so a boost is scheduled.
    assert posting.next_refresh_at == NOW + timedelta(days=7)


async def test_a_rate_limit_leaves_a_posting_row_with_a_retry_time(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """The checklist gate: a rate-limit failure must not silently drop a posting.

    The failure mode this prevents is not an exception — it is a job that is
    simply not on Naukri, discovered three weeks later when no applications came
    in.
    """
    await _account(session, tenant)
    requisition = await _requisition(session, tenant)
    registry.register_adapter(
        JobBoard.NAUKRI,
        ScriptedAdapter(raises=RateLimited("too many requests", retry_after_seconds=120)),
    )

    result = await distribution.publish_posting(
        session, hr_user, requisition_id=requisition.id, board=JobBoard.NAUKRI, now=NOW
    )
    await session.commit()

    assert result["rate_limited"] is True
    assert result["needs_a_human"] is False
    posting = await session.get(JobPosting, result["posting_id"])
    assert posting is not None, "the posting exists as a row rather than vanishing"
    assert posting.status is PostingStatus.PUBLISHING
    # The board's own Retry-After wins over our schedule.
    assert posting.next_attempt_at == NOW + timedelta(seconds=120)
    assert "too many requests" in posting.last_error


async def test_a_rate_limited_posting_comes_back_on_the_retry_sweep(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    await _account(session, tenant)
    requisition = await _requisition(session, tenant)
    registry.register_adapter(JobBoard.NAUKRI, ScriptedAdapter(raises=RateLimited("429")))
    await distribution.publish_posting(
        session, hr_user, requisition_id=requisition.id, board=JobBoard.NAUKRI, now=NOW
    )
    await session.commit()

    assert await distribution.retryable_postings(session, hr_user, as_of=NOW) == []
    due = await distribution.retryable_postings(
        session, hr_user, as_of=NOW + timedelta(hours=1)
    )
    assert len(due) == 1


async def test_a_retry_after_the_last_attempt_parks_for_a_human(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """Parked, not abandoned. A posting that stopped being tried and was not
    parked is a posting nobody knows is missing."""
    await _account(session, tenant)
    requisition = await _requisition(session, tenant)
    registry.register_adapter(JobBoard.NAUKRI, ScriptedAdapter(raises=RateLimited("429")))

    for _ in range(backoff.MAX_ATTEMPTS):
        result = await distribution.publish_posting(
            session, hr_user, requisition_id=requisition.id, board=JobBoard.NAUKRI, now=NOW
        )
        await session.commit()

    assert result["needs_a_human"] is True
    posting = await session.get(JobPosting, result["posting_id"])
    assert posting.status is PostingStatus.PARKED
    assert posting.next_attempt_at is None
    # And it does not spin on the retry sweep forever.
    assert await distribution.retryable_postings(
        session, hr_user, as_of=NOW + timedelta(days=7)
    ) == []


async def test_a_rejected_field_value_parks_immediately_without_retrying(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """A value the board refuses does not become acceptable by being sent
    again."""
    await _account(session, tenant)
    requisition = await _requisition(session, tenant)
    registry.register_adapter(
        JobBoard.NAUKRI,
        ScriptedAdapter(raises=BoardPermanentFailure("unknown functional area")),
    )

    result = await distribution.publish_posting(
        session, hr_user, requisition_id=requisition.id, board=JobBoard.NAUKRI, now=NOW
    )
    await session.commit()

    assert result["needs_a_human"] is True
    posting = await session.get(JobPosting, result["posting_id"])
    assert posting.status is PostingStatus.PARKED
    assert posting.attempt_count == 1
    assert "unknown functional area" in posting.last_error


async def test_a_parked_posting_is_visible_where_somebody_is_looking(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    await _account(session, tenant)
    requisition = await _requisition(session, tenant)
    registry.register_adapter(
        JobBoard.NAUKRI, ScriptedAdapter(raises=BoardPermanentFailure("rejected"))
    )
    await distribution.publish_posting(
        session, hr_user, requisition_id=requisition.id, board=JobBoard.NAUKRI, now=NOW
    )
    await session.commit()

    parked = (
        await session.scalars(
            select(JobPosting).where(
                JobPosting.tenant_id == tenant.id, JobPosting.status == PostingStatus.PARKED
            )
        )
    ).all()
    assert len(parked) == 1
    assert parked[0].last_error


async def test_an_unapproved_requisition_cannot_be_advertised(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    await _account(session, tenant)
    requisition = await _requisition(
        session, tenant, status=RequisitionStatus.PENDING_APPROVAL
    )
    registry.register_adapter(JobBoard.NAUKRI, ScriptedAdapter())
    with pytest.raises(HTTPException) as exc:
        await distribution.publish_posting(
            session, hr_user, requisition_id=requisition.id, board=JobBoard.NAUKRI
        )
    assert "headcount it does not have" in exc.value.detail


async def test_a_second_publish_to_the_same_board_is_refused(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """Two live adverts split the applications and confuse candidates."""
    await _account(session, tenant)
    requisition = await _requisition(session, tenant)
    registry.register_adapter(JobBoard.NAUKRI, ScriptedAdapter())
    await distribution.publish_posting(
        session, hr_user, requisition_id=requisition.id, board=JobBoard.NAUKRI, now=NOW
    )
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await distribution.publish_posting(
            session, hr_user, requisition_id=requisition.id, board=JobBoard.NAUKRI
        )
    assert "already live" in exc.value.detail


async def test_refresh_moves_the_next_boost_forward(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    await _account(session, tenant)
    requisition = await _requisition(session, tenant)
    adapter = ScriptedAdapter()
    registry.register_adapter(JobBoard.NAUKRI, adapter)
    published = await distribution.publish_posting(
        session, hr_user, requisition_id=requisition.id, board=JobBoard.NAUKRI, now=NOW
    )
    await session.commit()

    later = NOW + timedelta(days=7)
    assert len(await distribution.due_refreshes(session, hr_user, as_of=later)) == 1

    await distribution.refresh_posting(
        session, hr_user, posting_id=published["posting_id"], now=later
    )
    await session.commit()

    assert adapter.refreshed == ["NKR-1"]
    posting = await session.get(JobPosting, published["posting_id"])
    assert posting.next_refresh_at == later + timedelta(days=7)


async def test_a_missing_board_account_stops_before_anything_is_sent(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    requisition = await _requisition(session, tenant)
    registry.register_adapter(JobBoard.NAUKRI, ScriptedAdapter())
    with pytest.raises(HTTPException) as exc:
        await distribution.publish_posting(
            session, hr_user, requisition_id=requisition.id, board=JobBoard.NAUKRI
        )
    assert "Nothing was posted" in exc.value.detail


async def test_credentials_decrypt_only_at_the_point_of_use(
    session: AsyncSession, tenant: Tenant
) -> None:
    account = await _account(session, tenant)
    assert account.credentials_encrypted != "naukri-token"
    credentials = registry.credentials_for(account, tenant.id)
    assert credentials.secret == "naukri-token"
    assert credentials.extras["recruiter_id"] == "R1"


# ═══════════════════════════════════════════════════════════════════════════
# Ingestion and the repeat applicant
# ═══════════════════════════════════════════════════════════════════════════


async def _live_posting(session, tenant, hr_user, *, code="REQ-001"):
    await _account(session, tenant)
    requisition = await _requisition(session, tenant, code=code)
    registry.register_adapter(JobBoard.NAUKRI, ScriptedAdapter())
    await distribution.publish_posting(
        session, hr_user, requisition_id=requisition.id, board=JobBoard.NAUKRI, now=NOW
    )
    await session.commit()
    return requisition


async def test_a_first_application_creates_a_candidate(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    requisition = await _live_posting(session, tenant, hr_user)
    await _careers_config(session, tenant)

    outcome = await ingestion.ingest_application(session, hr_user, _raw(), now=NOW)
    await session.commit()

    assert outcome.created_candidate
    assert outcome.candidate.full_name == "Rohit Verma"
    assert outcome.application.requisition_id == requisition.id
    assert outcome.application.matched_on == "new"
    # Retention computed at ingestion from the policy in force then.
    assert outcome.application.retention_until == NOW.date() + timedelta(days=730)


async def test_a_repeat_applicant_links_to_the_existing_candidate_with_history(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """The checklist gate, with a real duplicate.

    "Applied three times, rejected twice at final round" is the sentence this
    exists to make possible.
    """
    await _careers_config(session, tenant)
    await _live_posting(session, tenant, hr_user, code="REQ-001")

    first = await ingestion.ingest_application(session, hr_user, _raw(), now=NOW)
    await session.commit()

    # Second role, second board, same person — different email, same phone.
    second_requisition = await _requisition(session, tenant, code="REQ-002")
    second = await ingestion.ingest_application(
        session, hr_user,
        _raw(
            external_application_id="LI-1", board="linkedin", external_job_id="",
            email="rohit.verma@gmail.test", phone="09876543210",
            current_company="Newer Labs",
        ),
        requisition_id=second_requisition.id,
        now=NOW + timedelta(days=200),
    )
    await session.commit()

    assert not second.created_candidate
    assert second.candidate.id == first.candidate.id
    assert second.application.matched_on == "phone"

    candidates = await session.scalar(
        select(func.count()).select_from(Candidate).where(Candidate.tenant_id == tenant.id)
    )
    assert candidates == 1, "one person, one candidate row"

    history = await ingestion.candidate_history(
        session, hr_user, candidate_id=first.candidate.id
    )
    assert len(history) == 2
    assert [a.board for a in history] == [JobBoard.NAUKRI, JobBoard.LINKEDIN]

    # Newer facts win; nothing is wiped by a blank from another board.
    await session.refresh(second.candidate)
    assert second.candidate.current_company == "Newer Labs"
    assert second.candidate.notice_period_days == 90, "not blanked by the second source"


async def test_the_same_board_application_delivered_twice_is_not_ingested_twice(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """Boards re-send on their own schedule, and a poll can overlap a webhook."""
    await _careers_config(session, tenant)
    await _live_posting(session, tenant, hr_user)

    first = await ingestion.ingest_application(session, hr_user, _raw(), now=NOW)
    await session.commit()
    second = await ingestion.ingest_application(session, hr_user, _raw(), now=NOW)
    await session.commit()

    assert second.duplicate
    assert second.application.id == first.application.id
    assert "Already ingested" in second.note

    total = await session.scalar(
        select(func.count()).select_from(Application).where(Application.tenant_id == tenant.id)
    )
    assert total == 1


async def test_an_application_with_no_contact_details_creates_nothing(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """No way to contact them, no way to tell them apart from anyone else."""
    await _live_posting(session, tenant, hr_user)
    outcome = await ingestion.ingest_application(
        session, hr_user, _raw(email="", phone=None), now=NOW
    )
    assert outcome.application is None
    assert "neither an email nor a phone" in outcome.note


async def test_an_application_for_an_unknown_job_is_held_back_not_misfiled(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """An application filed against the wrong role is worse than one held
    back."""
    await _careers_config(session, tenant)
    outcome = await ingestion.ingest_application(
        session, hr_user, _raw(external_job_id="POSTED-ELSEWHERE"), now=NOW
    )
    assert outcome.application is None
    assert "outside AskMira" in outcome.note


async def test_a_batch_counts_each_branch_separately(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """"40 applications" that turns out to be 38 duplicates is a very different
    day from 40 new people."""
    await _careers_config(session, tenant)
    await _live_posting(session, tenant, hr_user)

    await ingestion.ingest_application(session, hr_user, _raw(), now=NOW)
    await session.commit()

    result = await ingestion.ingest_batch(
        session,
        hr_user,
        [
            _raw(),  # already ingested
            _raw(external_application_id="NKR-A2", email="priya@example.test",
                 phone="9000000002", full_name="Priya Nair"),
            _raw(external_application_id="NKR-A3", email="rohit@example.test",
                 phone=None, full_name="Rohit Verma"),
            _raw(external_application_id="NKR-A4", email="", phone=None),
        ],
        now=NOW,
    )
    await session.commit()

    assert result["received"] == 4
    assert result["already_ingested"] == 1
    assert result["new_candidates"] == 1
    assert result["linked_to_existing"] == 1
    assert result["not_created"] == 1


# ═══════════════════════════════════════════════════════════════════════════
# The careers page
# ═══════════════════════════════════════════════════════════════════════════


async def test_the_careers_page_lists_only_open_roles(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """A page listing a filled role collects applications from people who have
    no chance."""
    await _careers_config(session, tenant)
    await _requisition(session, tenant, code="OPEN-1")
    await _requisition(session, tenant, code="HELD-1", status=RequisitionStatus.ON_HOLD)

    page = await careers_service.public_job_list(session, "acme")
    assert [j["code"] for j in page["jobs"]] == ["OPEN-1"]
    assert page["branding"]["hero_heading"] == "Build with us"
    assert page["allow_apply_without_account"] is True


async def test_an_unpublished_careers_page_is_a_404(
    session: AsyncSession, tenant: Tenant
) -> None:
    config = await _careers_config(session, tenant)
    config.is_published = False
    await session.commit()
    with pytest.raises(HTTPException) as exc:
        await careers_service.public_job_list(session, "acme")
    assert exc.value.status_code == 404


async def test_a_job_page_carries_structured_data_that_would_index(
    session: AsyncSession, tenant: Tenant, location: Location
) -> None:
    """The checklist gate: validate the structured data.

    A job page without indexable markup makes the whole portal a link recruiters
    have to send people, rather than a source of applicants.
    """
    await _careers_config(session, tenant)
    await _requisition(session, tenant, code="REQ-SEO", location_id=location.id)

    detail = await careers_service.public_job_detail(session, "acme", "REQ-SEO")
    data = detail["structured_data"]

    assert data["@type"] == "JobPosting"
    assert data["title"] == "Senior Backend Engineer"
    assert data["hiringOrganization"]["name"] == tenant.name
    assert data["datePosted"]
    assert data["jobLocation"]["address"]["addressLocality"] == location.name
    assert data["directApply"] is True
    assert detail["seo_problems"] == [], detail["seo_problems"]


async def test_the_budget_band_is_never_emitted_into_search_results(
    session: AsyncSession, tenant: Tenant, location: Location
) -> None:
    """The requisition's budget is an internal number. Publishing it because it
    happens to be available would put the client's own salary bands into
    Google."""
    await _careers_config(session, tenant)
    await _requisition(session, tenant, code="REQ-PAY", location_id=location.id)
    detail = await careers_service.public_job_detail(session, "acme", "REQ-PAY")
    assert "baseSalary" not in detail["structured_data"]


async def test_a_closed_role_is_gone_from_the_page_not_left_stale(
    session: AsyncSession, tenant: Tenant
) -> None:
    await _careers_config(session, tenant)
    requisition = await _requisition(session, tenant, code="REQ-GONE")
    requisition.status = RequisitionStatus.CLOSED
    await session.commit()
    with pytest.raises(HTTPException) as exc:
        await careers_service.public_job_detail(session, "acme", "REQ-GONE")
    assert "no longer open" in exc.value.detail


async def test_culture_content_carries_no_employee_names(
    session: AsyncSession, tenant: Tenant, employee: Employee, legal_entity: LegalEntity
) -> None:
    """A shoutout is visible to colleagues, which is not the same as consenting
    to appear on the public internet."""
    other = Employee(
        tenant_id=tenant.id, employee_code="CUL001", first_name="Vikram",
        last_name="Rao", work_email="vikram@acme.test", legal_entity_id=legal_entity.id,
    )
    session.add(other)
    await session.flush()
    session.add_all([
        Shoutout(
            tenant_id=tenant.id, from_employee_id=employee.id, to_employee_id=other.id,
            message="Carried the migration weekend single-handed.",
            value_tag="ownership", visibility=ShoutoutVisibility.COMPANY,
        ),
        Shoutout(
            tenant_id=tenant.id, from_employee_id=other.id, to_employee_id=employee.id,
            message="A private word of thanks.", visibility=ShoutoutVisibility.PRIVATE,
        ),
        Club(tenant_id=tenant.id, name="Running Club", slug="running", is_private=False),
        Club(tenant_id=tenant.id, name="Leadership", slug="leadership", is_private=True),
    ])
    await _careers_config(session, tenant)
    await session.commit()

    page = await careers_service.public_job_list(session, "acme")
    culture = page["culture"]

    assert len(culture["shoutouts"]) == 1, "the private one stays private"
    assert culture["shoutouts"][0]["message"].startswith("Carried the migration")
    assert [c["name"] for c in culture["clubs"]] == ["Running Club"]

    serialised = str(culture)
    for name in ("Vikram", "Rao", employee.first_name):
        assert name not in serialised, f"{name} leaked onto the public page"


async def test_culture_content_is_empty_rather_than_invented_when_there_is_none(
    session: AsyncSession, tenant: Tenant
) -> None:
    """An invented testimonial on a careers page is exactly the kind of thing a
    new joiner finds out about in their first week."""
    await _careers_config(session, tenant)
    page = await careers_service.public_job_list(session, "acme")
    assert page["culture"] == {"shoutouts": [], "clubs": []}


# ═══════════════════════════════════════════════════════════════════════════
# Consent, the candidate portal, and erasure
# ═══════════════════════════════════════════════════════════════════════════


async def test_consent_is_recorded_per_purpose_with_the_notice_that_was_shown(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """Bundled consent is not consent. Somebody applying for one job has not
    agreed to be kept on file for two years."""
    config = await _careers_config(session, tenant)
    await _live_posting(session, tenant, hr_user)

    outcome = await ingestion.ingest_application(
        session, hr_user, _raw(),
        consent_purposes=[ConsentPurpose.APPLICATION_PROCESSING],
        notice_text=config.privacy_notice_html,
        notice_version=config.privacy_notice_version,
        now=NOW,
    )
    await session.commit()

    rows = (
        await session.scalars(
            select(CandidateConsent).where(
                CandidateConsent.candidate_id == outcome.candidate.id
            )
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].purpose is ConsentPurpose.APPLICATION_PROCESSING
    assert rows[0].notice_version == "2026-01"
    assert "two years" in rows[0].notice_text

    assert await ingestion.effective_consent(
        session, hr_user, candidate_id=outcome.candidate.id,
        purpose=ConsentPurpose.APPLICATION_PROCESSING,
    )
    assert not await ingestion.effective_consent(
        session, hr_user, candidate_id=outcome.candidate.id,
        purpose=ConsentPurpose.TALENT_POOL_RETENTION,
    )


async def test_withdrawal_is_a_new_row_not_an_edit(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """"Did they consent on the day we processed this" has to stay answerable
    after they withdraw."""
    await _careers_config(session, tenant)
    await _live_posting(session, tenant, hr_user)
    outcome = await ingestion.ingest_application(
        session, hr_user, _raw(),
        consent_purposes=[ConsentPurpose.TALENT_POOL_RETENTION],
        notice_text="notice", notice_version="1", now=NOW,
    )
    await session.commit()

    await ingestion.record_consent(
        session, hr_user, candidate_id=outcome.candidate.id,
        purpose=ConsentPurpose.TALENT_POOL_RETENTION, granted=False,
        notice_text="Withdrawn by the candidate.", notice_version="1",
        now=NOW + timedelta(days=30),
    )
    await session.commit()

    rows = (
        await session.scalars(
            select(CandidateConsent).where(
                CandidateConsent.candidate_id == outcome.candidate.id
            )
        )
    ).all()
    assert len(rows) == 2, "the original grant survives"
    assert not await ingestion.effective_consent(
        session, hr_user, candidate_id=outcome.candidate.id,
        purpose=ConsentPurpose.TALENT_POOL_RETENTION,
    )


async def test_a_candidate_sees_their_own_status_without_an_account(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """Being left in silence is the loudest complaint about hiring here."""
    await _careers_config(session, tenant)
    await _live_posting(session, tenant, hr_user)
    outcome = await ingestion.ingest_application(session, hr_user, _raw(), now=NOW)
    outcome.application.status = ApplicationStatus.SHORTLISTED
    row, token = await careers_service.issue_portal_token(
        session, hr_user, candidate_id=outcome.candidate.id, now=NOW
    )
    await session.commit()

    view = await careers_service.portal_view(session, token, now=NOW)
    assert view["candidate"]["name"] == "Rohit Verma"
    assert view["applications"][0]["status"] == "shortlisted"
    assert "shortlisted" in view["applications"][0]["status_note"].lower()

    # Only the hash is stored — the token itself is never retrievable again.
    assert row.token_hash != token
    await session.refresh(row)
    assert row.last_used_at is not None


async def test_the_portal_never_exposes_the_internal_pipeline_stage(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """A candidate needs to know somebody is looking, not how many people are
    ahead of them."""
    await _careers_config(session, tenant)
    await _live_posting(session, tenant, hr_user)
    outcome = await ingestion.ingest_application(session, hr_user, _raw(), now=NOW)
    _, token = await careers_service.issue_portal_token(
        session, hr_user, candidate_id=outcome.candidate.id, now=NOW
    )
    await session.commit()

    view = await careers_service.portal_view(session, token, now=NOW)
    serialised = str(view)
    for internal in ("ai_interview", "human_interview", "fit_score", "screening"):
        assert internal not in serialised, internal


async def test_an_expired_portal_link_says_so_rather_than_404ing(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    await _careers_config(session, tenant)
    await _live_posting(session, tenant, hr_user)
    outcome = await ingestion.ingest_application(session, hr_user, _raw(), now=NOW)
    _, token = await careers_service.issue_portal_token(
        session, hr_user, candidate_id=outcome.candidate.id, now=NOW
    )
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await careers_service.portal_view(
            session, token, now=NOW + timedelta(days=careers_service.PORTAL_TOKEN_TTL_DAYS + 1)
        )
    assert exc.value.status_code == 410
    assert "expired" in exc.value.detail


# ═══════════════════════════════════════════════════════════════════════════
# Erasure and retention
# ═══════════════════════════════════════════════════════════════════════════


async def test_an_erasure_request_removes_the_person_and_keeps_the_statistics(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """The checklist gate: a deletion request purges the resume and the PII.

    The application row survives without personal data, because the count of
    applications is not personal data — deleting it would silently change last
    year's hiring numbers.
    """
    await _careers_config(session, tenant)
    await _live_posting(session, tenant, hr_user)
    outcome = await ingestion.ingest_application(
        session, hr_user, _raw(resume_url="https://naukri.test/resume/1"),
        consent_purposes=[ConsentPurpose.APPLICATION_PROCESSING],
        notice_text="notice", notice_version="1", now=NOW,
    )
    session.add(
        CandidateDocument(
            tenant_id=tenant.id, candidate_id=outcome.candidate.id, category="pan",
            file_name="pan.pdf", storage_key="s3://bucket/pan.pdf", uploaded_at=NOW,
            retention_until=NOW.date() + timedelta(days=730),
        )
    )
    _, token = await careers_service.issue_portal_token(
        session, hr_user, candidate_id=outcome.candidate.id, now=NOW
    )
    await session.commit()

    result = await careers_service.purge_on_request(
        session, hr_user, candidate_id=outcome.candidate.id, now=NOW
    )
    await session.commit()

    assert result["applications_purged"] == 1
    assert result["documents_purged"] == 1
    assert result["tokens_revoked"] == 1

    candidate = await session.get(Candidate, outcome.candidate.id)
    assert candidate.full_name == careers_service.REDACTED
    assert candidate.phone is None
    assert candidate.resume_url is None
    assert "rohit@example.test" not in candidate.email

    application = await session.get(Application, outcome.application.id)
    assert application is not None, "the row survives so the statistics do not change"
    assert application.raw_payload == {}
    assert application.purged_at is not None
    assert application.board is JobBoard.NAUKRI
    assert application.applied_at == NOW

    # The portal link dies with the data it showed.
    with pytest.raises(HTTPException):
        await careers_service.portal_view(session, token, now=NOW)

    # And the consent record survives, because "did they consent and then
    # withdraw" is exactly what gets asked about an erasure.
    consents = (
        await session.scalars(
            select(CandidateConsent).where(
                CandidateConsent.candidate_id == outcome.candidate.id
            )
        )
    ).all()
    assert any(c.granted for c in consents)
    assert any(not c.granted for c in consents)


async def test_someone_who_joined_cannot_be_erased_through_the_candidate_route(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """Their record is held under employment law retention now, not candidate
    retention."""
    await _careers_config(session, tenant)
    await _live_posting(session, tenant, hr_user)
    outcome = await ingestion.ingest_application(session, hr_user, _raw(), now=NOW)
    outcome.candidate.converted_employee_id = employee.id
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await careers_service.purge_on_request(
            session, hr_user, candidate_id=outcome.candidate.id
        )
    assert "employment law retention" in exc.value.detail


async def test_the_retention_purge_uses_the_notice_that_was_in_force_at_the_time(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """A candidate who applied under a one-year notice does not acquire a
    two-year retention because the client changed the page."""
    config = await _careers_config(session, tenant, retention_days=365)
    await _live_posting(session, tenant, hr_user)
    outcome = await ingestion.ingest_application(session, hr_user, _raw(), now=NOW)
    await session.commit()
    assert outcome.application.retention_until == NOW.date() + timedelta(days=365)

    # The client later extends the page's retention. The existing row keeps its
    # own date.
    config.resume_retention_days = 1825
    await session.commit()
    await session.refresh(outcome.application)
    assert outcome.application.retention_until == NOW.date() + timedelta(days=365)

    result = await careers_service.purge_expired(
        session, hr_user, as_of=NOW.date() + timedelta(days=400)
    )
    await session.commit()
    assert result["candidates_purged"] == 1


async def test_a_candidate_with_a_newer_application_is_not_purged(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """A person with something still inside its window has not aged out."""
    await _careers_config(session, tenant, retention_days=365)
    await _live_posting(session, tenant, hr_user, code="REQ-001")
    first = await ingestion.ingest_application(session, hr_user, _raw(), now=NOW)
    await session.commit()

    second_requisition = await _requisition(session, tenant, code="REQ-002")
    await ingestion.ingest_application(
        session, hr_user,
        _raw(external_application_id="NKR-A9", external_job_id=""),
        requisition_id=second_requisition.id,
        now=NOW + timedelta(days=300),
    )
    await session.commit()

    result = await careers_service.purge_expired(
        session, hr_user, as_of=NOW.date() + timedelta(days=400)
    )
    await session.commit()

    assert result["applications_past_retention"] == 1
    assert result["candidates_purged"] == 0
    candidate = await session.get(Candidate, first.candidate.id)
    assert candidate.full_name == "Rohit Verma"


async def test_careers_pages_are_tenant_isolated_by_slug(
    session: AsyncSession, tenant: Tenant, other_tenant: Tenant
) -> None:
    """The slug is the tenant resolution: there is nobody logged in on a careers
    page to resolve one from."""
    await _careers_config(session, tenant, slug="acme")
    session.add(
        CareersPageConfig(
            tenant_id=other_tenant.id, slug="other", is_published=True,
            privacy_notice_html="<p>notice</p>",
        )
    )
    await _requisition(session, tenant, code="ACME-1")
    session.add(
        Requisition(
            tenant_id=other_tenant.id, code="OTHER-1", title="Their role",
            status=RequisitionStatus.OPEN,
        )
    )
    await session.commit()

    acme = await careers_service.public_job_list(session, "acme")
    other = await careers_service.public_job_list(session, "other")
    assert [j["code"] for j in acme["jobs"]] == ["ACME-1"]
    assert [j["code"] for j in other["jobs"]] == ["OTHER-1"]
