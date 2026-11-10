"""Board adapters: the mapping, the failure typing, and never dropping a job."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.services.jobboards import backoff
from app.services.jobboards.base import (
    BoardCredentials,
    BoardPermanentFailure,
    BoardTransientFailure,
    JobBoardAdapter,
    JobSpec,
    RateLimited,
)
from app.services.jobboards.indeed import IndeedAdapter, feed_entry
from app.services.jobboards.linkedin import LinkedInAdapter
from app.services.jobboards.naukri import (
    DEFAULT_FUNCTIONAL_AREA,
    MAX_KEY_SKILLS,
    NaukriAdapter,
    map_functional_area,
)

NOW = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)


class FakeTransport:
    """A transport double.

    The point of injecting the transport is that the mapping — which is the part
    that is actually ours and actually hard — is testable without a network or a
    board account.
    """

    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.calls: list[tuple] = []

    async def request(self, method, path, *, json=None, headers=None, form=None):
        self.calls.append((method, path, json if form is None else form, headers))
        return self.responses.pop(0) if self.responses else (200, {})


def _spec(**overrides) -> JobSpec:
    base = {
        "requisition_code": "REQ-001",
        "title": "Senior Backend Engineer",
        "description_html": "<p>Build things.</p>",
        "skills": ("Python", "PostgreSQL"),
        "location": "Bengaluru",
        "experience_min_years": 4,
        "experience_max_years": 8,
        "ctc_min": Decimal("1500000"),
        "ctc_max": Decimal("2500000"),
        "openings": 2,
    }
    base.update(overrides)
    return JobSpec(**base)


CREDS = BoardCredentials(
    username="acme", secret="tok", extras={"recruiter_id": "R1", "organisation_urn": "urn:o:1"}
)


# ═══════════════════════════════════════════════════════════════════════════
# The interface itself
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("adapter_class", [NaukriAdapter, LinkedInAdapter, IndeedAdapter])
def test_every_adapter_implements_the_whole_interface(adapter_class) -> None:
    adapter = adapter_class(FakeTransport())
    assert isinstance(adapter, JobBoardAdapter)
    for method in (
        "publish_job",
        "update_job",
        "close_job",
        "fetch_applications",
        "fetch_resume",
    ):
        assert callable(getattr(adapter, method)), method
    assert adapter.board
    assert adapter.rate_limit_per_minute > 0


async def test_a_board_without_refresh_says_so_rather_than_pretending() -> None:
    """A scheduled refresh that silently does nothing is worse than no refresh,
    because the recruiter stops checking."""
    adapter = IndeedAdapter(FakeTransport())
    with pytest.raises(BoardPermanentFailure, match="no refresh or boost"):
        await adapter.refresh_job("job-1", CREDS)


# ═══════════════════════════════════════════════════════════════════════════
# Naukri's taxonomy
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Senior Backend Engineer", "IT Software - Application Programming"),
        ("QA Automation Lead", "IT Software - QA & Testing"),
        ("Enterprise Sales Manager", "Sales, Retail, Business Development"),
        ("Finance Controller", "Accounts, Finance, Tax, Company Secretary, Audit"),
        ("HR Business Partner", "HR / Recruitment / Administration / IR"),
    ],
)
def test_common_indian_mid_market_titles_map_to_a_functional_area(title, expected) -> None:
    area, matched = map_functional_area(_spec(title=title, skills=()))
    assert matched
    assert area == expected


def test_an_unmatched_title_falls_back_and_warns_about_it() -> None:
    """A posting under "Other" gets a fraction of the views. The recruiter needs
    to know today, not after three quiet weeks."""
    adapter = NaukriAdapter(FakeTransport())
    payload, warnings = adapter.build_payload(
        _spec(title="Chief Vibes Curator", skills=(), department=None)
    )
    assert payload["functionalArea"] == DEFAULT_FUNCTIONAL_AREA
    assert any("did not" in w or "did not match" in w or "matched a Naukri" in w for w in warnings)
    assert any(DEFAULT_FUNCTIONAL_AREA in w for w in warnings)


def test_too_many_key_skills_are_trimmed_and_reported() -> None:
    """Naukri rejects an over-long skills list; it does not truncate it."""
    adapter = NaukriAdapter(FakeTransport())
    payload, warnings = adapter.build_payload(_spec(skills=tuple(f"Skill{i}" for i in range(30))))
    assert len(payload["keySkills"]) == MAX_KEY_SKILLS
    assert any(str(MAX_KEY_SKILLS) in w for w in warnings)


def test_duplicate_skills_are_collapsed_in_order() -> None:
    adapter = NaukriAdapter(FakeTransport())
    payload, _ = adapter.build_payload(_spec(skills=("Python", "SQL", "Python", "SQL")))
    assert payload["keySkills"] == ["Python", "SQL"]


def test_salary_is_hidden_unless_the_requisition_says_to_show_it() -> None:
    """A board default that showed it would publish a number the client
    deliberately kept off the page, and it cannot be unpublished from a
    candidate's memory."""
    adapter = NaukriAdapter(FakeTransport())
    payload, warnings = adapter.build_payload(_spec(show_salary=False))
    assert payload["salary"] == {"hideSalary": True}
    assert any("not shown" in w for w in warnings)


