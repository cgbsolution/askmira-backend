"""join the web-portal workstream branches

Eight workstreams built against the same base (`b62b0a40ce25`, slice 37), so
each left its own head and `alembic upgrade head` refused to run at all. Every
one of them creates its own tables, so there is nothing to reconcile here — the
merge exists only to give the history a single tip again.

Revision ID: ae8b1d4d874e
Revises: 3f7a2c91d0e4, 7c3e9a1d4b52, a5f2c91e7d30, c3a9e2f4d801, c7e1a93d4b20, d3f1a9c47e21, f8a1c3d5e7b9
Create Date: 2026-09-18 00:41:33.533538
"""

from collections.abc import Sequence


revision: str = "ae8b1d4d874e"
down_revision: str | Sequence[str] | None = (
    "3f7a2c91d0e4",  # payroll ops: clearance owners, loan rejection, arrear second approval
    "7c3e9a1d4b52",  # expense reports and receipts
    "a5f2c91e7d30",  # performance: goals, key results, PIP statements
    "c3a9e2f4d801",  # payroll run lifecycle and input lines
    "c7e1a93d4b20",  # culture: award posts, re-survey, announcement delivery
    "d3f1a9c47e21",  # hiring: candidate stage events, manual source
    "f8a1c3d5e7b9",  # platform: scheduler job runs, tenant SMTP
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Nothing to do — the branches touch disjoint tables."""


def downgrade() -> None:
    """Nothing to undo."""
