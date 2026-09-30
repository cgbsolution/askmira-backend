"""asset management: categories, register, assignments, requests, exit recovery link

Four tables for the asset register, and one nullable column on
`clearance_items` linking an exit's "return this asset" item to the assignment
it stands for — which is how an unreturned asset's book value reaches the
settlement's existing "Unreturned assets" line.

Revision ID: a55e75000930
Revises: ae8b1d4d874e
Create Date: 2026-09-30 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a55e75000930"
down_revision: str | None = "ae8b1d4d874e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Created once with checkfirst and dropped explicitly in downgrade(), so the
# migration round-trips. SQLAlchemy stores enum member NAMES, as elsewhere.
_ASSET_STATUS = postgresql.ENUM(
    "IN_STOCK", "ASSIGNED", "IN_REPAIR", "LOST", "RETIRED", "DISPOSED", name="asset_status"
)
_ASSET_CONDITION = postgresql.ENUM("NEW", "GOOD", "FAIR", "DAMAGED", name="asset_condition")
_OUTCOME = postgresql.ENUM("RETURNED", "LOST", "WRITTEN_OFF", name="asset_assignment_outcome")
_REQUEST_STATUS = postgresql.ENUM(
    "PENDING", "APPROVED", "REJECTED", "FULFILLED", "CANCELLED", name="asset_request_status"
)


def _enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(name=name, create_type=False)


def _base() -> list[sa.Column]:
    return [
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    bind = op.get_bind()
    for enum in (_ASSET_STATUS, _ASSET_CONDITION, _OUTCOME, _REQUEST_STATUS):
        enum.create(bind, checkfirst=True)

    op.create_table(
        "asset_categories",
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("custodian_user_id", sa.UUID(), nullable=True),
        sa.Column("depreciation_months", sa.Integer(), nullable=True),
        sa.Column(
            "requires_acknowledgement", sa.Boolean(), server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        *_base(),
        sa.CheckConstraint(
            "depreciation_months IS NULL OR depreciation_months > 0",
            name="ck_asset_category_depreciation_positive",
        ),
        sa.ForeignKeyConstraint(["custodian_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "code", name="uq_asset_categories_tenant_code"),
    )
    op.create_index(op.f("ix_asset_categories_tenant_id"), "asset_categories", ["tenant_id"])

    op.create_table(
        "assets",
        sa.Column("category_id", sa.UUID(), nullable=False),
        sa.Column("asset_tag", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("serial_number", sa.String(length=128), nullable=True),
        sa.Column("make", sa.String(length=80), nullable=True),
        sa.Column("model", sa.String(length=120), nullable=True),
        sa.Column("specs", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("purchase_date", sa.Date(), nullable=True),
        sa.Column("purchase_cost", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("vendor", sa.String(length=160), nullable=True),
        sa.Column("invoice_number", sa.String(length=64), nullable=True),
        sa.Column("warranty_until", sa.Date(), nullable=True),
        sa.Column("location_id", sa.UUID(), nullable=True),
        sa.Column("status", _enum("asset_status"), nullable=False),
        sa.Column("condition", _enum("asset_condition"), nullable=False),
        sa.Column("status_reason", sa.Text(), nullable=True),
        sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        *_base(),
        sa.CheckConstraint(
            "purchase_cost IS NULL OR purchase_cost >= 0", name="ck_asset_cost_non_negative"
        ),
        sa.ForeignKeyConstraint(
            ["category_id"], ["asset_categories.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["location_id"], ["locations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "asset_tag", name="uq_assets_tenant_tag"),
    )
    op.create_index(op.f("ix_assets_tenant_id"), "assets", ["tenant_id"])
    op.create_index(op.f("ix_assets_category_id"), "assets", ["category_id"])
    op.create_index(op.f("ix_assets_serial_number"), "assets", ["serial_number"])
    op.create_index(op.f("ix_assets_status"), "assets", ["status"])

    op.create_table(
        "asset_requests",
        sa.Column("employee_id", sa.UUID(), nullable=False),
        sa.Column("category_id", sa.UUID(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("needed_by", sa.Date(), nullable=True),
        sa.Column("status", _enum("asset_request_status"), nullable=False),
        sa.Column("requested_by_user_id", sa.UUID(), nullable=True),
        sa.Column("decided_by_user_id", sa.UUID(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("fulfilled_asset_id", sa.UUID(), nullable=True),
        sa.Column("fulfilled_by_user_id", sa.UUID(), nullable=True),
        sa.Column("fulfilled_at", sa.DateTime(timezone=True), nullable=True),
        *_base(),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["category_id"], ["asset_categories.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["decided_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["fulfilled_asset_id"], ["assets.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["fulfilled_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_asset_requests_tenant_id"), "asset_requests", ["tenant_id"])
    op.create_index(op.f("ix_asset_requests_employee_id"), "asset_requests", ["employee_id"])
    op.create_index(op.f("ix_asset_requests_status"), "asset_requests", ["status"])

    op.create_table(
        "asset_assignments",
        sa.Column("asset_id", sa.UUID(), nullable=False),
        sa.Column("employee_id", sa.UUID(), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("assigned_by_user_id", sa.UUID(), nullable=True),
        sa.Column("expected_return_date", sa.Date(), nullable=True),
        sa.Column("condition_out", _enum("asset_condition"), nullable=False),
        sa.Column("assignment_notes", sa.Text(), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("returned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("returned_to_user_id", sa.UUID(), nullable=True),
        sa.Column("condition_in", _enum("asset_condition"), nullable=True),
        sa.Column("return_notes", sa.Text(), nullable=True),
        sa.Column("recovery_amount", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("exit_record_id", sa.UUID(), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", _enum("asset_assignment_outcome"), nullable=True),
        sa.Column("return_reminder_sent_at", sa.DateTime(timezone=True), nullable=True),
        *_base(),
        sa.CheckConstraint(
            "recovery_amount IS NULL OR recovery_amount >= 0",
            name="ck_asset_assignment_recovery_non_negative",
        ),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["assigned_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["returned_to_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["exit_record_id"], ["exit_records.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_asset_assignments_tenant_id"), "asset_assignments", ["tenant_id"])
    op.create_index(op.f("ix_asset_assignments_asset_id"), "asset_assignments", ["asset_id"])
    op.create_index(
        op.f("ix_asset_assignments_employee_id"), "asset_assignments", ["employee_id"]
    )
    # One open assignment per asset: a laptop is never with two people at once.
    op.create_index(
        "uq_asset_assignment_open",
        "asset_assignments",
        ["asset_id"],
        unique=True,
        postgresql_where=sa.text("closed_at IS NULL"),
    )

    op.add_column("clearance_items", sa.Column("asset_assignment_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_clearance_items_asset_assignment_id",
        "clearance_items",
        "asset_assignments",
        ["asset_assignment_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        op.f("ix_clearance_items_asset_assignment_id"), "clearance_items", ["asset_assignment_id"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_clearance_items_asset_assignment_id"), table_name="clearance_items")
    op.drop_constraint(
        "fk_clearance_items_asset_assignment_id", "clearance_items", type_="foreignkey"
    )
    op.drop_column("clearance_items", "asset_assignment_id")

    op.drop_index("uq_asset_assignment_open", table_name="asset_assignments")
    op.drop_index(op.f("ix_asset_assignments_employee_id"), table_name="asset_assignments")
    op.drop_index(op.f("ix_asset_assignments_asset_id"), table_name="asset_assignments")
    op.drop_index(op.f("ix_asset_assignments_tenant_id"), table_name="asset_assignments")
    op.drop_table("asset_assignments")

    op.drop_index(op.f("ix_asset_requests_status"), table_name="asset_requests")
    op.drop_index(op.f("ix_asset_requests_employee_id"), table_name="asset_requests")
    op.drop_index(op.f("ix_asset_requests_tenant_id"), table_name="asset_requests")
    op.drop_table("asset_requests")

    op.drop_index(op.f("ix_assets_status"), table_name="assets")
    op.drop_index(op.f("ix_assets_serial_number"), table_name="assets")
    op.drop_index(op.f("ix_assets_category_id"), table_name="assets")
    op.drop_index(op.f("ix_assets_tenant_id"), table_name="assets")
    op.drop_table("assets")

    op.drop_index(op.f("ix_asset_categories_tenant_id"), table_name="asset_categories")
    op.drop_table("asset_categories")

    bind = op.get_bind()
    for enum in (_REQUEST_STATUS, _OUTCOME, _ASSET_CONDITION, _ASSET_STATUS):
        enum.drop(bind, checkfirst=True)
