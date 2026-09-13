"""Alembic environment for the PostgreSQL infrastructure schema."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import Column, MetaData, String, Table, create_engine, pool
from sqlalchemy.schema import PrimaryKeyConstraint

from app.postgres.config import get_postgres_url

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None

# Alembic hard-codes ``alembic_version.version_num`` as ``VARCHAR(32)`` in
# ``DefaultImpl.version_table_impl`` and exposes no length option. Several
# QAgent Runtime revision ids are longer than 32 characters, e.g.
# ``0004_qagent_runtime_org_idempotency`` (35) and
# ``0005_qagent_runtime_execution_claim`` (35). On PostgreSQL the version
# update then fails with::
#
#     psycopg.errors.StringDataRightTruncation:
#     value too long for type character varying(32)
#
# and the whole migration transaction rolls back, so migrations can never
# advance past 0003. Pre-creating the version table at a safe width makes
# Alembic reuse it (``checkfirst=True``) instead of creating the narrow one.
VERSION_TABLE = "alembic_version"
VERSION_NUM_LENGTH = 255


def _ensure_version_table_width(connection) -> None:
    """Create ``alembic_version`` with a revision-id-safe column width.

    No-op when the table already exists, so existing databases keep their
    current version row untouched.
    """
    table = Table(
        VERSION_TABLE,
        MetaData(),
        Column("version_num", String(VERSION_NUM_LENGTH), nullable=False),
        PrimaryKeyConstraint("version_num", name=f"{VERSION_TABLE}_pkc"),
    )
    table.create(bind=connection, checkfirst=True)


def run_migrations_offline() -> None:
    url = get_postgres_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = get_postgres_url()
    connectable = create_engine(
        url,
        poolclass=pool.NullPool,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 10},
    )
    try:
        with connectable.connect() as connection:
            # Must run before context.configure(): Alembic creates its version
            # table while configuring the migration context.
            _ensure_version_table_width(connection)
            connection.commit()
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
