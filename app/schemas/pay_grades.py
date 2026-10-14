"""Grades and pay bands."""

from __future__ import annotations

import uuid
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MONEY = {"max_digits": 14, "decimal_places": 2, "ge": 0}


class PayGradeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_\-]+$")
    name: str = Field(min_length=1, max_length=128)
    level: int = Field(ge=0, le=1000)
    band_min: Decimal = Field(**MONEY)
    band_mid: Decimal = Field(**MONEY)
    band_max: Decimal = Field(**MONEY)

    @model_validator(mode="after")
    def _ordered(self) -> PayGradeCreate:
        if not (self.band_min <= self.band_mid <= self.band_max):
            raise ValueError("The band runs minimum ≤ midpoint ≤ maximum.")
        return self


class PayGradeUpdate(BaseModel):
    """The code is fixed: records carry it."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=128)
    level: int | None = Field(default=None, ge=0, le=1000)
    band_min: Decimal | None = Field(default=None, **MONEY)
    band_mid: Decimal | None = Field(default=None, **MONEY)
    band_max: Decimal | None = Field(default=None, **MONEY)
    active: bool | None = None


class PayGradeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    level: int
    band_min: Decimal
    band_mid: Decimal
    band_max: Decimal
    currency: str
    active: bool
    #: People on this grade now.
    headcount: int = 0
