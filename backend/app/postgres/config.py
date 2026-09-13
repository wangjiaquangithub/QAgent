"""Environment-driven PostgreSQL connection configuration."""

from __future__ import annotations

import os

from sqlalchemy.engine import URL, make_url

POSTGRES_URL_ENV = "QAGENT_POSTGRES_URL"
POSTGRES_VERIFY_URL_ENV = "QAGENT_POSTGRES_VERIFY_URL"
POSTGRES_VERIFY_DATABASE = "qagent_foundation_test"


def _first_non_empty(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return None


def is_allowed_postgres_verify_database(database: str) -> bool:
    """Return whether ``database`` is the dedicated destructive-test database."""
    return database == POSTGRES_VERIFY_DATABASE


def validate_postgres_verify_database(database: str) -> None:
    """Reject every database except the explicitly reserved verification database."""
    if not is_allowed_postgres_verify_database(database):
        raise RuntimeError(f"Refusing destructive PostgreSQL verification against database {database!r}. Only the dedicated database {POSTGRES_VERIFY_DATABASE!r} is allowed.")


def get_postgres_verify_url(*, required: bool = True) -> str | None:
    """Return the dedicated URL used by destructive PostgreSQL verification.

    This intentionally never falls back to ``QAGENT_POSTGRES_URL`` or the
    ordinary ``QAGENT_POSTGRES_*`` variables.
    """
    raw_url = os.getenv(POSTGRES_VERIFY_URL_ENV, "").strip()
    if not raw_url:
        if required:
            raise RuntimeError(f"Destructive PostgreSQL verification requires {POSTGRES_VERIFY_URL_ENV}; it never falls back to QAGENT_POSTGRES_URL or ordinary development settings.")
        return None

    url = normalize_postgres_url(raw_url)
    parts = get_psql_connection_parts(url)
    validate_postgres_verify_database(parts["database"])
    return url


def get_postgres_url(*, required: bool = True) -> str | None:
    """Return a SQLAlchemy URL for the PostgreSQL foundation database.

    ``QAGENT_POSTGRES_URL`` is preferred. For local Docker development, the
    individual ``QAGENT_POSTGRES_*`` variables are also supported so the same
    values can be shared with Compose.
    """
    raw_url = os.getenv(POSTGRES_URL_ENV, "").strip()
    if raw_url:
        return normalize_postgres_url(raw_url)

    host = _first_non_empty("QAGENT_POSTGRES_HOST")
    port = _first_non_empty("QAGENT_POSTGRES_PORT")
    database = _first_non_empty("QAGENT_POSTGRES_DB")
    username = _first_non_empty("QAGENT_POSTGRES_USER")
    password = _first_non_empty("QAGENT_POSTGRES_PASSWORD")

    if all((host, port, database, username, password)):
        url = URL.create(
            drivername="postgresql+psycopg",
            username=username,
            password=password,
            host=host,
            port=int(port),
            database=database,
        )
        return url.render_as_string(hide_password=False)

    if required:
        raise RuntimeError("PostgreSQL is not configured. Set QAGENT_POSTGRES_URL or all of QAGENT_POSTGRES_HOST, QAGENT_POSTGRES_PORT, QAGENT_POSTGRES_DB, QAGENT_POSTGRES_USER, and QAGENT_POSTGRES_PASSWORD.")
    return None


def normalize_postgres_url(url: str) -> str:
    """Normalize common PostgreSQL URL spellings for SQLAlchemy + psycopg."""
    value = url.strip()
    if value.startswith("postgres://"):
        return "postgresql+psycopg://" + value[len("postgres://") :]
    if value.startswith("postgresql://"):
        return "postgresql+psycopg://" + value[len("postgresql://") :]
    if value.startswith("postgresql+psycopg://"):
        return value
    raise ValueError(f"{POSTGRES_URL_ENV} must use postgresql://, postgres://, or postgresql+psycopg://")


def redact_postgres_url(url: str | None) -> str:
    """Return a URL safe for logs and error messages."""
    if not url:
        return "<unset>"
    try:
        return make_url(normalize_postgres_url(url)).render_as_string(hide_password=True)
    except Exception:
        # Do not attempt to print an untrusted URL if parsing failed.
        return "<invalid PostgreSQL URL>"


def get_psql_connection_parts(url: str | None = None) -> dict[str, str]:
    """Return pg_dump/pg_restore connection arguments without exposing a password."""
    parsed = make_url(normalize_postgres_url(url or get_postgres_url() or ""))
    if not parsed.host or not parsed.database or not parsed.username:
        raise ValueError("PostgreSQL URL must include host, database, and username")
    parts = {
        "host": parsed.host,
        "port": str(parsed.port or 5432),
        "username": parsed.username,
        "database": parsed.database,
    }
    if parsed.password is not None:
        # Kept as a separate environment value for libpq; never put it in argv.
        parts["password"] = parsed.password
    return parts
