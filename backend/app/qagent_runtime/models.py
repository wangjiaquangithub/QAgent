"""SQLAlchemy Core table definitions for the QAgent Runtime source of truth."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

metadata = sa.MetaData()
_json = JSONB().with_variant(sa.JSON(), "sqlite")

qagent_runs = sa.Table(
    "qagent_runs",
    metadata,
    sa.Column("run_id", sa.String(64), primary_key=True),
    sa.Column("org_id", sa.String(255), nullable=False, server_default="local", index=True),
    sa.Column("task_id", sa.String(64), nullable=False, index=True),
    sa.Column("status", sa.String(32), nullable=False, index=True),
    sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    sa.Column("input_payload", _json, nullable=False),
    sa.Column("plan_payload", _json),
    sa.Column("result_payload", _json),
    sa.Column("error_payload", _json),
    sa.Column("recovery_context", _json),
    sa.Column("executor_key", sa.String(128), nullable=False, server_default="qagent.server.no_device.v1"),
    sa.Column("idempotency_key", sa.String(255)),
    sa.Column("resume_count", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("last_started_at", sa.DateTime(timezone=True)),
    sa.Column("execution_claim", sa.String(128)),
    sa.Column("execution_epoch", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("execution_claimed_at", sa.DateTime(timezone=True)),
    sa.Column("completed_at", sa.DateTime(timezone=True)),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint(
        "org_id",
        "idempotency_key",
        name="uq_qagent_runs_org_idempotency_key",
    ),
)

qagent_run_events = sa.Table(
    "qagent_run_events",
    metadata,
    sa.Column("event_id", sa.String(64), primary_key=True),
    sa.Column("run_id", sa.String(64), sa.ForeignKey("qagent_runs.run_id", ondelete="CASCADE"), nullable=False),
    sa.Column("sequence", sa.BigInteger(), nullable=False),
    sa.Column("type", sa.String(64), nullable=False),
    sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", _json, nullable=False),
    sa.UniqueConstraint("run_id", "sequence", name="uq_qagent_run_events_run_sequence"),
    sa.Index("ix_qagent_run_events_run_sequence", "run_id", "sequence"),
)

qagent_approvals = sa.Table(
    "qagent_approvals",
    metadata,
    sa.Column("approval_id", sa.String(64), primary_key=True),
    sa.Column("org_id", sa.String(255), nullable=False, server_default="local", index=True),
    sa.Column("run_id", sa.String(64), sa.ForeignKey("qagent_runs.run_id", ondelete="CASCADE"), nullable=False, index=True),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("decided_at", sa.DateTime(timezone=True)),
    sa.Column("decided_by", sa.String(255)),
    sa.Column("reason", sa.Text()),
)

qagent_recovery_points = sa.Table(
    "qagent_recovery_points",
    metadata,
    sa.Column("recovery_point_id", sa.String(64), primary_key=True),
    sa.Column("run_id", sa.String(64), sa.ForeignKey("qagent_runs.run_id", ondelete="CASCADE"), nullable=False, index=True),
    sa.Column("checkpoint_key", sa.String(128), nullable=False),
    sa.Column("sequence", sa.BigInteger(), nullable=False),
    sa.Column("context", _json, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint(
        "run_id",
        "checkpoint_key",
        name="uq_qagent_recovery_points_run_checkpoint",
    ),
)

qagent_run_assets = sa.Table(
    "qagent_run_assets",
    metadata,
    sa.Column("asset_id", sa.String(128), primary_key=True),
    sa.Column("run_id", sa.String(64), sa.ForeignKey("qagent_runs.run_id", ondelete="CASCADE"), nullable=False, index=True),
    sa.Column("asset_type", sa.String(64), nullable=False),
    sa.Column("uri", sa.Text(), nullable=False),
    sa.Column("name", sa.String(255)),
    sa.Column("content_type", sa.String(255)),
    sa.Column("metadata", _json, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)
