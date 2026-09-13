"""Scope QAgent Runtime idempotency keys by organization.

Revision ID: 0004_qagent_runtime_org_idempotency
Revises: 0003_qagent_runtime_org_scoping
"""

from __future__ import annotations

from alembic import op

revision = "0004_qagent_runtime_org_idempotency"
down_revision = "0003_qagent_runtime_org_scoping"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("qagent_runs", recreate="always") as batch_op:
        batch_op.drop_constraint("uq_qagent_runs_idempotency_key", type_="unique")
        batch_op.create_unique_constraint(
            "uq_qagent_runs_org_idempotency_key",
            ["org_id", "idempotency_key"],
        )


def downgrade() -> None:
    with op.batch_alter_table("qagent_runs", recreate="always") as batch_op:
        batch_op.drop_constraint("uq_qagent_runs_org_idempotency_key", type_="unique")
        batch_op.create_unique_constraint("uq_qagent_runs_idempotency_key", ["idempotency_key"])
