"""kiosk badges club posts recurring one on ones

Badges that punch at a kiosk, posts inside a club, and 1:1s that repeat.

Revision ID: d541c2908b0a
Revises: 35567c879698
Create Date: 2026-10-15 11:04:02.006831
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = 'd541c2908b0a'
down_revision: str | None = '35567c879698'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('club_posts',
    sa.Column('club_id', sa.UUID(), nullable=False),
    sa.Column('author_employee_id', sa.UUID(), nullable=True),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('tenant_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['author_employee_id'], ['employees.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['club_id'], ['clubs.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_club_posts_club', 'club_posts', ['tenant_id', 'club_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_club_posts_tenant_id'), 'club_posts', ['tenant_id'], unique=False)
    op.create_table('employee_badges',
    sa.Column('employee_id', sa.UUID(), nullable=False),
    sa.Column('badge_hash', sa.String(length=64), nullable=False),
    sa.Column('last_four', sa.String(length=4), nullable=False),
    sa.Column('issued_by_user_id', sa.UUID(), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('tenant_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], name='fk_employee_badges_employee_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['issued_by_user_id'], ['users.id'], name='fk_employee_badges_issued_by', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_employee_badges_employee_id'), 'employee_badges', ['employee_id'], unique=False)
    op.create_index(op.f('ix_employee_badges_tenant_id'), 'employee_badges', ['tenant_id'], unique=False)
    op.create_index('uq_employee_badges_active', 'employee_badges', ['tenant_id', 'badge_hash'], unique=True, postgresql_where=sa.text('revoked_at IS NULL'))
    op.add_column('one_on_ones', sa.Column('repeat_every_weeks', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('one_on_ones', 'repeat_every_weeks')
    op.drop_index('uq_employee_badges_active', table_name='employee_badges', postgresql_where=sa.text('revoked_at IS NULL'))
    op.drop_index(op.f('ix_employee_badges_tenant_id'), table_name='employee_badges')
    op.drop_index(op.f('ix_employee_badges_employee_id'), table_name='employee_badges')
    op.drop_table('employee_badges')
    op.drop_index(op.f('ix_club_posts_tenant_id'), table_name='club_posts')
    op.drop_index('ix_club_posts_club', table_name='club_posts')
    op.drop_table('club_posts')