def test_salary_is_published_when_the_requisition_asks_for_it() -> None:
    adapter = NaukriAdapter(FakeTransport())
    payload, _ = adapter.build_payload(_spec(show_salary=True))
    assert payload["salary"]["hideSalary"] is False
    assert payload["salary"]["min"] == "1500000"


async def test_publishing_returns_the_board_id_and_what_it_accepted() -> None:
    transport = FakeTransport((201, {"jobId": "NKR-99"}))
    adapter = NaukriAdapter(transport)
    result = await adapter.publish_job(_spec(), CREDS)
    assert result.external_job_id == "NKR-99"
    assert result.accepted_as["functionalArea"]
    assert transport.calls[0][0] == "POST"


async def test_a_posting_accepted_with_no_id_is_treated_as_a_failure() -> None:
    """Nothing could refresh, update or close it. A posting we cannot manage is
    not a success."""
    adapter = NaukriAdapter(FakeTransport((201, {})))
    with pytest.raises(BoardPermanentFailure, match="returned no job id"):
        await adapter.publish_job(_spec(), CREDS)


# ═══════════════════════════════════════════════════════════════════════════
# Failure typing — the distinction is the point
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_429_is_a_rate_limit_not_a_rejection() -> None:
    adapter = NaukriAdapter(FakeTransport((429, {"message": "slow down", "retryAfter": 45})))
    with pytest.raises(RateLimited) as exc:
        await adapter.publish_job(_spec(), CREDS)
    assert exc.value.retry_after_seconds == 45
    assert isinstance(exc.value, BoardTransientFailure)


async def test_a_422_is_permanent() -> None:
    adapter = NaukriAdapter(FakeTransport((422, {"message": "unknown functional area"})))
    with pytest.raises(BoardPermanentFailure, match="rejected"):
        await adapter.publish_job(_spec(), CREDS)


async def test_a_500_is_transient_but_not_a_rate_limit() -> None:
    adapter = NaukriAdapter(FakeTransport((500, {"message": "oops"})))
    with pytest.raises(BoardTransientFailure) as exc:
        await adapter.publish_job(_spec(), CREDS)
    assert not isinstance(exc.value, RateLimited)


async def test_resdex_without_the_subscription_explains_rather_than_returning_nothing() -> None:
    """An empty list here would read as "no matching candidates", which is not
    what happened."""
    adapter = NaukriAdapter(FakeTransport())
    with pytest.raises(BoardPermanentFailure, match="separate subscription"):
        await adapter.search_resumes("python", CREDS)


async def test_resdex_searches_when_the_subscription_is_on() -> None:
    transport = FakeTransport((200, {"results": [{"id": "1", "email": "a@b.test"}]}))
    adapter = NaukriAdapter(transport)
    creds = BoardCredentials(
        username="acme", secret="tok", extras={"recruiter_id": "R1", "resdex_enabled": True}
    )
    results = await adapter.search_resumes("python", creds)
    assert len(results) == 1


# ═══════════════════════════════════════════════════════════════════════════
# Naukri application parsing
# ═══════════════════════════════════════════════════════════════════════════


async def test_naukri_applications_normalise_into_our_shape() -> None:
    transport = FakeTransport(
        (
            200,
            {
                "applications": [
                    {
                        "applicationId": "A1",
                        "candidateName": " Rohit Verma ",
                        "email": " ROHIT@example.test ",
                        "mobile": "+91 98765 43210",
                        "currentCtc": "1400000",
                        "noticePeriod": "2 Months",
                        "someFieldWeNeverMapped": "kept anyway",
                    }
                ]
            },
        ),
    )
    adapter = NaukriAdapter(transport)
    [application] = await adapter.fetch_applications("NKR-99", CREDS)

    assert application.full_name == "Rohit Verma"
    assert application.email == "ROHIT@example.test"
    assert application.current_ctc == Decimal("1400000")
    assert application.notice_period_days == 60
    # A field nobody mapped is still evidence when a recruiter asks why a
    # candidate looks wrong.
    assert application.raw["someFieldWeNeverMapped"] == "kept anyway"


