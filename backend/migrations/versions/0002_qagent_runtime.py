"""Add the PostgreSQL-backed QAgent Runtime vertical slice.

Revision ID: 0002_qagent_runtime
Revises: 0001_postgres_foundation
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002_qagent_runtime"
down_revision = "0001_postgres_foundation"
branch_labels = None
depends_on = None

_json = JSONB()


def upgrade() -> None:
    op.create_table(
        "qagent_runs",
        sa.Column("run_id", sa.String(length=64), primary_key=True),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("input_payload", _json, nullable=False),
        sa.Column("plan_payload", _json),
        sa.Column("result_payload", _json),
        sa.Column("error_payload", _json),
        sa.Column("recovery_context", _json),
        sa.Column("executor_key", sa.String(length=128), nullable=False, server_default="qagent.server.no_device.v1"),
        sa.Column("idempotency_key", sa.String(length=255)),
        sa.Column("resume_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_qagent_runs_idempotency_key"),
    )
    op.create_index("ix_qagent_runs_task_id", "qagent_runs", ["task_id"])
    op.create_index("ix_qagent_runs_status", "qagent_runs", ["status"])

    op.create_table(
        "qagent_run_events",
        sa.Column("event_id", sa.String(length=64), primary_key=True),
        sa.Column("run_id", sa.String(length=64), sa.ForeignKey("qagent_runs.run_id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", _json, nullable=False),
        sa.UniqueConstraint("run_id", "sequence", name="uq_qagent_run_events_run_sequence"),
    )
    op.create_index("ix_qagent_run_events_run_sequence", "qagent_run_events", ["run_id", "sequence"])

    op.create_table(
        "qagent_approvals",
        sa.Column("approval_id", sa.String(length=64), primary_key=True),
        sa.Column("run_id", sa.String(length=64), sa.ForeignKey("qagent_runs.run_id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("decided_by", sa.String(length=255)),
        sa.Column("reason", sa.Text()),
    )
    op.create_index("ix_qagent_approvals_run_id", "qagent_approvals", ["run_id"])

    op.create_table(
        "qagent_recovery_points",
        sa.Column("recovery_point_id", sa.String(length=64), primary_key=True),
        sa.Column("run_id", sa.String(length=64), sa.ForeignKey("qagent_runs.run_id", ondelete="CASCADE"), nullable=False),
        sa.Column("checkpoint_key", sa.String(length=128), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("context", _json, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "run_id",
            "checkpoint_key",
            name="uq_qagent_recovery_points_run_checkpoint",
        ),
    )
    op.create_index("ix_qagent_recovery_points_run_id", "qagent_recovery_points", ["run_id"])

    op.create_table(
        "qagent_run_assets",
        sa.Column("asset_id", sa.String(length=128), primary_key=True),
        sa.Column("run_id", sa.String(length=64), sa.ForeignKey("qagent_runs.run_id", ondelete="CASCADE"), nullable=False),
        sa.Column("asset_type", sa.String(length=64), nullable=False),
        sa.Column("uri", sa.Text(), nullable=False),
        sa.Column("name", sa.String(length=255)),
        sa.Column("content_type", sa.String(length=255)),
        sa.Column("metadata", _json, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_qagent_run_assets_run_id", "qagent_run_assets", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_qagent_run_assets_run_id", table_name="qagent_run_assets")
    op.drop_table("qagent_run_assets")
    op.drop_index("ix_qagent_recovery_points_run_id", table_name="qagent_recovery_points")
    op.drop_table("qagent_recovery_points")
    op.drop_index("ix_qagent_approvals_run_id", table_name="qagent_approvals")
    op.drop_table("qagent_approvals")
    op.drop_index("ix_qagent_run_events_run_sequence", table_name="qagent_run_events")
    op.drop_table("qagent_run_events")
    op.drop_index("ix_qagent_runs_status", table_name="qagent_runs")
    op.drop_index("ix_qagent_runs_task_id", table_name="qagent_runs")
    op.drop_table("qagent_runs")
