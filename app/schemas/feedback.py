"""360 feedback and private feedback schemas.

Ratings are `Decimal` and serialise as strings, like every other score in the
performance module.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.models.feedback import (
    FeedbackRequestStatus,
    FeedbackReviewerStatus,
    FeedbackVisibility,
    PrivateFeedbackKind,
)

FeedbackScope = Literal["about_me", "team", "all"]
PrivateFeedbackScope = Literal["received", "given", "team"]
#: How the caller relates to a request, which decides what they see of it.
FeedbackViewerRole = Literal["subject", "manager", "hr", "requester"]


class FeedbackRequestCreate(BaseModel):
    #: Whose feedback this is. Omitted means "mine".
    subject_employee_id: uuid.UUID | None = None
    reviewer_employee_ids: list[uuid.UUID] = Field(min_length=1, max_length=8)
    appraisal_cycle_id: uuid.UUID | None = None
    message: str | None = Field(default=None, max_length=2000)
    due_on: date
    employee_visibility: FeedbackVisibility = FeedbackVisibility.ANONYMISED


class FeedbackQuestion(BaseModel):
    competency_id: uuid.UUID
    code: str
    name: str
    description: str | None = None


class FeedbackScaleLevel(BaseModel):
    value: Decimal
    label: str


class FeedbackScale(BaseModel):
    min: Decimal
    max: Decimal
    levels: list[FeedbackScaleLevel] = Field(default_factory=list)


class FeedbackRequestSummary(BaseModel):
    id: uuid.UUID
    subject_employee_id: uuid.UUID
    subject_name: str
    requested_by_name: str | None
    appraisal_cycle_id: uuid.UUID | None
    cycle_name: str | None
    due_on: date
    status: FeedbackRequestStatus
    employee_visibility: FeedbackVisibility
    reviewers_total: int
    responses: int
    declined: int
    created_at: datetime
    my_role: FeedbackViewerRole


class ReviewerStatusRead(BaseModel):
    id: uuid.UUID
    reviewer_employee_id: uuid.UUID
    name: str
    status: FeedbackReviewerStatus
    submitted_at: datetime | None
    decline_reason: str | None
    last_reminded_at: datetime | None
    reminder_count: int


class CompetencyAnswerRead(BaseModel):
    competency_id: uuid.UUID
    name: str
    rating: Decimal | None
    comment: str | None


class ResponseRead(BaseModel):
    """One answer, with its writer. Only ever returned where names are allowed."""

    reviewer_name: str
    competency_ratings: list[CompetencyAnswerRead]
    strengths: str | None
    improvements: str | None
    overall_rating: Decimal | None
    submitted_at: datetime


class CompetencyAggregateRead(BaseModel):
    competency_id: uuid.UUID
    name: str
    average: Decimal | None
    responses: int
    comments: list[str]


class FeedbackAggregateRead(BaseModel):
    """The anonymised summary. Nothing in it says who wrote what."""

    responses: int
    overall_average: Decimal | None
    overall_count: int
    competencies: list[CompetencyAggregateRead]
    strengths: list[str]
    improvements: list[str]


class FeedbackRequestDetail(FeedbackRequestSummary):
    message: str | None
    questions: list[FeedbackQuestion]
    scale: FeedbackScale | None
    anonymity_threshold: int
    #: Who was asked and where each stands. Absent where it would identify writers.
    reviewers: list[ReviewerStatusRead] | None
    #: Named answers. Absent where names are not allowed to this viewer.
    responses_named: list[ResponseRead] | None
    aggregate: FeedbackAggregateRead | None
    #: One sentence on what this viewer is seeing and why.
    visibility_note: str
    #: The caller may remind reviewers or close the request.
    can_manage: bool


class FeedbackInboxItem(BaseModel):
    reviewer_id: uuid.UUID
    request_id: uuid.UUID
    subject_name: str
    requested_by_name: str | None
    cycle_name: str | None
    due_on: date
    status: FeedbackReviewerStatus
    request_status: FeedbackRequestStatus
    submitted_at: datetime | None
    created_at: datetime


class CompetencyAnswerIn(BaseModel):
    competency_id: uuid.UUID
    rating: Decimal | None = Field(default=None, max_digits=6, decimal_places=2)
    comment: str | None = Field(default=None, max_length=2000)


class FeedbackForm(BaseModel):
    """What a reviewer sees when they answer."""

    reviewer_id: uuid.UUID
    request_id: uuid.UUID
    subject_name: str
    requested_by_name: str | None
    cycle_name: str | None
    message: str | None
    due_on: date
    questions: list[FeedbackQuestion]
    scale: FeedbackScale | None
    employee_visibility: FeedbackVisibility
    #: Plain words on who will read the answer, shown above the form.
    visibility_statement: str
    status: FeedbackReviewerStatus
    request_status: FeedbackRequestStatus
    competency_ratings: list[CompetencyAnswerIn]
    strengths: str | None
    improvements: str | None
    overall_rating: Decimal | None
    submitted_at: datetime | None


class FeedbackResponseSubmit(BaseModel):
    competency_ratings: list[CompetencyAnswerIn] = Field(default_factory=list, max_length=50)
    strengths: str | None = Field(default=None, max_length=4000)
    improvements: str | None = Field(default=None, max_length=4000)
    #: Optional, 1 to 5.
    overall_rating: Decimal | None = Field(default=None, ge=1, le=5, decimal_places=2)

    @model_validator(mode="after")
    def _says_something(self) -> FeedbackResponseSubmit:
        rated = any(c.rating is not None for c in self.competency_ratings)
        texts = [self.strengths, self.improvements, *(c.comment for c in self.competency_ratings)]
        written = any((text or "").strip() for text in texts)
        if not rated and not written and self.overall_rating is None:
            raise ValueError("Feedback needs at least a rating or a written answer.")
        return self


class FeedbackDecline(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


class FeedbackRemindResult(BaseModel):
    reminded: int


class PrivateFeedbackCreate(BaseModel):
    to_employee_id: uuid.UUID
    kind: PrivateFeedbackKind = PrivateFeedbackKind.APPRECIATION
    body: str = Field(min_length=1, max_length=4000)


class PrivateFeedbackRead(BaseModel):
    id: uuid.UUID
    from_employee_id: uuid.UUID
    from_name: str
    to_employee_id: uuid.UUID
    to_name: str
    kind: PrivateFeedbackKind
    body: str
    created_at: datetime