@pytest.mark.parametrize(
    "written,expected",
    [
        ("Immediate", 0),
        ("15 Days", 15),
        ("2 Months", 60),
        ("3 Month", 90),
        ("", None),
        ("whenever", None),
    ],
)
async def test_naukri_notice_period_free_text_becomes_a_number(written, expected) -> None:
    """Drop-rate by notice period (slice 36) needs a number. Anything
    unparseable stays None — a wrong number in a report is worse than a gap."""
    transport = FakeTransport(
        (
            200,
            {
                "applications": [
                    {"applicationId": "A1", "email": "a@b.test", "noticePeriod": written}
                ]
            },
        )
    )
    [application] = await NaukriAdapter(transport).fetch_applications("J", CREDS)
    assert application.notice_period_days == expected


# ═══════════════════════════════════════════════════════════════════════════
# LinkedIn and Indeed
# ═══════════════════════════════════════════════════════════════════════════


async def test_linkedin_refuses_to_poll_rather_than_returning_an_empty_list() -> None:
    """An empty list would be read as a quiet week, and the applications would
    sit unseen in LinkedIn's own inbox."""
    adapter = LinkedInAdapter(FakeTransport())
    with pytest.raises(BoardPermanentFailure, match="not an empty result"):
        await adapter.fetch_applications("job", CREDS)


async def test_linkedin_without_an_organisation_urn_refuses_to_post() -> None:
    adapter = LinkedInAdapter(FakeTransport())
    creds = BoardCredentials(username="a", secret="b", extras={})
    with pytest.raises(BoardPermanentFailure, match="organisation URN"):
        await adapter.publish_job(_spec(), creds)


def test_linkedin_warns_that_it_guessed_the_workplace_type() -> None:
    """A remote role listed as on-site gets the wrong applicants, not fewer."""
    adapter = LinkedInAdapter(FakeTransport())
    element, warnings = adapter.build_payload(_spec(apply_url="https://hr.example/a"), CREDS)
    assert any("on-site" in w for w in warnings)
    # LinkedIn's documented element: on-site is the default when workplaceTypes is absent.
    assert "workplaceTypes" not in element
    assert isinstance(element["description"], str)
    assert element["listingType"] == "BASIC"
    assert element["listedAt"] < datetime.now(UTC).timestamp() * 1000


def _linkedin(*answers) -> tuple[LinkedInAdapter, FakeTransport]:
    from app.services.jobboards import linkedin

    linkedin._TOKENS.clear()
    transport = FakeTransport((200, {"access_token": "li-token", "expires_in": 1799}), *answers)
    return LinkedInAdapter(transport), transport


LI_CREDS = BoardCredentials(
    username="li-client", secret="li-secret", extras={"organisation_urn": "urn:li:organization:1"}
)


async def test_linkedin_posts_a_batch_create_with_a_client_credentials_token() -> None:
    """LinkedIn's documented contract: a client-credentials token, then one
    batch_create with the job in `elements`, keyed by our own id."""
    adapter, transport = _linkedin(
        (200, {"elements": [{"id": "urn:li:simpleJobPostingTask:abc", "status": 202}]})
    )
    result = await adapter.publish_job(_spec(apply_url="https://hr.example/a"), LI_CREDS)

    token_call, post_call = transport.calls
    assert token_call[1] == "https://www.linkedin.com/oauth/v2/accessToken"
    assert token_call[2] == {
        "grant_type": "client_credentials",
        "client_id": "li-client",
        "client_secret": "li-secret",
    }
    method, path, body, headers = post_call
    assert (method, path) == ("POST", "/v2/simpleJobPostings")
    assert headers["x-restli-method"] == "batch_create"
    assert headers["Authorization"] == "Bearer li-token"
    [element] = body["elements"]
    assert element["jobPostingOperationType"] == "CREATE"
    assert element["externalJobPostingId"] == "REQ-001"
    assert element["integrationContext"] == "urn:li:organization:1"
    assert result.external_job_id == "REQ-001"
    assert result.accepted_as["task"] == "urn:li:simpleJobPostingTask:abc"

    # The token is reused, as LinkedIn asks, rather than fetched again.
    transport.responses.append((200, {"elements": [{"id": "t2", "status": 202}]}))
    await adapter.update_job("REQ-001", _spec(apply_url="https://hr.example/a"), LI_CREDS)
    assert len(transport.calls) == 3
    assert transport.calls[2][2]["elements"][0]["jobPostingOperationType"] == "UPDATE"


