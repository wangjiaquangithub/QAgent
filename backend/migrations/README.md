# PostgreSQL migrations

This directory contains the PostgreSQL-only foundation schema. It is separate
from the existing SQLite schema and must not be used to migrate production
SQLite data.

Run from `backend/`:

```bash
uv run alembic upgrade head
uv run alembic current
```
