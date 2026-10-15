"""The AI features, end to end, against a deterministic model.

What these hold: nothing reaches a model unless the server is configured, the
tenant has switched the feature on and the person has agreed; every call leaves
a transfer row and an invocation row; what the model sees is bounded (no names,
no personal records, no data rows); and what it returns is checked before
anyone sees it.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_features import CandidateScreening, SurveyTopicSummary
from app.models.autonomy import AgentProposal, ProposalStatusEnum
from app.models.culture_depth import (
    Survey,
    SurveyKindEnum,
    SurveyParticipant,
    SurveyResponse,
    SurveyStatusEnum,
)
from app.models.cycle_config import ProbationReview
from app.models.dpdp import (
    ConsentCapturedVia,
    ConsentRecord,
    CrossBorderTransfer,
    DataPrincipalKind,
)
from app.models.employee import Employee, EmploymentStatus
from app.models.grounding import AgentInvocation, InvocationOutcome
from app.models.hiring import Candidate, Requisition, RequisitionStatus
from app.models.leave import Holiday
from app.models.lifecycle import (
    LifecycleEvent,
    LifecycleEventType,
    LifecycleStatus,
    ProbationPolicy,
    SilencePolicy,
)
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.ai import analytics, assistant, governance, hiring, recognition, survey_topics
from app.services.ai.client import Completion, use_backend
from app.services.autonomy import agents
from app.services.autonomy import service as autonomy
from app.services.hiring import requisitions
from tests.integration.conftest import as_caller, requires_db

pytestmark = requires_db

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)


class FakeModel:
    """Answers in order; remembers what it was shown."""

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    async def complete(
        self, *, system: str, user: str, schema: dict, max_tokens: int, effort: str
    ) -> Completion:
        self.prompts.append(user)
        reply = self.replies.pop(0)
        text = reply(user) if callable(reply) else json.dumps(reply)
        return Completion(text=text, model="claude-test", input_tokens=120, output_tokens=40)


async def _consent(
    session: AsyncSession, tenant: Tenant, principal: uuid.UUID, purpose: str
) -> None:
    session.add(
        ConsentRecord(
            tenant_id=tenant.id,
            principal_id=principal,
            principal_kind=DataPrincipalKind.EMPLOYEE,
            purpose_key=purpose,
            granted=True,
            decided_at=NOW - timedelta(days=1),
            purpose_version=1,
            captured_via=ConsentCapturedVia.SELF_SERVICE,
        )
    )
    await session.commit()


async def _ai_on(session: AsyncSession, hr_user: User) -> None:
    await governance.set_settings(
        session, hr_user, governance.AiSettings(enabled=True), request=None
    )


async def _employee_user(
    session: AsyncSession, tenant: Tenant, employee: Employee, *, role: UserRole = UserRole.EMPLOYEE
) -> User:
    user = User(
        tenant_id=tenant.id,
        email=f"{uuid.uuid4().hex[:8]}@acme.test",
        password_hash="x",
        full_name="Anita Sharma",
        role=role,
        is_active=True,
        employee_id=employee.id,
    )
    session.add(user)
    await session.commit()
    return await as_caller(session, user)


# ── The gate ────────────────────────────────────────────────────────────────


async def test_nothing_is_sent_until_the_tenant_turns_it_on_and_the_person_agrees(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    me = await _employee_user(session, tenant, employee)
    model = FakeModel()
    with use_backend(model):
        with pytest.raises(HTTPException) as off:
            await assistant.ask(session, me, "How many casual leaves do we get?")
        assert off.value.status_code == 403
        assert "turned off" in off.value.detail

        await _ai_on(session, hr_user)
        with pytest.raises(HTTPException) as no_consent:
            await assistant.ask(session, me, "How many casual leaves do we get?")
        assert no_consent.value.status_code == 403
        assert "not agreed" in no_consent.value.detail
    assert model.prompts == []


async def test_without_a_configured_provider_the_feature_says_so(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    me = await _employee_user(session, tenant, employee)
    await _ai_on(session, hr_user)
    with pytest.raises(HTTPException) as unconfigured:
        await assistant.ask(session, me, "How many casual leaves do we get?")
    assert unconfigured.value.status_code == 503


# ── The assistant ───────────────────────────────────────────────────────────


async def test_a_policy_answer_cites_what_hr_wrote_and_leaves_a_record(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    me = await _employee_user(session, tenant, employee)
    await _ai_on(session, hr_user)
    await _consent(session, tenant, employee.id, "ai_assistance")
    article = await assistant.create_article(
        session,
        hr_user,
        assistant.ArticleIn(
            title="Work from home",
            body="Employees may work from home two days a week with their manager's agreement.",
            is_published=True,
        ),
        request=None,
    )
    await assistant.create_article(
        session,
        hr_user,
        assistant.ArticleIn(
            title="Draft policy", body="Not yet decided, do not cite.", is_published=False
        ),
        request=None,
    )
    cite = f"[[assistant_articles:{article['id']}]]"
    model = FakeModel(
        {
            "kind": "answer",
            "answer": f"You can work from home two days a week with manager agreement {cite}.",
            "lookup": "none",
            "confidence": 0.9,
        }
    )
    with use_backend(model):
        result = await assistant.ask(session, me, "Can I work from home?")

    assert result["outcome"] == "answered"
    assert result["answer"].endswith("[1].")
    assert result["citations"][0]["label"] == "Work from home"
    assert result["confidence_band"] == "high"
    # Drafts are not policy, and nobody's record is in the prompt.
    assert "Not yet decided" not in model.prompts[0]
    assert "Anita" not in model.prompts[0]

    transfer = (await session.scalars(select(CrossBorderTransfer))).one()
    assert transfer.sub_processor_key == "anthropic"
    assert set(transfer.data_categories) == {"ai_interaction", "organisational"}
    invocation = (await session.scalars(select(AgentInvocation))).one()
    assert invocation.agent_key == "assistant"
    assert invocation.sub_processor_key == "anthropic"
    assert invocation.tokens == 160


async def test_an_uncited_claim_is_withheld_not_shown(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    me = await _employee_user(session, tenant, employee)
    await _ai_on(session, hr_user)
    await _consent(session, tenant, employee.id, "ai_assistance")
    await assistant.create_article(
        session,
        hr_user,
        assistant.ArticleIn(
            title="Leave", body="Casual leave is twelve days a year.", is_published=True
        ),
        request=None,
    )
    model = FakeModel(
        {
            "kind": "answer",
            "answer": "You get 18 days of leave.",
            "lookup": "none",
            "confidence": 0.8,
        }
    )
    with use_backend(model):
        result = await assistant.ask(session, me, "How much leave do I get?")
    assert result["outcome"] == "withheld"
    assert "18 days" not in result["answer"]


async def test_a_question_about_my_own_record_is_answered_here_not_by_the_model(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    me = await _employee_user(session, tenant, employee)
    await _ai_on(session, hr_user)
    await _consent(session, tenant, employee.id, "ai_assistance")
    session.add(
        Holiday(
            tenant_id=tenant.id,
            name="Diwali",
            holiday_date=date.today() + timedelta(days=10),
            year=(date.today() + timedelta(days=10)).year,
        )
    )
    await session.commit()
    model = FakeModel(
        {"kind": "personal_lookup", "answer": "", "lookup": "leave_balance", "confidence": 1}
    )
    with use_backend(model):
        result = await assistant.ask(session, me, "How many leaves do I have left?")
    assert result["answered_from"] == "your_record"
    assert result["open"] == "/time"
    assert "leave" in result["answer"].lower()


async def test_a_question_the_policies_do_not_cover_is_refused_plainly(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    me = await _employee_user(session, tenant, employee)
    await _ai_on(session, hr_user)
    await _consent(session, tenant, employee.id, "ai_assistance")
    await assistant.create_article(
        session,
        hr_user,
        assistant.ArticleIn(
            title="Dress code", body="Business casual on weekdays.", is_published=True
        ),
        request=None,
    )
    model = FakeModel(
        {
            "kind": "cannot_answer",
            "answer": "Nothing on relocation.",
            "lookup": "none",
            "confidence": 0.2,
        }
    )
    with use_backend(model):
        result = await assistant.ask(session, me, "Is there a relocation allowance?")
    assert result["outcome"] == "refused"
    assert "not guessed" in result["answer"]

    report = await assistant.report(session, hr_user)
    assert report["total"] == 1
    assert report["unanswered"][0]["question"] == "Is there a relocation allowance?"


# ── Recognition ─────────────────────────────────────────────────────────────


async def test_a_shoutout_draft_never_sends_the_colleagues_name(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    colleague = Employee(
        tenant_id=tenant.id,
        employee_code="ACM777",
        first_name="Ravi",
        last_name="Iyer",
        work_email="ravi@acme.test",
        legal_entity_id=employee.legal_entity_id,
        status=EmploymentStatus.ACTIVE,
        date_of_joining=date(2025, 1, 1),
    )
    session.add(colleague)
    await session.commit()
    me = await _employee_user(session, tenant, employee)
    await _ai_on(session, hr_user)
    await _consent(session, tenant, employee.id, "ai_assistance")
    model = FakeModel({"message": "Thank you {name} for staying late to fix the release."})
    with use_backend(model):
        result = await recognition.draft(
            session,
            me,
            to_employee_id=colleague.id,
            notes="fixed the release late night",
            value_code=None,
        )
    assert result["draft"] == "Thank you Ravi for staying late to fix the release."
    assert "Ravi" not in model.prompts[0]


# ── Survey themes ───────────────────────────────────────────────────────────


async def _survey_with_comments(
    session: AsyncSession,
    tenant: Tenant,
    employee: Employee,
    comments: list[str],
    *,
    ai_ok: set[int] | None = None,
    code_prefix: str = "S",
) -> tuple[Survey, list[Employee]]:
    survey = Survey(
        tenant_id=tenant.id,
        name="October pulse",
        kind=SurveyKindEnum.PULSE,
        opens_on=date(2026, 10, 1),
        closes_on=date(2026, 10, 10),
        status=SurveyStatusEnum.CLOSED,
        min_responses_to_report=5,
    )
    session.add(survey)
    await session.flush()
    people = []
    for index, text in enumerate(comments):
        person = Employee(
            tenant_id=tenant.id,
            employee_code=f"{code_prefix}{index:03d}",
            first_name=f"P{index}",
            work_email=f"{code_prefix.lower()}{index}@acme.test",
            legal_entity_id=employee.legal_entity_id,
            status=EmploymentStatus.ACTIVE,
            date_of_joining=date(2025, 1, 1),
        )
        session.add(person)
        await session.flush()
        people.append(person)
        session.add(
            SurveyParticipant(
                tenant_id=tenant.id,
                survey_id=survey.id,
                employee_id=person.id,
                responded_at=NOW,
            )
        )
        session.add(
            SurveyResponse(
                tenant_id=tenant.id,
                survey_id=survey.id,
                answers={},
                free_text=text,
                comment_ai_ok=ai_ok is None or index in ai_ok,
                submitted_on=date(2026, 10, 5),
            )
        )
    await session.commit()
    return survey, people


async def test_only_comments_their_writers_opened_to_ai_are_read(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    comments = [f"The new shift timings make the commute very hard, comment {i}" for i in range(7)]
    # Four of seven ticked the box: below the floor of five, nothing is sent.
    survey, _ = await _survey_with_comments(
        session, tenant, employee, comments, ai_ok={0, 1, 2, 3}
    )
    await _ai_on(session, hr_user)

    model = FakeModel()
    with use_backend(model), pytest.raises(HTTPException) as blocked:
        await survey_topics.generate(session, hr_user, survey.id)
    assert blocked.value.status_code == 422
    assert "4 of the survey's 7" in blocked.value.detail
    assert model.prompts == []

    # Five ticked: those five are sent, and the two that did not tick are not.
    other, _ = await _survey_with_comments(
        session, tenant, employee, [f"Second survey comment number {i}" for i in range(7)],
        ai_ok={0, 1, 2, 3, 4}, code_prefix="T",
    )
    model = FakeModel(
        {"topics": [{"theme": "Comments", "summary": "People left comments.", "mentions": 5,
                     "sentiment": "mixed"}], "confidence": 0.6}
    )
    with use_backend(model):
        result = await survey_topics.generate(session, hr_user, other.id)
    assert result["comment_count"] == 5
    sent = model.prompts[-1]
    assert "comment number 4" in sent and "comment number 5" not in sent


async def test_survey_themes_are_counted_folded_and_never_quoted(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    comments = [
        "The new shift timings make the commute very hard for those of us outside the city",
        "Shift start is too early",
        "Commute after the shift change is exhausting",
        "Timings changed without asking anyone",
        "Shift timings are fine but parking is a problem",
        "My manager is supportive and listens",
    ]
    survey, people = await _survey_with_comments(session, tenant, employee, comments)
    await _ai_on(session, hr_user)
    for person in people:
        await _consent(session, tenant, person.id, "culture_analytics")

    model = FakeModel(
        {
            "topics": [
                {
                    "theme": "Shift timings",
                    "summary": "The new shift timings make the commute very hard for those of us",
                    "mentions": 5,
                    "sentiment": "negative",
                },
                {
                    "theme": "Managers",
                    "summary": "Managers are seen as supportive.",
                    "mentions": 1,
                    "sentiment": "positive",
                },
            ],
            "confidence": 0.7,
        }
    )
    with use_backend(model):
        result = await survey_topics.generate(session, hr_user, survey.id)

    themes = {t["theme"]: t for t in result["topics"]}
    assert themes["Shift timings"]["mentions"] == 5
    assert themes["Shift timings"]["summary"].startswith("Withheld")
    assert "Managers" not in themes
    assert themes["Other themes"]["mentions"] == 1
    assert result["confidence_band"] in ("moderate", "high", "low")
    assert (await session.scalars(select(SurveyTopicSummary))).one().comment_count == 6
    assert (await survey_topics.latest(session, hr_user, survey.id))["id"] == result["id"]


# ── Analytics ───────────────────────────────────────────────────────────────


async def test_an_analytics_question_runs_through_the_compiler_and_no_row_reaches_the_model(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    await _ai_on(session, hr_user)
    await _consent(session, tenant, hr_user.employee_id or hr_user.id, "ai_assistance")
    model = FakeModel(
        {
            "answerable": True,
            "interpretation": "Headcount of active employees.",
            "entity_key": "employee",
            "selections": [{"field_key": "headcount", "aggregation": "count"}],
            "filters": [
                {"field_key": "status", "operator": "equals", "value": "active", "values": []}
            ],
            "group_by": [],
            "sort_by": [],
            "limit": 10,
            "chart": "number",
            "confidence": 0.85,
        }
    )
    with use_backend(model):
        result = await analytics.ask(session, hr_user, "How many active employees do we have?")
    assert result["answered"], result["problem"]
    assert result["result"]["rows"] == [[1]]
    assert "Anita" not in model.prompts[0]
    assert "headcount" in model.prompts[0]


async def test_a_definition_the_compiler_refuses_is_reported_not_guessed(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    await _ai_on(session, hr_user)
    await _consent(session, tenant, hr_user.employee_id or hr_user.id, "ai_assistance")
    model = FakeModel(
        {
            "answerable": True,
            "interpretation": "Sum of employee names.",
            "entity_key": "employee",
            "selections": [{"field_key": "employee_name", "aggregation": "sum"}],
            "filters": [],
            "group_by": [],
            "sort_by": [],
            "limit": 10,
            "chart": "number",
            "confidence": 0.3,
        }
    )
    with use_backend(model):
        result = await analytics.ask(session, hr_user, "Add up everyone's names")
    assert not result["answered"]
    assert result["problem"]
    invocation = (await session.scalars(select(AgentInvocation))).one()
    assert invocation.outcome is InvocationOutcome.WITHHELD


# ── Hiring ──────────────────────────────────────────────────────────────────


async def _requisition(session: AsyncSession, tenant: Tenant, **extra: Any) -> Requisition:
    requisition = Requisition(
        tenant_id=tenant.id,
        code=f"REQ-{uuid.uuid4().hex[:6]}",
        title="Backend engineer",
        skills=["Python", "PostgreSQL"],
        experience_min_years=3,
        experience_max_years=6,
        **extra,
    )
    session.add(requisition)
    await session.commit()
    return requisition


async def test_a_resume_is_read_without_the_candidates_identity_and_evidence_is_checked(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    await _ai_on(session, hr_user)
    requisition = await _requisition(session, tenant)
    candidate = Candidate(
        tenant_id=tenant.id,
        requisition_id=requisition.id,
        full_name="Meera Krishnan",
        email="meera@example.com",
        parsed_resume={
            "text": (
                "Meera Krishnan meera@example.com +91 98765 43210 linkedin.com/in/meera\n"
                "Five years building Python services on Django."
            )
        },
    )
    session.add(candidate)
    await session.commit()

    model = FakeModel(
        {
            "requirements": [
                {
                    "requirement": "Python",
                    "assessment": "met",
                    "evidence": "Five years building Python services",
                },
                {
                    "requirement": "PostgreSQL",
                    "assessment": "met",
                    "evidence": "Ran PostgreSQL at scale",
                },
                {
                    "requirement": "3 to 6 years of relevant experience",
                    "assessment": "met",
                    "evidence": "Five years",
                },
            ],
            "summary": "Strong Python background.",
            "confidence": 0.75,
        }
    )
    with use_backend(model):
        result = await hiring.screen_candidate(session, hr_user, candidate.id)

    prompt = model.prompts[0]
    for secret in ("Meera", "Krishnan", "meera@example.com", "98765", "linkedin.com/in/meera"):
        assert secret not in prompt
    rows = {row["requirement"]: row for row in result["requirements"]}
    assert rows["Python"]["assessment"] == "met"
    assert rows["PostgreSQL"]["assessment"] == "not_evident"
    assert rows["PostgreSQL"]["note"]
    assert result["requirements_met"] == 2
    assert result["requirements_total"] == 3
    assert result["redactions"]
    assert (await session.scalars(select(CandidateScreening))).one().requirements_met == 2


async def test_a_jd_draft_is_returned_and_the_published_version_remembers_it(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    await _ai_on(session, hr_user)
    requisition = await _requisition(session, tenant, status=RequisitionStatus.ON_HOLD)
    model = FakeModel({"description": "About the role: build services.\nWhat you will do:\n- Ship"})
    with use_backend(model):
        drafted = await hiring.draft_jd(session, hr_user, requisition_id=requisition.id, notes="")
    assert drafted["draft"].startswith("About the role")

    requisition.description = drafted["draft"]
    requisition.description_ai_assisted = True
    await session.commit()
    await requisitions.change_status(
        session,
        hr_user,
        requisition_id=requisition.id,
        to_status=RequisitionStatus.OPEN,
        reason="Budget released",
    )
    await session.commit()
    await session.refresh(requisition)
    assert requisition.jd_versions[-1]["ai_generated"] is True
    assert requisition.jd_versions[-1]["body"] == drafted["draft"]

    # Reopening with an unchanged description adds no version.
    await requisitions.change_status(
        session,
        hr_user,
        requisition_id=requisition.id,
        to_status=RequisitionStatus.ON_HOLD,
        reason="Pause",
    )
    await requisitions.change_status(
        session,
        hr_user,
        requisition_id=requisition.id,
        to_status=RequisitionStatus.OPEN,
        reason="Resume",
    )
    await session.commit()
    await session.refresh(requisition)
    assert len(requisition.jd_versions) == 1


# ── The probation agent (AI-03) ─────────────────────────────────────────────


async def test_the_probation_agent_drafts_and_proposes_once_and_approval_submits_it(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    end = date.today() - timedelta(days=20)
    employee.probation_end_date = end
    session.add(
        ProbationPolicy(
            tenant_id=tenant.id, silence_policy=SilencePolicy.AUTO_CONFIRM, grace_days=3
        )
    )
    review = ProbationReview(
        tenant_id=tenant.id,
        employee_id=employee.id,
        probation_end_date=end,
        review_due_date=end - timedelta(days=14),
    )
    session.add(review)
    await session.commit()

    from app.services.scheduler.jobs import system_actor

    actor = system_actor(tenant.id)
    first = await agents.probation_agent(session, actor)
    assert first["proposed"] == 1

    event = (
        await session.scalars(
            select(LifecycleEvent).where(
                LifecycleEvent.event_type == LifecycleEventType.CONFIRMATION
            )
        )
    ).one()
    assert event.status is LifecycleStatus.DRAFT
    proposal = (await session.scalars(select(AgentProposal))).one()
    assert proposal.status is ProposalStatusEnum.PENDING
    assert f"probation_reviews:{review.id}" in proposal.citations

    again = await agents.probation_agent(session, actor)
    assert again["proposed"] == 0
    assert again["already_in_hand"] == 1

    await autonomy.decide_proposal(session, hr_user, proposal_id=proposal.id, approve=True)
    await session.refresh(event)
    assert event.status is LifecycleStatus.PENDING_APPROVAL


async def test_the_probation_agent_skips_a_tenant_with_no_policy(
    session: AsyncSession, tenant: Tenant
) -> None:
    from app.services.scheduler.jobs import system_actor

    result = await agents.probation_agent(session, system_actor(tenant.id))
    assert "skipped" in result


async def test_ai_is_off_by_default_and_every_feature_is_listed(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    status = await governance.status_for(session, hr_user)
    assert status["enabled"] is False
    assert not any(status["features"].values())
    assert set(status["features"]) == set(governance.FEATURES)
