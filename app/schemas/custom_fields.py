"""Custom field definitions on the employee record (FS-CHR-01)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

FieldType = Literal["text", "number", "date", "select", "multiselect", "boolean"]

#: Lower-case words joined by underscores: it becomes a JSON key on every record.
KEY = r"^[a-z][a-z0-9_]{1,63}$"


def _options_fit(field_type: str | None, options: list[str] | None) -> None:
    if field_type in ("select", "multiselect"):
        if not options:
            raise ValueError("A choice field needs at least one option.")
        if len({o.strip().lower() for o in options}) != len(options):
            raise ValueError("Each option must be different.")
    elif options:
        raise ValueError("Only a choice field has options.")


#: Tab names a custom field or table may not take: the profile's own tabs.
RESERVED_TABS = frozenset(
    {"overview", "personal", "family", "statutory", "employment", "compensation", "documents",
     "lifecycle", "timeline", "attendance", "assets"}
)


def clean_tab(value: str | None) -> str | None:
    """A tab name as stored: trimmed, single-spaced, or None for the Employment tab."""
    if value is None:
        return None
    name = " ".join(value.split())
    if not name:
        return None
    if name.lower() in RESERVED_TABS:
        raise ValueError(f"'{name}' is one of the profile's own tabs; choose another name.")
    return name


class CustomFieldCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=KEY)
    label: str = Field(min_length=1, max_length=255)
    field_type: FieldType
    options: list[str] | None = Field(default=None, max_length=100)
    is_required: bool = False
    is_sensitive: bool = False
    display_order: int = Field(default=0, ge=0, le=10_000)
    #: A profile tab of the admin's own, by name; empty for the Employment tab.
    tab: str | None = Field(default=None, max_length=40)

    @model_validator(mode="after")
    def _options(self) -> CustomFieldCreate:
        self.tab = clean_tab(self.tab)
        if self.options is not None:
            self.options = [o.strip() for o in self.options if o.strip()]
        _options_fit(self.field_type, self.options)
        return self


class CustomFieldUpdate(BaseModel):
    """The key and type are fixed once created: records already hold values
    under that key, in that shape."""

    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, min_length=1, max_length=255)
    options: list[str] | None = Field(default=None, max_length=100)
    is_required: bool | None = None
    is_sensitive: bool | None = None
    display_order: int | None = Field(default=None, ge=0, le=10_000)
    is_active: bool | None = None
    #: An empty string moves the field back to the Employment tab.
    tab: str | None = Field(default=None, max_length=40)


class CustomFieldRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    key: str
    label: str
    field_type: FieldType
    options: list[str] | None
    is_required: bool
    is_sensitive: bool
    display_order: int
    is_active: bool
    tab: str | None = None
    created_at: datetime | None = None
