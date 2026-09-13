"""Scope QAgent Runtime idempotency keys by organization.

Revision ID: 0004_qagent_runtime_org_idempotency
Revises: 0003_qagent_runtime_org_scoping

PostgreSQL note (G0.7 fix):
    The previous implementation used
    ``op.batch_alter_table("qagent_runs", recreate="always")``. On PostgreSQL
    that mode performs a full table rebuild, which drops and recreates the
    table's primary key. Four child tables declare foreign keys that depend on
    ``qagent_runs_pkey`` (``qagent_run_events``, ``qagent_approvals``,
    ``qagent_recovery_points``, ``qagent_run_assets``), so the rebuild aborted
    with ``psycopg.errors.DependentObjectsStillExist``::

        cannot drop constraint qagent_runs_pkey on table qagent_runs
        because other objects depend on it

    Only the idempotency unique constraint needs to change; the primary key and
    every foreign key must stay untouched. PostgreSQL can swap a unique
    constraint in place, so this revision now issues plain ``ALTER TABLE`` DDL
    with no table recreation. The resulting schema matches the previously
    passing SQLite path, and ``downgrade`` stays exactly reversible.
"""

from __future__ import annotations

from alembic import op

revision = "0004_qagent_runtime_org_idempotency"
down_revision = "0003_qagent_runtime_org_scoping"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Replace the global idempotency unique constraint with an org-scoped one.

    No table recreate: the primary key and all foreign keys remain in place.
    """
    op.drop_constraint("uq_qagent_runs_idempotency_key", "qagent_runs", type_="unique")
    op.create_unique_constraint(
        "uq_qagent_runs_org_idempotency_key",
        "qagent_runs",
        ["org_id", "idempotency_key"],
    )


def downgrade() -> None:
    """Restore the legacy global idempotency unique constraint."""
    op.drop_constraint("uq_qagent_runs_org_idempotency_key", "qagent_runs", type_="unique")
    op.create_unique_constraint(
        "uq_qagent_runs_idempotency_key",
        "qagent_runs",
        ["idempotency_key"],
    )
