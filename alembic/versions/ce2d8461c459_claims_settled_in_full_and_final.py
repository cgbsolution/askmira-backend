"""claims settled in full and final

A claim paid in a leaver's full and final settlement records the settlement,
so payroll never pays it again.

Revision ID: ce2d8461c459
Revises: f4d9e6a8b0c1
Create Date: 2026-10-15 01:26:07.915532
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = 'ce2d8461c459'
down_revision: str | None = 'f4d9e6a8b0c1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('expense_claims', sa.Column('settled_in_settlement_id', sa.UUID(), nullable=True))
    op.create_foreign_key('fk_expense_claims_settled_in_settlement_id', 'expense_claims', 'settlements', ['settled_in_settlement_id'], ['id'], ondelete='SET NULL')
    op.add_column('reimbursement_claims', sa.Column('settled_in_settlement_id', sa.UUID(), nullable=True))
    op.create_foreign_key('fk_reimbursement_claims_settled_in_settlement_id', 'reimbursement_claims', 'settlements', ['settled_in_settlement_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    op.drop_constraint('fk_reimbursement_claims_settled_in_settlement_id', 'reimbursement_claims', type_='foreignkey')
    op.drop_column('reimbursement_claims', 'settled_in_settlement_id')
    op.drop_constraint('fk_expense_claims_settled_in_settlement_id', 'expense_claims', type_='foreignkey')
    op.drop_column('expense_claims', 'settled_in_settlement_id')
