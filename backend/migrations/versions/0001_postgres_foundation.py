"""Create the PostgreSQL foundation probe table.

This is deliberately not a Run, Task, session, or other product table. It is a
small, disposable verification table proving that a clean PostgreSQL database
can be initialized and managed by the migration tool.
"""

import sqlalchemy as sa
from alembic import op

revision = "0001_postgres_foundation"
down_revision = None
branch_labels = None
depends_on = None


_TABLE = "qagent_migration_probe"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("marker", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name="ck_qagent_migration_probe_singleton"),
        sa.PrimaryKeyConstraint("id", name="pk_qagent_migration_probe"),
        comment="Infrastructure-only migration verification table; not a business table.",
    )
    op.execute(sa.text("INSERT INTO qagent_migration_probe (id, marker) VALUES (1, 'foundation-ready') ON CONFLICT (id) DO NOTHING"))


def downgrade() -> None:
    op.drop_table(_TABLE)