async def test_a_linkedin_refusal_inside_a_200_is_a_refusal() -> None:
    """LinkedIn answers HTTP 200 with the error inside the element."""
    adapter, _ = _linkedin(
        (
            200,
            {
                "elements": [
                    {
                        "error": {
                            "message": "ERROR :: /title :: field is required",
                            "status": 422,
                        },
                        "status": 422,
                    }
                ]
            },
        )
    )
    with pytest.raises(BoardPermanentFailure, match="title :: field is required"):
        await adapter.publish_job(_spec(apply_url="https://hr.example/a"), LI_CREDS)


async def test_linkedin_closes_by_reposting_with_close_and_a_closed_job_is_closed() -> None:
    adapter, transport = _linkedin(
        (200, {"elements": [{"id": "t3", "status": 202}]}),
    )
    await adapter.close_job("REQ-001", LI_CREDS, spec=_spec(apply_url="https://hr.example/a"))
    method, path, body, _ = transport.calls[-1]
    assert (method, path) == ("POST", "/v2/simpleJobPostings")
    assert body["elements"][0]["jobPostingOperationType"] == "CLOSE"
    assert body["elements"][0]["externalJobPostingId"] == "REQ-001"

    transport.responses.append(
        (409, {"message": "Cannot close job which is already closed or does not exist"})
    )
    await adapter.close_job("REQ-001", LI_CREDS)  # already closed: nothing to do


async def test_bad_linkedin_app_credentials_say_so() -> None:
    from app.services.jobboards import linkedin

    linkedin._TOKENS.clear()
    adapter = LinkedInAdapter(FakeTransport((401, {"error": "invalid_client"})))
    with pytest.raises(BoardPermanentFailure, match="client id and secret"):
        await adapter.publish_job(_spec(apply_url="https://hr.example/a"), LI_CREDS)


async def test_a_cached_linkedin_token_is_never_reused_with_a_different_secret() -> None:
    """The token cache is keyed on the secret as well as the client id. Keyed on
    the client id alone, a wrong secret (or another workspace typing the same
    client id) posted on the token the right secret had earned. Found in local
    QA on 2026-11-10."""
    adapter, transport = _linkedin((200, {"elements": [{"id": "t1", "status": 202}]}))
    await adapter.publish_job(_spec(apply_url="https://hr.example/a"), LI_CREDS)

    wrong = BoardCredentials(username="li-client", secret="not-the-secret", extras=LI_CREDS.extras)
    transport.responses.append((401, {"error": "invalid_client"}))
    with pytest.raises(BoardPermanentFailure, match="client id and secret"):
        await adapter.update_job("REQ-001", _spec(apply_url="https://hr.example/a"), wrong)
    assert transport.calls[-1][1] == "https://www.linkedin.com/oauth/v2/accessToken"
    assert transport.calls[-1][2]["client_secret"] == "not-the-secret"


def test_linkedin_parses_its_own_webhook() -> None:
    adapter = LinkedInAdapter(FakeTransport())
    application = adapter.parse_webhook({
        "applicationId": "L1",
        "jobPostingId": "P1",
        "applicant": {"firstName": "Priya", "lastName": "Nair", "emailAddress": "p@n.test"},
    })
    assert application.full_name == "Priya Nair"
    assert application.board == "linkedin"


def test_an_ampersand_in_a_description_does_not_break_the_indeed_feed() -> None:
    """An unescaped feed is rejected wholesale, taking every other job in it
    down too."""
    entry = feed_entry(
        _spec(description_html="<p>R&D and Q&A</p>", apply_url="https://x.test/a?b=1&c=2"),
        reference="REQ&001",
    )
    assert "REQ&amp;001" in entry
    assert "b=1&amp;c=2" in entry
    assert entry.startswith("<job>") and entry.endswith("</job>")


async def test_indeed_says_the_job_is_queued_for_a_crawler_not_live() -> None:
    """"Published" that actually means "queued" is the kind of half-truth a
    recruiter discovers by refreshing Indeed for two days. And nothing is sent:
    Indeed reads the feed."""
    transport = FakeTransport()
    adapter = IndeedAdapter(transport)
    spec = _spec(apply_url="https://hr.example/careers/a/jobs/R1")
    result = await adapter.publish_job(spec, CREDS)
    assert any("own schedule" in w and "feed" in w for w in result.warnings)
    assert result.accepted_as["apply_url"] == "https://hr.example/careers/a/jobs/R1"
    assert getattr(transport, "calls", []) == []


