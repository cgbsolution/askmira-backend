"""slice 37 BI connections, export audit, benchmark consent, contributions and datasets

Revision ID: b62b0a40ce25
Revises: 1699f6aca180
Create Date: 2026-09-07 23:09:52.517842
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'b62b0a40ce25'
down_revision: str | None = '1699f6aca180'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Autogenerate emits the enum types inline, which creates them on upgrade and
# leaves them behind on downgrade — so the second upgrade fails with "type
# already exists". Created once here with checkfirst and dropped explicitly in
# downgrade(), the migration round-trips.
_BENCHMARK_SOURCE = postgresql.ENUM('OWN_CLIENT_BASE', 'LICENSED', name='benchmark_source')
_BI_DESTINATION_KIND = postgresql.ENUM('POSTGRES_ROLE', 'OBJECT_STORAGE', name='bi_destination_kind')
_BI_EXPORT_KIND = postgresql.ENUM('INCREMENTAL', 'FULL', name='bi_export_kind')
_BI_EXPORT_STATUS = postgresql.ENUM('RUNNING', 'SUCCEEDED', 'FAILED', name='bi_export_status')

_ENUM_TYPES = (
    _BENCHMARK_SOURCE,
    _BI_DESTINATION_KIND,
    _BI_EXPORT_KIND,
    _BI_EXPORT_STATUS,
)


def upgrade() -> None:
    bind = op.get_bind()
    for enum_type in _ENUM_TYPES:
        enum_type.create(bind, checkfirst=True)

    op.create_table('benchmark_datasets',
    sa.Column('metric', sa.String(length=64), nullable=False),
    sa.Column('dimension_key', sa.String(length=200), nullable=False),
    sa.Column('period', sa.Date(), nullable=False),
    sa.Column('source', postgresql.ENUM('OWN_CLIENT_BASE', 'LICENSED', name='benchmark_source', create_type=False), nullable=False),
    sa.Column('contributors', sa.Integer(), nullable=False),
    sa.Column('published', sa.Boolean(), nullable=False),
    sa.Column('p25', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('p50', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('p75', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('note', sa.Text(), nullable=False),
    sa.Column('computed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('NOT published OR (p25 IS NOT NULL AND p50 IS NOT NULL AND p75 IS NOT NULL)', name='ck_benchmark_datasets_published_cells_are_complete'),
    sa.CheckConstraint('contributors >= 0', name='ck_benchmark_datasets_contributors'),
    sa.CheckConstraint('published OR (p25 IS NULL AND p50 IS NULL AND p75 IS NULL)', name='ck_benchmark_datasets_suppressed_cells_are_empty'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('metric', 'dimension_key', 'period', 'source', name='uq_benchmark_datasets_cell')
    )
    op.create_index('ix_benchmark_datasets_lookup', 'benchmark_datasets', ['metric', 'dimension_key', 'period'], unique=False)
    op.create_table('benchmark_contributions',
    sa.Column('metric', sa.String(length=64), nullable=False),
    sa.Column('dimension_key', sa.String(length=200), nullable=False),
    sa.Column('period', sa.Date(), nullable=False),
    sa.Column('value', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('employee_count', sa.Integer(), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('tenant_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('employee_count > 0', name='ck_benchmark_contributions_employee_count'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'metric', 'dimension_key', 'period', name='uq_benchmark_contributions_cell')
    )
    op.create_index('ix_benchmark_contributions_cell', 'benchmark_contributions', ['metric', 'dimension_key', 'period'], unique=False)
    op.create_index(op.f('ix_benchmark_contributions_tenant_id'), 'benchmark_contributions', ['tenant_id'], unique=False)
    op.create_table('benchmark_consents',
    sa.Column('is_contributor', sa.Boolean(), nullable=False),
    sa.Column('contract_reference', sa.String(length=200), nullable=True),
    sa.Column('accepted_by_user_id', sa.UUID(), nullable=True),
    sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_by_user_id', sa.UUID(), nullable=True),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('tenant_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('NOT is_contributor OR accepted_at IS NOT NULL', name='ck_benchmark_consents_accepted_when_contributing'),
    sa.CheckConstraint('NOT is_contributor OR revoked_at IS NULL', name='ck_benchmark_consents_not_contributing_after_revocation'),
    sa.ForeignKeyConstraint(['accepted_by_user_id'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['revoked_by_user_id'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', name='uq_benchmark_consents_tenant')
    )
    op.create_index(op.f('ix_benchmark_consents_tenant_id'), 'benchmark_consents', ['tenant_id'], unique=False)
    op.create_table('bi_connections',
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('entity_name', sa.String(length=64), nullable=False),
    sa.Column('destination', postgresql.ENUM('POSTGRES_ROLE', 'OBJECT_STORAGE', name='bi_destination_kind', create_type=False), nullable=False),
    sa.Column('destination_ref', sa.String(length=500), nullable=False),
    sa.Column('export_role', sa.String(length=50), nullable=False),
    sa.Column('field_names', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('schema_version', sa.String(length=16), nullable=False),
    sa.Column('last_watermark', sa.DateTime(timezone=True), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_by_user_id', sa.UUID(), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('tenant_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('length(trim(destination_ref)) > 0', name='ck_bi_connections_destination_ref_present'),
    sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'name', name='uq_bi_connections_tenant_name')
    )
    op.create_index('ix_bi_connections_tenant_active', 'bi_connections', ['tenant_id', 'is_active'], unique=False)
    op.create_index(op.f('ix_bi_connections_tenant_id'), 'bi_connections', ['tenant_id'], unique=False)
    op.create_table('bi_export_runs',
    sa.Column('connection_id', sa.UUID(), nullable=True),
    sa.Column('entity_name', sa.String(length=64), nullable=False),
    sa.Column('kind', postgresql.ENUM('INCREMENTAL', 'FULL', name='bi_export_kind', create_type=False), nullable=False),
    sa.Column('destination', postgresql.ENUM('POSTGRES_ROLE', 'OBJECT_STORAGE', name='bi_destination_kind', create_type=False), nullable=False),
    sa.Column('destination_ref', sa.String(length=500), nullable=False),
    sa.Column('export_role', sa.String(length=50), nullable=False),
    sa.Column('schema_version', sa.String(length=16), nullable=False),
    sa.Column('since', sa.DateTime(timezone=True), nullable=True),
    sa.Column('next_watermark', sa.DateTime(timezone=True), nullable=True),
    sa.Column('row_count', sa.Integer(), nullable=False),
    sa.Column('field_names', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('field_count', sa.Integer(), nullable=False),
    sa.Column('withheld_fields', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('carries_payroll_data', sa.Boolean(), nullable=False),
    sa.Column('status', postgresql.ENUM('RUNNING', 'SUCCEEDED', 'FAILED', name='bi_export_status', create_type=False), nullable=False),
    sa.Column('failure_reason', sa.Text(), nullable=True),
    sa.Column('initiated_by_user_id', sa.UUID(), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('tenant_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('field_count > 0', name='ck_bi_export_runs_has_fields'),
    sa.CheckConstraint('row_count >= 0', name='ck_bi_export_runs_row_count'),
    sa.ForeignKeyConstraint(['connection_id'], ['bi_connections.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['initiated_by_user_id'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_bi_export_runs_connection_id'), 'bi_export_runs', ['connection_id'], unique=False)
    op.create_index(op.f('ix_bi_export_runs_tenant_id'), 'bi_export_runs', ['tenant_id'], unique=False)
    op.create_index('ix_bi_export_runs_tenant_payroll', 'bi_export_runs', ['tenant_id', 'carries_payroll_data'], unique=False)
    op.create_index('ix_bi_export_runs_tenant_started', 'bi_export_runs', ['tenant_id', 'started_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_bi_export_runs_tenant_started', table_name='bi_export_runs')
    op.drop_index('ix_bi_export_runs_tenant_payroll', table_name='bi_export_runs')
    op.drop_index(op.f('ix_bi_export_runs_tenant_id'), table_name='bi_export_runs')
    op.drop_index(op.f('ix_bi_export_runs_connection_id'), table_name='bi_export_runs')
    op.drop_table('bi_export_runs')
    op.drop_index(op.f('ix_bi_connections_tenant_id'), table_name='bi_connections')
    op.drop_index('ix_bi_connections_tenant_active', table_name='bi_connections')
    op.drop_table('bi_connections')
    op.drop_index(op.f('ix_benchmark_consents_tenant_id'), table_name='benchmark_consents')
    op.drop_table('benchmark_consents')
    op.drop_index(op.f('ix_benchmark_contributions_tenant_id'), table_name='benchmark_contributions')
    op.drop_index('ix_benchmark_contributions_cell', table_name='benchmark_contributions')
    op.drop_table('benchmark_contributions')
    op.drop_index('ix_benchmark_datasets_lookup', table_name='benchmark_datasets')
    op.drop_table('benchmark_datasets')

    bind = op.get_bind()
    for enum_type in _ENUM_TYPES:
        enum_type.drop(bind, checkfirst=True)
