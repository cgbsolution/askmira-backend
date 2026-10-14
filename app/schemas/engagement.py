"""Polls and company events."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.engagement import RsvpResponse


class AudienceIn(BaseModel):
    """Every list empty means everybody. The lists are and-ed."""

    model_config = ConfigDict(extra="forbid")

    department_ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)
    location_ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)
    grades: list[str] = Field(default_factory=list, max_length=100)


class PollCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=3, max_length=300)
    options: list[str] = Field(min_length=2, max_length=10)
    is_anonymous: bool = True
    closes_at: datetime | None = None
    audience: AudienceIn = Field(default_factory=AudienceIn)

    @field_validator("options")
    @classmethod
    def _options(cls, value: list[str]) -> list[str]:
        cleaned = [v.strip() for v in value]
        if any(not v or len(v) > 120 for v in cleaned):
            raise ValueError("Each option is 1 to 120 characters.")
        if len({v.lower() for v in cleaned}) != len(cleaned):
            raise ValueError("Each option must be different.")
        return cleaned


class PollVoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    option_index: int = Field(ge=0, le=9)


class EventCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=3, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    starts_at: datetime
    ends_at: datetime | None = None
    venue: str | None = Field(default=None, max_length=300)
    capacity: int | None = Field(default=None, ge=1, le=100_000)
    rsvp_by: datetime | None = None
    audience: AudienceIn = Field(default_factory=AudienceIn)

    @model_validator(mode="after")
    def _times(self) -> EventCreate:
        if self.ends_at is not None and self.ends_at < self.starts_at:
            raise ValueError("The event cannot end before it starts.")
        if self.rsvp_by is not None and self.rsvp_by > self.starts_at:
            raise ValueError("Answers must close before the event starts.")
        return self


class RsvpIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response: RsvpResponse
