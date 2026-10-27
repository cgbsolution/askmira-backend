"""Demo requests from the marketing website.

The limits here match the website's own form schema
(website/src/lib/demo-schema.ts): the browser checks as people type, the
website's server checks again, and this API checks a third time, because it is
reachable by anyone.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.demo_request import DemoRequestStatus

__all__ = [
    "COMPANY_SIZES",
    "INTERESTS",
    "ROLES",
    "TOPICS",
    "DemoRequestCreate",
    "DemoRequestRead",
    "DemoRequestUpdate",
]

COMPANY_SIZES = ("1-50", "51-200", "201-500", "501-1,000", "1,000+")
ROLES = ("HR or People", "Finance or Payroll", "Founder or Leadership", "IT", "Other")
INTERESTS = ("payroll", "attendance", "core_hr", "hiring", "performance", "engagement", "ai")
TOPICS = ("demo", "pricing", "partnership", "other")

#: Space, full stop, straight and curly apostrophes, hyphen.
_NAME_PUNCTUATION = frozenset(" .'\u2019-")
_PHONE = re.compile(r"^\+?[\d\s()-]+$")
#: Control characters other than ordinary line breaks and tabs.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _is_name(value: str) -> bool:
    """Starts with a letter; then letters in any script (with their vowel signs and
    accents), spaces, full stops, apostrophes and hyphens. "D'Souza", "Anne-Marie"
    and "प्रिया" all pass."""
    if not value or not unicodedata.category(value[0]).startswith("L"):
        return False
    return all(unicodedata.category(ch)[0] in ("L", "M") or ch in _NAME_PUNCTUATION for ch in value)


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = _CONTROL.sub("", value).strip()
    return value or None


class DemoRequestCreate(BaseModel):
    """What the website sends. Unknown fields are refused."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    full_name: str = Field(min_length=2, max_length=100)
    work_email: EmailStr = Field(max_length=254)
    company: str = Field(min_length=2, max_length=120)
    phone: str | None = Field(default=None, max_length=20)
    company_size: Literal["1-50", "51-200", "201-500", "501-1,000", "1,000+"]
    role: (
        Literal["HR or People", "Finance or Payroll", "Founder or Leadership", "IT", "Other"] | None
    ) = None
    interests: list[
        Literal["payroll", "attendance", "core_hr", "hiring", "performance", "engagement", "ai"]
    ] = Field(default_factory=list, max_length=len(INTERESTS))
    message: str | None = Field(default=None, max_length=1000)
    topic: Literal["demo", "pricing", "partnership", "other"] = "demo"
    consent: bool
    source_path: str | None = Field(default=None, max_length=200)
    #: The website's hidden field. A person never fills it in.
    website: str | None = Field(default=None, max_length=200)

    @field_validator("full_name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = _clean(value) or ""
        if not _is_name(value):
            raise ValueError("Use letters only — no numbers or symbols.")
        return value

    @field_validator("company", "message", "source_path")
    @classmethod
    def _text(cls, value: str | None) -> str | None:
        return _clean(value)

    @field_validator("phone")
    @classmethod
    def _phone(cls, value: str | None) -> str | None:
        value = _clean(value)
        if value is None:
            return None
        digits = sum(ch.isdigit() for ch in value)
        if not _PHONE.match(value) or not 8 <= digits <= 15:
            raise ValueError("Use 8 to 15 digits, with an optional + at the start.")
        return value

    @field_validator("interests")
    @classmethod
    def _unique(cls, value: list[str]) -> list[str]:
        # Keep the order of the form, drop repeats.
        return list(dict.fromkeys(value))

    @field_validator("consent")
    @classmethod
    def _consented(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("Agree to be contacted so that we can reply to your request.")
        return value


class DemoRequestUpdate(BaseModel):
    """What the platform team changes while following up."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    status: DemoRequestStatus | None = None
    notes: str | None = Field(default=None, max_length=4000)


class DemoRequestRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
    full_name: str
    work_email: str
    company: str
    phone: str | None
    company_size: str
    role: str | None
    interests: list[str]
    message: str | None
    topic: str
    source_path: str | None
    status: DemoRequestStatus
    notes: str | None
    handled_by_name: str | None = None
    handled_at: datetime | None
