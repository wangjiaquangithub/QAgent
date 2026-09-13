"""Add durable execution claim fields for QAgent Runtime."""
import sqlalchemy as sa
from alembic import op

revision = "0005_qagent_runtime_execution_claim"
down_revision = "0004_qagent_runtime_org_idempotency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("qagent_runs", sa.Column("execution_claim", sa.String(length=128), nullable=True))
    op.add_column("qagent_runs", sa.Column("execution_epoch", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("qagent_runs", sa.Column("execution_claimed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("qagent_runs", "execution_claimed_at")
    op.drop_column("qagent_runs", "execution_epoch")
    op.drop_column("qagent_runs", "execution_claim")
