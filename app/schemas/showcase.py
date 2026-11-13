"""Showcase links (Platform → Showcase links) and a visitor's story."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.showcase.story import MAX_HEADCOUNT

Rupees = Annotated[Decimal, Field(gt=0, le=Decimal("1000000000"), max_digits=14, decimal_places=2)]


class _Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    client_name: str = Field(min_length=2, max_length=80)
    role_title: str = Field(min_length=2, max_length=60)
    headcount: int = Field(ge=1, le=MAX_HEADCOUNT)
    months: int = Field(ge=3, le=36)
    order_value: Rupees
    team_budget: Rupees
    laptop_price: Annotated[Decimal, Field(ge=1000, le=500000, max_digits=12, decimal_places=2)]
    payment_days: int = Field(ge=0, le=90)


class ShowcaseLinkCreate(_Scenario):
    label: str = Field(min_length=3, max_length=120)
    #: Whose name and logo the story wears. None: "AskMiraa".
    brand_tenant_id: uuid.UUID | None = None
    is_active: bool = True
    starts_at: datetime
    ends_at: datetime

    @model_validator(mode="after")
    def _check(self) -> ShowcaseLinkCreate:
        if self.team_budget >= self.order_value:
            raise ValueError(
                "The team budget must be less than the order value, or there is no margin."
            )
        if self.starts_at.tzinfo is None or self.ends_at.tzinfo is None:
            raise ValueError("Give the start and end with their time zone.")
        if self.ends_at <= self.starts_at:
            raise ValueError("The link must end after it starts.")
        if (self.ends_at - self.starts_at).days > 366:
            raise ValueError("A showcase link can run for a year at most.")
        return self


class ShowcaseLinkUpdate(BaseModel):
    """Any of the link's fields; the scenario's rules are checked on the result."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    label: str | None = Field(default=None, min_length=3, max_length=120)
    brand_tenant_id: uuid.UUID | None = None
    clear_tenant: bool = False
    client_name: str | None = Field(default=None, min_length=2, max_length=80)
    role_title: str | None = Field(default=None, min_length=2, max_length=60)
    headcount: int | None = Field(default=None, ge=1, le=MAX_HEADCOUNT)
    months: int | None = Field(default=None, ge=3, le=36)
    order_value: Rupees | None = None
    team_budget: Rupees | None = None
    laptop_price: (
        Annotated[Decimal, Field(ge=1000, le=500000, max_digits=12, decimal_places=2)] | None
    ) = None
    payment_days: int | None = Field(default=None, ge=0, le=90)
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
    brand_tenant_id: uuid.UUID | None
    company: str
    client_name: str
    role_title: str
    headcount: int
    months: int
    order_value: Decimal
    team_budget: Decimal
    margin_floor: Decimal
    laptop_price: Decimal
    payment_days: int
    is_active: bool
    starts_at: datetime
    ends_at: datetime
    visits: int
    last_visit_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ShowcaseSayIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=500)


class ShowcaseDecideIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str = Field(min_length=1, max_length=64)
    confirm: bool
