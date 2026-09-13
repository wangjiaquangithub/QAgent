install:
	uv sync

dev:
	uv run langgraph dev --no-browser --allow-blocking --no-reload --server-log-level INFO

gateway:
	PYTHONPATH=. uv run uvicorn app.gateway.app:app --host 0.0.0.0 --port 8001

test:
	PYTHONPATH=. uv run pytest tests/ -v

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check . --fix && uv run ruff format .

# Standalone PostgreSQL foundation commands. These do not alter SQLite or LangGraph.
POSTGRES_COMPOSE = docker compose $(if $(wildcard ../.env),--env-file ../.env,) -f ../docker/docker-compose-postgres.yaml

postgres-up:
	$(POSTGRES_COMPOSE) up -d

postgres-down:
	$(POSTGRES_COMPOSE) down

postgres-health:
	@set -a; [ ! -f ../.env ] || . ../.env; set +a; PYTHONPATH=. uv run python -m app.postgres.healthcheck

postgres-migrate:
	@set -a; [ ! -f ../.env ] || . ../.env; set +a; PYTHONPATH=. uv run alembic -c alembic.ini upgrade head

postgres-verify:
	@set -a; [ ! -f ../.env ] || . ../.env; set +a; \
	if [ -z "$${QAGENT_POSTGRES_VERIFY_URL:-}" ]; then \
		echo "postgres-verify refused: set QAGENT_POSTGRES_VERIFY_URL for the dedicated qagent_foundation_test database." >&2; \
		echo "It never falls back to QAGENT_POSTGRES_URL or ordinary QAGENT_POSTGRES_* settings." >&2; \
		exit 1; \
	fi; \
	PYTHONPATH=. uv run python scripts/postgres_verify.py
