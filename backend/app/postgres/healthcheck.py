"""PostgreSQL connection health check CLI."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from sqlalchemy import create_engine, text

from .config import get_postgres_url, redact_postgres_url


def check_postgres() -> dict[str, object]:
    """Run a small read-only query and return a machine-readable result."""
    url = get_postgres_url()
    engine = create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 5})
    try:
        with engine.connect() as connection:
            row = connection.execute(text("SELECT current_database(), current_user, current_setting('server_version')")).one()
        return {
            "status": "ok",
            "database": row[0],
            "user": row[1],
            "server_version": row[2],
            "checked_at": datetime.now(UTC).isoformat(),
        }
    finally:
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="Check QAgent PostgreSQL connectivity")
    parser.parse_args()
    try:
        result = check_postgres()
    except Exception as exc:  # noqa: BLE001 - CLI must return an actionable error
        try:
            url = get_postgres_url(required=False)
        except Exception:
            url = None
        print(
            json.dumps(
                {
                    "status": "error",
                    "url": redact_postgres_url(url),
                    "error": str(exc),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
