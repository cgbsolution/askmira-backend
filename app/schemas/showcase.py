"""Showcase links (Platform → Showcase links) and a visitor's Ask Miraa chat."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: A stand PIN: digits only, typed by the team at the stand.
Pin = Field(default=None, pattern=r"^\d{4,8}$")


class ShowcaseLinkCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    label: str = Field(min_length=3, max_length=120)
    #: The workspace whose Ask Miraa the link opens.
    workspace_id: uuid.UUID
    #: The admin of that workspace the chat answers as.
    acting_user_id: uuid.UUID
    pin: str | None = Pin
    daily_messages: int = Field(default=300, ge=10, le=5000)
    is_active: bool = True
    starts_at: datetime
    ends_at: datetime

    @model_validator(mode="after")
    def _check(self) -> ShowcaseLinkCreate:
        if self.starts_at.tzinfo is None or self.ends_at.tzinfo is None:
            raise ValueError("Give the start and end with their time zone.")
        if self.ends_at <= self.starts_at:
            raise ValueError("The link must end after it starts.")
        if (self.ends_at - self.starts_at).days > 366:
            raise ValueError("A showcase link can run for a year at most.")
        return self


class ShowcaseLinkUpdate(BaseModel):
    """Any of the link's fields but its workspace: the QR belongs to that workspace.

    The window's rules are checked on the result."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    label: str | None = Field(default=None, min_length=3, max_length=120)
    acting_user_id: uuid.UUID | None = None
    pin: str | None = Pin
    #: True: the scan alone opens the chat again.
    clear_pin: bool = False
    daily_messages: int | None = Field(default=None, ge=10, le=5000)
    is_active: bool | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None


class ShowcaseLinkRead(BaseModel):
    id: uuid.UUID
    label: str
    url: str
    code: str
    #: "live", "scheduled", "ended" or "off".
    status: Literal["live", "scheduled", "ended", "off"]
    workspace_id: uuid.UUID
    company: str
    acting_user_id: uuid.UUID
    acting_name: str
    acting_email: str
    pin: str | None
    daily_messages: int
    messages_today: int
    is_active: bool
    starts_at: datetime
    ends_at: datetime
    visits: int
    last_visit_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ShowcaseStartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pin: str | None = Field(default=None, max_length=16)


class ShowcasePickIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str = Field(min_length=1, max_length=64)
    inputs: dict[str, str] = Field(default_factory=dict, max_length=8)


class ShowcaseSayIn(BaseModel):
    """A visitor's message. Never a conversation id: the run decides which one."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(default="", max_length=2000)
    pick: ShowcasePickIn | None = None
    client_ref: str | None = Field(default=None, min_length=8, max_length=64, pattern=r"^[\w-]+$")
