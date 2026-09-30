"""employee profile and self-service: extended profile, change requests,
self-uploaded documents, self-resignation

Core HR ESS workstream.

- `employee_profiles`: one row per employee, one JSONB column per profile
  section (personal, addresses, emergency contacts, family, nominees,
  education, previous employment), the profile photo's storage key, and the
  HR-only `hiring_snapshot`.
- `profile_change_requests`: sensitive changes an employee proposes and HR
  decides, with the `profile_change_status` enum and a partial unique index
  allowing one pending request per section per person.
- `employee_documents`: verification status for documents the employee
  uploads themselves (`not_required` for everything HR uploaded).
- `exit_records`: who initiated the exit, the employee's comments, acceptance,
  and withdrawal. Existing exits were all opened by HR or a manager, so they
  are marked accepted as of their creation.

Data: hiring data that offer conversion used to write into
`employees.custom_fields` (source, offer code, candidate id, expected CTC, CTC
at offer, notice period at offer, fit score) is moved into
`employee_profiles.hiring_snapshot`, where no self-service read reaches it.
Only records carrying the conversion's own markers are touched.

Revision ID: f40f11e00930
Revises: ae8b1d4d874e
Create Date: 2026-09-30 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f40f11e00930"
down_revision: str | None = "ae8b1d4d874e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# SQLAlchemy stores enum member NAMES, as elsewhere in the schema.
_CHANGE_STATUS = postgresql.ENUM(
    "PENDING", "APPROVED", "REJECTED", "WITHDRAWN", name="profile_change_status"
)

_HIRING_KEYS = (
    "source",
    "offer_code",
    "candidate_id",
    "expected_ctc",
    "current_ctc_at_offer",
    "notice_period_days_at_offer",
    "fit_score",
)


def _base_columns() -> list[sa.Column]:
    return [
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def _jsonb(name: str, empty: str) -> sa.Column:
    return sa.Column(
        name,
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text(f"'{empty}'::jsonb"),
        nullable=False,
    )


def upgrade() -> None:
    bind = op.get_bind()
    _CHANGE_STATUS.create(bind, checkfirst=True)

    # ── employee_profiles ──────────────────────────────────────────────────
    op.create_table(
        "employee_profiles",
        *_base_columns(),
        sa.Column("employee_id", sa.UUID(), nullable=False),
        _jsonb("personal", "{}"),
        _jsonb("addresses", "{}"),
        _jsonb("emergency_contacts", "[]"),
        _jsonb("family_members", "[]"),
        _jsonb("nominees", "[]"),
        _jsonb("education", "[]"),
        _jsonb("previous_employment", "[]"),
        sa.Column("photo_storage_key", sa.String(length=160), nullable=True),
        _jsonb("hiring_snapshot", "{}"),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "employee_id", name="uq_employee_profile_employee"),
    )
    op.create_index(
        op.f("ix_employee_profiles_employee_id"), "employee_profiles", ["employee_id"]
    )
    op.create_index(op.f("ix_employee_profiles_tenant_id"), "employee_profiles", ["tenant_id"])

    # ── profile_change_requests ────────────────────────────────────────────
    op.create_table(
        "profile_change_requests",
        *_base_columns(),
        sa.Column("employee_id", sa.UUID(), nullable=False),
        sa.Column("section", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="profile_change_status", create_type=False),
            nullable=False,
        ),
        sa.Column("proposed", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("current_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("requested_by_user_id", sa.UUID(), nullable=True),
        sa.Column("decided_by_user_id", sa.UUID(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status = 'PENDING' OR decided_at IS NOT NULL",
            name="ck_profile_change_decided_has_time",
        ),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["decided_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_profile_change_requests_employee_id"),
        "profile_change_requests",
        ["employee_id"],
    )
    op.create_index(
        op.f("ix_profile_change_requests_status"), "profile_change_requests", ["status"]
    )
    op.create_index(
        op.f("ix_profile_change_requests_tenant_id"), "profile_change_requests", ["tenant_id"]
    )
    op.create_index(
        "uq_profile_change_one_pending",
        "profile_change_requests",
        ["tenant_id", "employee_id", "section"],
        unique=True,
        postgresql_where=sa.text("status = 'PENDING'"),
    )

    # ── employee_documents: verification ───────────────────────────────────
    op.add_column(
        "employee_documents",
        sa.Column(
            "verification_status",
            sa.String(length=16),
            server_default="not_required",
            nullable=False,
        ),
    )
    op.add_column(
        "employee_documents", sa.Column("verified_by_user_id", sa.UUID(), nullable=True)
    )
    op.add_column(
        "employee_documents",
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("employee_documents", sa.Column("verification_note", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_employee_documents_verified_by_user_id_users",
        "employee_documents",
        "users",
        ["verified_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        op.f("ix_employee_documents_verification_status"),
        "employee_documents",
        ["verification_status"],
    )

    # ── exit_records: self-resignation ─────────────────────────────────────
    op.add_column(
        "exit_records",
        sa.Column(
            "initiated_by_employee",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column("exit_records", sa.Column("employee_comments", sa.Text(), nullable=True))
    op.add_column(
        "exit_records", sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("exit_records", sa.Column("accepted_by_user_id", sa.UUID(), nullable=True))
    op.add_column(
        "exit_records", sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_foreign_key(
        "fk_exit_records_accepted_by_user_id_users",
        "exit_records",
        "users",
        ["accepted_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # Every exit so far was opened by HR or a manager: accepted when opened.
    op.execute("UPDATE exit_records SET accepted_at = created_at WHERE accepted_at IS NULL")

    # ── Move converted hires' pre-hire data out of custom_fields ───────────
    keys = ", ".join(f"'{key}'" for key in _HIRING_KEYS)
    op.execute(
        f"""
        INSERT INTO employee_profiles (id, tenant_id, employee_id, hiring_snapshot)
        SELECT gen_random_uuid(), e.tenant_id, e.id,
               COALESCE(
                   (SELECT jsonb_object_agg(kv.key, kv.value)
                      FROM jsonb_each(e.custom_fields) AS kv
                     WHERE kv.key IN ({keys})),
                   '{{}}'::jsonb
               )
          FROM employees e
         WHERE e.custom_fields ? 'offer_code' AND e.custom_fields ? 'candidate_id'
        ON CONFLICT (tenant_id, employee_id) DO NOTHING
        """
    )
    op.execute(
        f"""
        UPDATE employees
           SET custom_fields = custom_fields - ARRAY[{keys}]::text[]
         WHERE custom_fields ? 'offer_code' AND custom_fields ? 'candidate_id'
        """
    )


def downgrade() -> None:
    # Put the pre-hire data back where it came from before the table goes.
    op.execute(
        """
        UPDATE employees e
           SET custom_fields = e.custom_fields || p.hiring_snapshot
          FROM employee_profiles p
         WHERE p.employee_id = e.id AND p.hiring_snapshot <> '{}'::jsonb
        """
    )

    op.drop_constraint(
        "fk_exit_records_accepted_by_user_id_users", "exit_records", type_="foreignkey"
    )
    for column in (
        "withdrawn_at",
        "accepted_by_user_id",
        "accepted_at",
        "employee_comments",
        "initiated_by_employee",
    ):
        op.drop_column("exit_records", column)

    op.drop_index(
        op.f("ix_employee_documents_verification_status"), table_name="employee_documents"
    )
    op.drop_constraint(
        "fk_employee_documents_verified_by_user_id_users",
        "employee_documents",
        type_="foreignkey",
    )
    for column in (
        "verification_note",
        "verified_at",
        "verified_by_user_id",
        "verification_status",
    ):
        op.drop_column("employee_documents", column)

    op.drop_index("uq_profile_change_one_pending", table_name="profile_change_requests")
    op.drop_index(
        op.f("ix_profile_change_requests_tenant_id"), table_name="profile_change_requests"
    )
    op.drop_index(
        op.f("ix_profile_change_requests_status"), table_name="profile_change_requests"
    )
    op.drop_index(
        op.f("ix_profile_change_requests_employee_id"), table_name="profile_change_requests"
    )
    op.drop_table("profile_change_requests")

    op.drop_index(op.f("ix_employee_profiles_tenant_id"), table_name="employee_profiles")
    op.drop_index(op.f("ix_employee_profiles_employee_id"), table_name="employee_profiles")
    op.drop_table("employee_profiles")

    _CHANGE_STATUS.drop(op.get_bind(), checkfirst=True)
