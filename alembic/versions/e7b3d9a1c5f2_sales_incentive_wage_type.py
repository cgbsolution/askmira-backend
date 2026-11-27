"""Salary master: a SALES INCENTIVE wage type in every workspace with the standard set.

A sales incentive is a yearly figure the offer promises on top of the fixed
salary ("Annual fixed 12,00,000 + variable 4,00,000 = CTC 16,00,000"). It
counts in CTC TOTAL and is paid when earned, through Payroll > Adjustments;
payroll never pays it as a monthly line (`PAID_WHEN_EARNED_CODES`).

Workspaces that installed the standard wage types (they have the Variable
earning BONUS) get it now, with an audit row each; others get it when they
install the standard set.

Revision ID: e7b3d9a1c5f2
Revises: d5a8c2e4f6b1
Create Date: 2026-11-24
"""

from __future__ import annotations

from alembic import op

revision = "e7b3d9a1c5f2"
down_revision = "d5a8c2e4f6b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        WITH added AS (
            INSERT INTO salary_components (
                id, tenant_id, created_at, updated_at, code, name, component_type,
                calculation_type, base_component_code, value, formula, taxable, part_of_ctc,
                pro_rate_on_lop, counts_toward_pf, counts_toward_esi, display_order,
                wage_group, is_system, active
            )
            SELECT gen_random_uuid(), b.tenant_id, now(), now(), 'SALES_INCENTIVE',
                   'SALES INCENTIVE', 'EARNING', 'FIXED', NULL, 0, NULL, true, true,
                   false, false, false, 130, 'variable_earning', false, true
              FROM salary_components AS b
             WHERE b.code = 'BONUS'
               AND b.wage_group = 'variable_earning'
               AND NOT EXISTS (
                   SELECT 1 FROM salary_components AS s
                    WHERE s.tenant_id = b.tenant_id AND s.code = 'SALES_INCENTIVE'
               )
            RETURNING id, tenant_id
        )
        INSERT INTO audit_logs (
            id, tenant_id, created_at, updated_at, actor_user_id, actor_type, action,
            entity_type, entity_id, changes, summary
        )
        SELECT gen_random_uuid(), a.tenant_id, now(), now(), NULL, 'system', 'CREATE',
               'salary_component', a.id,
               jsonb_build_object(
                   'code', 'SALES_INCENTIVE', 'name', 'SALES INCENTIVE', 'standard_setup', true
               ),
               'Created salary component SALES_INCENTIVE (standard setup: in CTC, paid when earned)'
          FROM added AS a
        """
    )


def downgrade() -> None:
    # A wage type a salary uses stays; the audit log is insert-only.
    op.execute(
        """
        DELETE FROM salary_components AS c
         WHERE c.code = 'SALES_INCENTIVE'
           AND NOT EXISTS (
               SELECT 1 FROM employee_salary_components AS e
                 JOIN employee_salaries AS s ON s.id = e.employee_salary_id
                WHERE s.tenant_id = c.tenant_id AND e.component_code = 'SALES_INCENTIVE'
           )
        """
    )
