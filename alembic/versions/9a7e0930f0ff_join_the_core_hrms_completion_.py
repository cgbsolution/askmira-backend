"""Join the core HRMS completion workstreams

Attendance and sign-in (c07e5a1a0930 → c07e5a1a0931), leave depth, assets,
helpdesk, onboarding, the extended employee profile, payroll and expense money
flows, and performance conversations were built in parallel, each on its own
branch from ae8b1d4d874e. They touch disjoint tables and enum types, so the
order in which the branches apply does not matter; this revision only joins
them into one head.

Revision ID: 9a7e0930f0ff
Revises: 0b0a4d000930, 1ea7e0000930, a55e75000930, b31bd0000930, c07e5a1a0931,
    d15b09300a07, e4f009300b08, f40f11e00930
Create Date: 2026-09-30 04:19:03.914134
"""

from collections.abc import Sequence

revision: str = "9a7e0930f0ff"
down_revision: str | Sequence[str] | None = (
    "0b0a4d000930",
    "1ea7e0000930",
    "a55e75000930",
    "b31bd0000930",
    "c07e5a1a0931",
    "d15b09300a07",
    "e4f009300b08",
    "f40f11e00930",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
