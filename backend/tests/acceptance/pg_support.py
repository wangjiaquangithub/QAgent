"""Real-PostgreSQL support for the G0.7 Runtime concurrency acceptance card.

Design rules (from the card):
- Real, dedicated, disposable PostgreSQL only. Never SQLite.
- Only ``qagent_foundation_test`` may receive destructive SQL, verified on the
  live connection via ``SELECT current_database()`` before anything destructive.
- Two-connection races must prove they used two distinct PostgreSQL backends
  (``pg_backend_pid()``) before running the competing operations.

This module holds the shared engine/database plumbing and the evidence model so
each scenario test stays readable and keeps the same safety checks.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.postgres.config import (
    POSTGRES_VERIFY_DATABASE,
    POSTGRES_VERIFY_URL_ENV,
    get_postgres_verify_url,
)
from app.qagent_runtime.events import EventType
from app.qagent_runtime.models import (
    metadata,
)
from app.qagent_runtime.repository import RuntimeRepository

# Opt-in gate, matching the existing PostgreSQL integration tests.
RUN_ENV = "QAGENT_RUN_POSTGRES_TESTS"

# Scenario-local org so parallel/repeat runs never collide with other data.
ACCEPTANCE_ORG = "g07_acceptance"


class PostgresNotConfigured(RuntimeError):
    """Raised when a real PostgreSQL verification target is unavailable."""


def require_postgres_verify_url() -> str:
    """Return the dedicated verification URL or fail with a clear message.

    Never falls back to ``QAGENT_POSTGRES_URL``; the card forbids using an
    ordinary development database for this acceptance work.
    """
    try:
        url = get_postgres_verify_url()
    except RuntimeError as exc:  # missing URL or non-dedicated database name
        raise PostgresNotConfigured(str(exc)) from exc
    if not url:
        raise PostgresNotConfigured(f"{POSTGRES_VERIFY_URL_ENV} is not configured")
    return url


def postgres_tests_enabled() -> bool:
    return os.getenv(RUN_ENV) == "1"


def make_engine(url: str, *, pool_size: int = 1, max_overflow: int = 0) -> Engine:
    """One pooled connection per engine so a worker keeps its checked backend.

    ``pool_size=1`` + ``max_overflow=0`` means the engine cannot silently open a
    second backend mid-scenario, so the ``pg_backend_pid()`` identity check made
    before the barrier still describes the connection doing the work.
    """
    return sa.create_engine(
        url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 10},
        pool_size=pool_size,
        max_overflow=max_overflow,
    )


def assert_dedicated_database(engine: Engine) -> tuple[str, int]:
    """Assert the live connection targets the dedicated test database.

    Returns ``(database, backend_pid)``. Callers MUST invoke this on the same
    engine/connection that will run destructive SQL.
    """
    with engine.connect() as connection:
        row = connection.execute(sa.text("SELECT current_database(), pg_backend_pid()")).one()
    database, pid = str(row[0]), int(row[1])
    if database != POSTGRES_VERIFY_DATABASE:
        raise PostgresNotConfigured(f"Refusing to run against {database!r}; expected exactly {POSTGRES_VERIFY_DATABASE!r}")
    return database, pid


def assert_is_postgres(engine: Engine) -> None:
    """Fail loudly if the engine is not talking to PostgreSQL.

    Guards against a fixture accidentally substituting SQLite.
    """
    with engine.connect() as connection:
        dialect = connection.dialect.name
    if dialect != "postgresql":
        raise AssertionError(f"acceptance tests require real PostgreSQL, got dialect {dialect!r}")


def reset_runtime_schema(engine: Engine) -> None:
    """Drop and recreate the runtime tables on the dedicated database only."""
    assert_dedicated_database(engine)
    metadata.drop_all(engine)
    metadata.create_all(engine)


def truncate_runtime_tables(engine: Engine) -> None:
    """Clear runtime rows while keeping the schema, for scenario isolation."""
    assert_dedicated_database(engine)
    with engine.begin() as connection:
        connection.execute(sa.text("TRUNCATE qagent_run_events, qagent_run_assets, qagent_recovery_points, qagent_approvals, qagent_runs RESTART IDENTITY CASCADE"))


@dataclass
class RaceOutcome:
    """Result of one worker in a two-connection race."""

    name: str
    database: str | None
    pid: int | None
    result: Any
    error: str | None

    @property
    def succeeded(self) -> bool:
        return self.error is None


def safe_error(exc: BaseException) -> str:
    """Describe an exception without leaking a DSN or traceback."""
    text = str(exc).replace("postgresql+psycopg://", "<postgresql-url>")
    return f"{type(exc).__name__}: {text[:500]}"


def run_two_connection_race(
    *,
    url: str,
    operations: dict[str, Callable[[RuntimeRepository, Engine], Any]],
) -> dict[str, RaceOutcome]:
    """Run two operations concurrently on two independent PostgreSQL backends.

    Each worker gets its own engine, asserts the dedicated database identity on
    its own connection *before* the barrier, then executes its operation. Both
    workers must report distinct backend PIDs, proving genuine multi-connection
    concurrency rather than single-connection interleaving.
    """
    names = list(operations)
    if len(names) != 2:
        raise ValueError("run_two_connection_race requires exactly two operations")

    engines = [make_engine(url) for _ in range(2)]
    barrier = Barrier(2)
    outcomes: dict[str, RaceOutcome] = {}

    def worker(name: str, engine: Engine) -> RaceOutcome:
        database: str | None = None
        pid: int | None = None
        try:
            database, pid = assert_dedicated_database(engine)
            barrier.wait(timeout=30)
            repository = RuntimeRepository(engine, create_schema=False)
            result = operations[name](repository, engine)
            return RaceOutcome(name=name, database=database, pid=pid, result=result, error=None)
        except BaseException as exc:  # noqa: BLE001 - surfaced as evidence
            return RaceOutcome(name=name, database=database, pid=pid, result=None, error=safe_error(exc))

    try:
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="g07-race") as executor:
            futures = [executor.submit(worker, name, engine) for name, engine in zip(names, engines, strict=True)]
            for future in futures:
                outcome = future.result()
                outcomes[outcome.name] = outcome
    finally:
        for engine in engines:
            engine.dispose()

    errors = {name: outcome.error for name, outcome in outcomes.items() if outcome.error}
    if errors:
        raise AssertionError(f"race worker errors: {errors}")

    pids = [outcome.pid for outcome in outcomes.values()]
    if len(pids) != 2 or len(set(pids)) != 2:
        raise AssertionError(f"race did not use two independent PostgreSQL backends: {pids}")
    databases = {outcome.database for outcome in outcomes.values()}
    if databases != {POSTGRES_VERIFY_DATABASE}:
        raise AssertionError(f"race worker database identity failure: {databases}")
    return outcomes


# --------------------------------------------------------------------------
# Run-state helpers
# --------------------------------------------------------------------------


def seed_waiting_approval_run(
    repository: RuntimeRepository,
    *,
    task_id: str,
    org_id: str = ACCEPTANCE_ORG,
) -> tuple[str, str]:
    """Create a run parked in ``waiting_approval`` with one requested approval."""
    run = repository.create_run(
        org_id=org_id,
        task_id=task_id,
        input_payload={"scenario": task_id},
        idempotency_key=f"g07-{task_id}",
    )
    run_id = str(run["run_id"])
    if not repository.transition(run_id, "planning", EventType.RUN_PLANNING, from_statuses={"queued"}, org_id=org_id):
        raise AssertionError(f"could not move {run_id} to planning")
    approval = repository.set_plan_and_approval(run_id, {"steps": []}, {"scenario": task_id}, org_id=org_id)
    approval_id = str(approval["approval_id"])
    if not repository.transition(
        run_id,
        "waiting_approval",
        EventType.RUN_WAITING_APPROVAL,
        from_statuses={"planning"},
        org_id=org_id,
    ):
        raise AssertionError(f"could not move {run_id} to waiting_approval")
    return run_id, approval_id


def seed_running_run(
    repository: RuntimeRepository,
    *,
    task_id: str,
    org_id: str = ACCEPTANCE_ORG,
) -> str:
    """Create a run parked in ``running`` so execution claim can be contested."""
    run_id, approval_id = seed_waiting_approval_run(repository, task_id=task_id, org_id=org_id)
    approval, changed = repository.decide_approval_atomically(
        approval_id,
        status="granted",
        decided_by="seed",
        reason=None,
        org_id=org_id,
        run_id=run_id,
    )
    if not changed:
        raise AssertionError(f"seed grant failed for {run_id}")
    return current_run_status(repository, run_id, org_id=org_id)["run_id"]


def current_run_status(repository: RuntimeRepository, run_id: str, *, org_id: str = ACCEPTANCE_ORG) -> dict[str, Any]:
    run = repository.get_run(run_id, org_id=org_id)
    if not run:
        raise AssertionError(f"run {run_id} disappeared")
    return run


def event_sequences(repository: RuntimeRepository, run_id: str, *, org_id: str = ACCEPTANCE_ORG) -> list[int]:
    events = repository.list_events(run_id, org_id=org_id)
    return [int(event["sequence"]) for event in events]


def assert_sequences_monotonic_unique(repository: RuntimeRepository, run_id: str, *, org_id: str = ACCEPTANCE_ORG) -> list[int]:
    """Sequence must be strictly increasing, gap-free from 1, and unique."""
    sequences = event_sequences(repository, run_id, org_id=org_id)
    expected = list(range(1, len(sequences) + 1))
    if sequences != expected:
        raise AssertionError(f"event sequences not monotonic/unique/contiguous: {sequences}")
    return sequences


# --------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------


@dataclass
class ScenarioEvidence:
    """Machine-readable evidence for one acceptance scenario."""

    scenario: str
    initial_state: dict[str, Any] = field(default_factory=dict)
    concurrency_mode: str = ""
    final_state: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)
    conclusion: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "recorded_at": datetime.now(UTC).isoformat(),
            "initial_state": self.initial_state,
            "concurrency_mode": self.concurrency_mode,
            "final_state": self.final_state,
            "extra": self.extra,
            "conclusion": self.conclusion,
        }


def dump_run_snapshot(engine: Engine, run_id: str, approval_id: str | None = None) -> dict[str, Any]:
    """Read the final Run / Approval / Event / epoch state straight from SQL."""
    assert_dedicated_database(engine)
    with engine.connect() as connection:
        run_row = (
            connection.execute(
                sa.text("SELECT run_id, status, version, execution_claim, execution_epoch, execution_claimed_at, result_payload, error_payload, org_id FROM qagent_runs WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
            .mappings()
            .first()
        )
        if run_row is None:
            return {"run": None}
        run = dict(run_row)
        run["execution_claimed_at"] = _iso(run.get("execution_claimed_at"))

        approval: dict[str, Any] | None = None
        approval_query = "SELECT approval_id, status, decided_by, reason FROM qagent_approvals WHERE run_id = :run_id"
        params: dict[str, Any] = {"run_id": run_id}
        if approval_id:
            approval_query += " AND approval_id = :approval_id"
            params["approval_id"] = approval_id
        approval_row = connection.execute(sa.text(approval_query), params).mappings().first()
        if approval_row is not None:
            approval = dict(approval_row)

        events = [
            dict(row)
            for row in connection.execute(
                sa.text("SELECT event_id, sequence, type, payload FROM qagent_run_events WHERE run_id = :run_id ORDER BY sequence"),
                {"run_id": run_id},
            ).mappings()
        ]
        asset_count = connection.execute(
            sa.text("SELECT COUNT(*) FROM qagent_run_assets WHERE run_id = :run_id"),
            {"run_id": run_id},
        ).scalar_one()
    return {
        "run": run,
        "approval": approval,
        "events": events,
        "event_sequences": [int(event["sequence"]) for event in events],
        "event_types": [str(event["type"]) for event in events],
        "asset_count": int(asset_count),
    }


def _iso(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return value


def write_evidence(evidence: ScenarioEvidence, directory: Path) -> Path:
    """Persist one scenario's evidence as JSON next to the pytest artifacts."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{evidence.scenario}.json"
    path.write_text(json.dumps(evidence.as_dict(), indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def pytest_skip_if_disabled() -> None:
    if not postgres_tests_enabled():
        pytest.skip(f"set {RUN_ENV}=1 to run real-PostgreSQL acceptance tests")


def resolve_url_or_skip() -> str:
    try:
        return require_postgres_verify_url()
    except PostgresNotConfigured as exc:
        pytest.skip(str(exc))


def evidence_directory() -> Path:
    """Evidence root; override with ``QAGENT_G07_EVIDENCE_DIR``.

    Defaults to the repository's gitignored ``.tmp/`` area: the card requires
    local run evidence to stay out of Git, and ``.tmp/`` is already reserved
    for temporary migration/check output.
    """
    configured = os.getenv("QAGENT_G07_EVIDENCE_DIR", "").strip()
    if configured:
        return Path(configured)
    return Path(".tmp/g07-postgres-concurrency")


@dataclass
class RuntimeHarness:
    """Convenience bundle passed to scenario tests."""

    url: str
    engine: Engine
    repository: RuntimeRepository
    evidence_dir: Path

    def snapshot(self, run_id: str, approval_id: str | None = None) -> dict[str, Any]:
        return dump_run_snapshot(self.engine, run_id, approval_id)


def _harness_fixture() -> Iterator[RuntimeHarness]:
    pytest_skip_if_disabled()
    url = resolve_url_or_skip()
    engine = make_engine(url)
    try:
        assert_is_postgres(engine)
        assert_dedicated_database(engine)
        reset_runtime_schema(engine)
        yield RuntimeHarness(
            url=url,
            engine=engine,
            repository=RuntimeRepository(engine, create_schema=False),
            evidence_dir=evidence_directory(),
        )
    finally:
        engine.dispose()


@pytest.fixture
def runtime_harness() -> Iterator[RuntimeHarness]:
    """Real PostgreSQL harness with a freshly created runtime schema."""
    yield from _harness_fixture()


@pytest.fixture
def clean_runtime(runtime_harness: RuntimeHarness) -> Iterator[RuntimeHarness]:
    """Per-scenario isolation: truncate runtime rows before each scenario."""
    truncate_runtime_tables(runtime_harness.engine)
    yield runtime_harness
    truncate_runtime_tables(runtime_harness.engine)


__all__ = [
    "ACCEPTANCE_ORG",
    "RUN_ENV",
    "PostgresNotConfigured",
    "RaceOutcome",
    "RuntimeHarness",
    "ScenarioEvidence",
    "assert_dedicated_database",
    "assert_is_postgres",
    "assert_sequences_monotonic_unique",
    "clean_runtime",
    "current_run_status",
    "dump_run_snapshot",
    "event_sequences",
    "evidence_directory",
    "make_engine",
    "postgres_tests_enabled",
    "require_postgres_verify_url",
    "reset_runtime_schema",
    "run_two_connection_race",
    "runtime_harness",
    "safe_error",
    "seed_running_run",
    "seed_waiting_approval_run",
    "truncate_runtime_tables",
    "write_evidence",
]