async def test_indeed_refuses_a_job_with_nowhere_to_apply() -> None:
    adapter = IndeedAdapter(FakeTransport())
    with pytest.raises(BoardPermanentFailure, match="apply"):
        await adapter.publish_job(_spec(apply_url=None), CREDS)


async def test_indeed_does_not_pretend_to_poll_applications() -> None:
    adapter = IndeedAdapter(FakeTransport())
    with pytest.raises(BoardPermanentFailure, match="not an empty result"):
        await adapter.fetch_applications("R1", CREDS)


def test_the_indeed_feed_wraps_jobs_in_a_publisher_source() -> None:
    from app.services.jobboards.indeed import render_feed

    xml = render_feed(
        [(_spec(description_html="<p>Ends with ]]> oddly</p>"), None)],
        publisher="Acme & Sons",
        publisher_url="https://hr.example/careers/acme",
    )
    import xml.etree.ElementTree as ET

    root = ET.fromstring(xml)  # well-formed, even with ]]> inside a description
    assert root.tag == "source"
    assert root.findtext("publisher") == "Acme & Sons"
    assert root.findtext("job/description") == "<p>Ends with ]]> oddly</p>"
    assert root.findtext("job/country") == "IN"
    # The office's name is the location; Indeed matches on the city.
    assert root.findtext("job/location") == "Bengaluru"
    city = render_feed(
        [(_spec(location="Bengaluru HQ", city="Bengaluru"), None)], publisher="A", publisher_url="u"
    )
    assert "<city><![CDATA[Bengaluru]]></city>" in city
    assert "<location><![CDATA[Bengaluru HQ]]></location>" in city


# ═══════════════════════════════════════════════════════════════════════════
# Backoff — the rule that nothing is dropped
# ═══════════════════════════════════════════════════════════════════════════


def test_a_transient_failure_is_retried() -> None:
    decision = backoff.plan_attempt(
        attempt=1, permanent=False, message="timeout", now=NOW
    )
    assert decision.should_retry
    assert decision.next_attempt_at > NOW
    assert not decision.needs_a_human


def test_the_boards_own_retry_after_wins_over_our_schedule() -> None:
    """Guessing when the board has already told us is how an account gets
    throttled harder — and on Naukri, suspended."""
    decision = backoff.plan_attempt(
        attempt=1, permanent=False, message="429", now=NOW, retry_after_seconds=300
    )
    assert (decision.next_attempt_at - NOW).total_seconds() == 300
    assert "asked us to wait" in decision.reason


def test_the_backoff_delay_grows_and_then_stops_growing() -> None:
    """Doubling past ten minutes means the sixth attempt lands hours later, by
    which point the recruiter has posted it by hand and there are two live
    adverts."""
    delays = [backoff.retry_delay(n).total_seconds() for n in range(1, 9)]
    assert delays == sorted(delays)
    assert max(delays) == 600


def test_a_permanent_failure_is_parked_immediately_not_retried() -> None:
    decision = backoff.plan_attempt(
        attempt=1, permanent=True, message="unknown functional area", now=NOW
    )
    assert decision.needs_a_human
    assert not decision.should_retry
    assert decision.next_attempt_at is None
    assert "will reject it again" in decision.reason


def test_exhausted_attempts_park_rather_than_giving_up_quietly() -> None:
    """The whole point. A posting that stopped being tried and was not parked is
    a posting nobody knows is missing."""
    decision = backoff.plan_attempt(
        attempt=backoff.MAX_ATTEMPTS, permanent=False, message="still timing out", now=NOW
    )
    assert decision.needs_a_human
    assert "not live on the board" in decision.reason


@pytest.mark.parametrize("attempt", range(1, backoff.MAX_ATTEMPTS + 3))
def test_every_outcome_is_retry_or_park_and_never_a_silent_third(attempt) -> None:
    for permanent in (True, False):
        decision = backoff.plan_attempt(
            attempt=attempt, permanent=permanent, message="x", now=NOW
        )
        assert decision.action in ("retry", "park")
        assert decision.should_retry != decision.needs_a_human
        assert decision.reason


def test_a_parked_decision_carries_the_boards_own_words() -> None:
    decision = backoff.plan_attempt(
        attempt=1, permanent=True, message="Location 'Bengaluru' is not recognised", now=NOW
    )
    assert "Bengaluru" in decision.reason
