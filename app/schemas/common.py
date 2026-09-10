"""Shared response envelopes."""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """A page of results.

    `total` is the count before pagination, so a table can render "showing
    1-25 of 312" without a second request.
    """

    items: list[T]
    total: int
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=200)

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.page_size))


class PaginationParams(BaseModel):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=25, ge=1, le=200)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


class Message(BaseModel):
    detail: str
