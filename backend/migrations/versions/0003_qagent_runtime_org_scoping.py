"""Add organization scope to QAgent Runtime runs and approvals.

Revision ID: 0003_qagent_runtime_org_scoping
Revises: 0002_qagent_runtime
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_qagent_runtime_org_scoping"
down_revision = "0002_qagent_runtime"
branch_labels = None
depends_on = None


def _add_scoped_column(table_name: str) -> None:
    op.add_column(
        table_name,
        sa.Column("org_id", sa.String(length=255), nullable=True, server_default="local"),
    )
    op.execute(sa.text(f"UPDATE {table_name} SET org_id = 'local' WHERE org_id IS NULL"))
    op.alter_column(table_name, "org_id", existing_type=sa.String(length=255), nullable=False)
    op.create_index(f"ix_{table_name}_org_id", table_name, ["org_id"])


def upgrade() -> None:
    _add_scoped_column("qagent_runs")
    _add_scoped_column("qagent_approvals")


def downgrade() -> None:
    op.drop_index("ix_qagent_approvals_org_id", table_name="qagent_approvals")
    op.drop_column("qagent_approvals", "org_id")
    op.drop_index("ix_qagent_runs_org_id", table_name="qagent_runs")
    op.drop_column("qagent_runs", "org_id")
