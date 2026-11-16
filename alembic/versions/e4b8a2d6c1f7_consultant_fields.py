"""Consultant fields on the person record, and when a workspace's payouts began.

TDS section, payee kind, GST rate and GSTIN, contract end, and who approves the
timesheets (the customer, or somebody inside). `tenants.consultant_payouts_from`
is the day payouts began: existing workspaces get today, so every week approved
before it stays out of the first payout run.

Revision ID: e4b8a2d6c1f7
Revises: d3a7f1c9e5b2
Create Date: 2026-11-16
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "e4b8a2d6c1f7"
down_revision = "d3a7f1c9e5b2"
branch_labels = None
depends_on = None

# Both types were made with the contractors tables; only this one is new.
_tds_section = postgresql.ENUM(
    "S194C", "S194J_PROFESSIONAL", "S194J_TECHNICAL", name="tds_section", create_type=False
)
_contractor_kind = postgresql.ENUM(
    "INDIVIDUAL", "HUF", "FIRM", "COMPANY", name="contractor_kind", create_type=False
)
_approver_route = postgresql.ENUM("CUSTOMER", "INTERNAL", name="approver_route", create_type=False)


def upgrade() -> None:
    _approver_route.create(op.get_bind(), checkfirst=True)
    op.add_column("employees", sa.Column("tds_section", _tds_section, nullable=True))
    op.add_column("employees", sa.Column("payee_kind", _contractor_kind, nullable=True))
    op.add_column("employees", sa.Column("gst_rate", sa.Numeric(5, 2), nullable=True))
    op.add_column("employees", sa.Column("gstin", sa.String(15), nullable=True))
    op.add_column("employees", sa.Column("contract_end_date", sa.Date(), nullable=True))
    op.add_column(
        "employees", sa.Column("timesheet_approver_route", _approver_route, nullable=True)
    )
    op.add_column("employees", sa.Column("customer_contact_name", sa.String(200), nullable=True))
    op.add_column("employees", sa.Column("customer_contact_email", sa.String(255), nullable=True))
    op.add_column(
        "employees",
        sa.Column(
            "internal_approver_user_id",
            sa.UUID(),
            sa.ForeignKey(
                "users.id", ondelete="SET NULL", name="fk_employees_internal_approver_user_id"
            ),
            nullable=True,
        ),
    )
    op.add_column("tenants", sa.Column("consultant_payouts_from", sa.Date(), nullable=True))
    op.execute("UPDATE tenants SET consultant_payouts_from = CURRENT_DATE")


def downgrade() -> None:
    op.drop_column("tenants", "consultant_payouts_from")
    op.drop_column("employees", "internal_approver_user_id")
    op.drop_column("employees", "customer_contact_email")
    op.drop_column("employees", "customer_contact_name")
    op.drop_column("employees", "timesheet_approver_route")
    op.drop_column("employees", "contract_end_date")
    op.drop_column("employees", "gstin")
    op.drop_column("employees", "gst_rate")
    op.drop_column("employees", "payee_kind")
    op.drop_column("employees", "tds_section")
    sa.Enum(name="approver_route").drop(op.get_bind(), checkfirst=True)
