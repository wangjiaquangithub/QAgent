"""Verify the disposable PostgreSQL foundation and Runtime concurrency semantics.

This script is intentionally destructive.  It accepts exactly one connection
configuration, ``QAGENT_POSTGRES_VERIFY_URL``, and refuses every database other
than ``qagent_foundation_test`` before executing any destructive SQL.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any

from sqlalchemy import Engine, create_engine, inspect, text

from app.postgres.config import (
    POSTGRES_URL_ENV,
    get_postgres_verify_url,
    get_psql_connection_parts,
    redact_postgres_url,
    validate_postgres_verify_database,
)
from app.qagent_runtime.events import EventType
from app.qagent_runtime.repository import RuntimeRepository

EXPECTED_MARKER = "backup-restore-verified"
FOUNDATION_MARKER = "foundation-ready"
BACKUP_FILE_ENV = "QAGENT_POSTGRES_BACKUP_FILE"
BACKEND_DIR = Path(__file__).resolve().parents[1]
ORG_ID = "postgres_verify"

REVISION_0002 = "0002_qagent_runtime"
REVISION_0003 = "0003_qagent_runtime_org_scoping"
REVISION_0004 = "0004_qagent_runtime_org_idempotency"


def _required_binary(name: str, env_name: str) -> str:
    configured = os.getenv(env_name, "").strip()
    path = configured or shutil.which(name)
    if not path:
        raise RuntimeError(f"{name} was not found. Install PostgreSQL client utilities or set {env_name}.")
    return path


def _display_command(command: list[str]) -> str:
    """Render an argv without ever putting a password in argv."""
    return shlex.join(command)


def _run(command: list[str], *, env: dict[str, str], label: str) -> None:
    print(f"[postgres-verify] {label}")
    print(f"[postgres-verify] command: {_display_command(command)}")
    result = subprocess.run(
        command,
        cwd=BACKEND_DIR,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    print(f"[postgres-verify] return code: {result.returncode}")
    print(f"[postgres-verify] stdout: {result.stdout.rstrip() if result.stdout else '<empty>'}")
    print(f"[postgres-verify] stderr: {result.stderr.rstrip() if result.stderr else '<empty>'}")
    if result.returncode != 0:
        raise RuntimeError(f"{label} failed with exit code {result.returncode}")


def _libpq_env(parts: dict[str, str]) -> dict[str, str]:
    env = os.environ.copy()
    password = parts.get("password")
    if password is not None:
        env["PGPASSWORD"] = password
    return env


def _client_args(parts: dict[str, str]) -> list[str]:
    return [
        "--host",
        parts["host"],
        "--port",
        parts["port"],
        "--username",
        parts["username"],
        "--dbname",
        parts["database"],
    ]


def _database_identity(engine: Engine) -> tuple[str, int]:
    """Check the database name on the same connection used by the caller."""
    with engine.connect() as connection:
        row = connection.execute(text("SELECT current_database(), pg_backend_pid()")).one()
    database, pid = str(row[0]), int(row[1])
    validate_postgres_verify_database(database)
    return database, pid


def _reset_public_schema(engine: Engine) -> None:
    print("[postgres-verify] Resetting public schema (test database only)")
    with engine.begin() as connection:
        actual_database = str(connection.execute(text("SELECT current_database()")).scalar_one())
        validate_postgres_verify_database(actual_database)
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))


def _verification_environment(url: str) -> dict[str, str]:
    """Build an Alembic environment containing only the verified PostgreSQL URL."""
    environment = os.environ.copy()
    for name in list(environment):
        if name.startswith("QAGENT_POSTGRES_"):
            del environment[name]
    environment[POSTGRES_URL_ENV] = url
    return environment


def _marker(engine: Engine) -> str:
    with engine.connect() as connection:
        value = connection.execute(text("SELECT marker FROM qagent_migration_probe WHERE id = 1")).scalar_one()
    return str(value)


def _write_test_marker(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO qagent_migration_probe (id, marker) VALUES (1, :marker) "
                "ON CONFLICT (id) DO UPDATE SET marker = EXCLUDED.marker"
            ),
            {"marker": EXPECTED_MARKER},
        )


def _backup_path() -> tuple[Path, tempfile.TemporaryDirectory[str] | None]:
    configured = os.getenv(BACKUP_FILE_ENV, "").strip()
    if configured:
        path = Path(configured).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path, None
    directory = tempfile.TemporaryDirectory(prefix="qagent-postgres-verify-")
    return Path(directory.name) / "qagent-foundation.dump", directory


def _revision(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one())


def _assert_revision(engine: Engine, expected: str) -> None:
    actual = _revision(engine)
    if actual != expected:
        raise RuntimeError(f"Expected Alembic revision {expected}, got {actual}")
    print(f"[postgres-verify] Alembic revision verified: {actual}")


def _table_columns(engine: Engine, table_name: str) -> set[str]:
    return {str(column["name"]) for column in inspect(engine).get_columns(table_name)}


def _constraint_names(engine: Engine, table_name: str) -> set[str]:
    return {str(item["name"]) for item in inspect(engine).get_unique_constraints(table_name) if item.get("name")}


def _index_names(engine: Engine, table_name: str) -> set[str]:
    return {str(item["name"]) for item in inspect(engine).get_indexes(table_name) if item.get("name")}


def _assert_runtime_schema_at_0002(engine: Engine) -> None:
    expected_tables = {
        "qagent_runs",
        "qagent_run_events",
        "qagent_approvals",
        "qagent_recovery_points",
        "qagent_run_assets",
    }
    tables = set(inspect(engine).get_table_names())
    missing = expected_tables - tables
    if missing:
        raise RuntimeError(f"0002 runtime tables are missing: {sorted(missing)}")
    if "org_id" in _table_columns(engine, "qagent_runs") or "org_id" in _table_columns(engine, "qagent_approvals"):
        raise RuntimeError("0002 unexpectedly contains org_id columns")
    constraints = _constraint_names(engine, "qagent_runs")
    if "uq_qagent_runs_idempotency_key" not in constraints:
        raise RuntimeError("0002 old idempotency constraint is missing")
    print("[postgres-verify] 0002 schema verified: Runtime tables and legacy idempotency constraint")


def _assert_runtime_schema_at_0003(engine: Engine) -> None:
    for table_name in ("qagent_runs", "qagent_approvals"):
        if "org_id" not in _table_columns(engine, table_name):
            raise RuntimeError(f"0003 did not add {table_name}.org_id")
        if f"ix_{table_name}_org_id" not in _index_names(engine, table_name):
            raise RuntimeError(f"0003 did not add the {table_name}.org_id index")
    print("[postgres-verify] 0003 schema verified: org_id columns and indexes")


def _assert_runtime_schema_at_0004(engine: Engine) -> None:
    constraints = _constraint_names(engine, "qagent_runs")
    if "uq_qagent_runs_org_idempotency_key" not in constraints:
        raise RuntimeError("0004 scoped idempotency constraint is missing")
    if "uq_qagent_runs_idempotency_key" in constraints:
        raise RuntimeError("0004 legacy idempotency constraint was not removed")
    print("[postgres-verify] 0004 schema verified: scoped idempotency constraint replaced legacy constraint")


def _verify_runtime_migrations(engine: Engine, alembic: list[str], environment: dict[str, str]) -> None:
    """Exercise each Runtime revision, one downgrade, and a final re-upgrade."""
    _run(alembic + ["upgrade", REVISION_0002], env=environment, label="Upgrade to 0002_qagent_runtime")
    _assert_revision(engine, REVISION_0002)
    _assert_runtime_schema_at_0002(engine)

    _run(alembic + ["upgrade", REVISION_0003], env=environment, label="Upgrade to 0003_qagent_runtime_org_scoping")
    _assert_revision(engine, REVISION_0003)
    _assert_runtime_schema_at_0003(engine)

    _run(alembic + ["upgrade", REVISION_0004], env=environment, label="Upgrade to 0004_qagent_runtime_org_idempotency")
    _assert_revision(engine, REVISION_0004)
    _assert_runtime_schema_at_0004(engine)

    _run(alembic + ["upgrade", "head"], env=environment, label="Repeat Runtime upgrade to head")
    _assert_revision(engine, REVISION_0004)

    # Revert only 0004 first, proving its downgrade restores the old unique key.
    _run(alembic + ["downgrade", REVISION_0003], env=environment, label="Downgrade 0004 to 0003")
    _assert_revision(engine, REVISION_0003)
    _assert_runtime_schema_at_0003(engine)
    constraints = _constraint_names(engine, "qagent_runs")
    if "uq_qagent_runs_idempotency_key" not in constraints or "uq_qagent_runs_org_idempotency_key" in constraints:
        raise RuntimeError("0004 downgrade did not restore the legacy idempotency constraint")
    print("[postgres-verify] 0004 downgrade verified: legacy idempotency constraint restored")

    # Revert 0003 as well, proving the org-scoping migration is reversible.
    _run(alembic + ["downgrade", REVISION_0002], env=environment, label="Downgrade 0003 to 0002")
    _assert_revision(engine, REVISION_0002)
    _assert_runtime_schema_at_0002(engine)

    _run(alembic + ["upgrade", "head"], env=environment, label="Re-upgrade Runtime migrations to head")
    _assert_revision(engine, REVISION_0004)
    _assert_runtime_schema_at_0003(engine)
    _assert_runtime_schema_at_0004(engine)
    print("[postgres-verify] Runtime migration upgrade/downgrade checks passed")


def _prepare_waiting_approval(repo: RuntimeRepository, *, task_id: str, idempotency_key: str) -> tuple[str, str]:
    run = repo.create_run(
        org_id=ORG_ID,
        task_id=task_id,
        input_payload={"scenario": task_id},
        idempotency_key=idempotency_key,
    )
    run_id = str(run["run_id"])
    if not repo.transition(
        run_id,
        "planning",
        EventType.RUN_PLANNING,
        from_statuses={"queued"},
        org_id=ORG_ID,
    ):
        raise RuntimeError(f"Could not transition {run_id} to planning")
    approval = repo.set_plan_and_approval(
        run_id,
        {"steps": []},
        {"scenario": task_id},
        org_id=ORG_ID,
    )
    approval_id = str(approval["approval_id"])
    if not repo.transition(
        run_id,
        "waiting_approval",
        EventType.RUN_WAITING_APPROVAL,
        from_statuses={"planning"},
        org_id=ORG_ID,
    ):
        raise RuntimeError(f"Could not transition {run_id} to waiting_approval")
    return run_id, approval_id


def _race_identity(engine: Engine) -> tuple[str, int]:
    database, pid = _database_identity(engine)
    print(f"[postgres-verify] worker connection verified: database={database} backend_pid={pid}")
    return database, pid


def _safe_outcome_error(exc: BaseException) -> str:
    # SQLAlchemy/psycopg normally omit passwords, but do not expose a traceback
    # or a potentially embedded DSN in this user-facing verification report.
    return f"{type(exc).__name__}: {str(exc).replace('postgresql+psycopg://', '<postgresql-url>')[:500]}"


def _run_two_connection_race(
    *,
    url: str,
    label: str,
    operations: dict[str, Callable[[RuntimeRepository], Any]],
) -> dict[str, dict[str, Any]]:
    """Run two operation factories against two independently pooled engines.

    Each worker receives a repository backed by its own SQLAlchemy engine. The
    backend PID is checked before the barrier and the engine pool is limited to
    one connection, so the operation uses the same independent backend that was
    identity-checked for that worker.
    """
    engines = [
        create_engine(
            url,
            pool_pre_ping=True,
            connect_args={"connect_timeout": 10},
            pool_size=1,
            max_overflow=0,
        )
        for _ in range(2)
    ]
    barrier = Barrier(2)
    names = list(operations)
    if len(names) != 2:
        raise ValueError(f"{label} requires exactly two operations")
    outcomes: dict[str, dict[str, Any]] = {}

    def worker(name: str, engine: Engine) -> tuple[str, dict[str, Any]]:
        database: str | None = None
        pid: int | None = None
        try:
            database, pid = _race_identity(engine)
            barrier.wait(timeout=30)
            worker_repo = RuntimeRepository(engine, create_schema=False)
            result = operations[name](worker_repo)
            return name, {"database": database, "pid": pid, "result": result, "error": None}
        except BaseException as exc:  # captured so the other worker cannot hang at the barrier
            return name, {"database": database, "pid": pid, "result": None, "error": _safe_outcome_error(exc)}

    try:
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="postgres-race") as executor:
            futures = [executor.submit(worker, name, engine) for name, engine in zip(names, engines, strict=True)]
            for future in futures:
                name, outcome = future.result()
                outcomes[name] = outcome
    finally:
        for engine in engines:
            engine.dispose()

    print(f"[postgres-verify] {label} worker outcomes: {outcomes}")
    errors = {name: result["error"] for name, result in outcomes.items() if result["error"]}
    if errors:
        raise RuntimeError(f"{label} had worker errors: {errors}")
    pids = [int(result["pid"]) for result in outcomes.values()]
    if len(pids) != 2 or len(set(pids)) != 2:
        raise RuntimeError(f"{label} did not use two independent PostgreSQL backend connections: {pids}")
    if any(result["database"] != "qagent_foundation_test" for result in outcomes.values()):
        raise RuntimeError(f"{label} worker database identity check failed")
    return outcomes


def _assert_event_sequences(repo: RuntimeRepository, run_id: str) -> list[dict[str, Any]]:
    events = repo.list_events(run_id, org_id=ORG_ID)
    sequences = [int(event["sequence"]) for event in events]
    expected = list(range(1, len(sequences) + 1))
    if sequences != sorted(sequences) or len(sequences) != len(set(sequences)) or sequences != expected:
        raise RuntimeError(f"Event sequence invariant failed for {run_id}: {sequences}")
    return events


def _assert_race_invariants(
    repo: RuntimeRepository,
    *,
    run_id: str,
    approval_id: str,
    scenario: str,
    expected_statuses: set[tuple[str, str]],
) -> None:
    run = repo.get_run(run_id, org_id=ORG_ID)
    approval = repo.get_approval(approval_id, org_id=ORG_ID, run_id=run_id)
    if not run or not approval:
        raise RuntimeError(f"{scenario} did not leave a readable run and approval")
    state = (str(approval["status"]), str(run["status"]))
    if state not in expected_statuses:
        raise RuntimeError(f"{scenario} reached invalid approval/run state: {state}")
    if state == ("granted", "failed") or state == ("rejected", "running"):
        raise RuntimeError(f"{scenario} violated approval/run invariant: {state}")

    events = _assert_event_sequences(repo, run_id)
    types = [str(event["type"]) for event in events]
    event_counts = {event_type: types.count(event_type) for event_type in set(types)}
    if state == ("granted", "running"):
        required = {str(EventType.APPROVAL_GRANTED), str(EventType.RUN_RUNNING)}
    elif state == ("rejected", "failed"):
        required = {str(EventType.APPROVAL_REJECTED), str(EventType.RUN_FAILED)}
    elif state == ("requested", "cancelled"):
        required = {str(EventType.RUN_CANCELLED)}
    else:
        raise RuntimeError(f"{scenario} has unsupported final state {state}")
    for event_type in required:
        if event_counts.get(event_type, 0) != 1:
            raise RuntimeError(f"{scenario} is missing or duplicated required event {event_type}: {event_counts}")
    for event_type in (
        str(EventType.APPROVAL_GRANTED),
        str(EventType.APPROVAL_REJECTED),
        str(EventType.RUN_RUNNING),
        str(EventType.RUN_FAILED),
        str(EventType.RUN_CANCELLED),
    ):
        if event_counts.get(event_type, 0) > 1:
            raise RuntimeError(f"{scenario} contains duplicate race event {event_type}: {event_counts}")
    print(f"[postgres-verify] {scenario} invariants passed: state={state}, sequences={[event['sequence'] for event in events]}")


def _verify_grant_vs_reject(url: str, repo: RuntimeRepository) -> None:
    run_id, approval_id = _prepare_waiting_approval(
        repo,
        task_id="grant-vs-reject",
        idempotency_key="postgres-verify-grant-vs-reject",
    )
    outcomes = _run_two_connection_race(
        url=url,
        label="Grant vs Reject",
        operations={
            "grant": lambda worker_repo: worker_repo.decide_approval_atomically(
                approval_id,
                status="granted",
                decided_by="grant-worker",
                reason=None,
                org_id=ORG_ID,
                run_id=run_id,
            ),
            "reject": lambda worker_repo: worker_repo.decide_approval_atomically(
                approval_id,
                status="rejected",
                decided_by="reject-worker",
                reason="verification rejection",
                org_id=ORG_ID,
                run_id=run_id,
            ),
        },
    )
    successes = [bool(outcomes[name]["result"][1]) for name in ("grant", "reject")]
    if successes.count(True) != 1 or successes.count(False) != 1:
        raise RuntimeError(f"Grant vs Reject expected exactly one successful decision: {outcomes}")
    _assert_race_invariants(
        repo,
        run_id=run_id,
        approval_id=approval_id,
        scenario="Grant vs Reject",
        expected_statuses={("granted", "running"), ("rejected", "failed")},
    )


def _verify_cancel_vs_grant(url: str, repo: RuntimeRepository) -> None:
    run_id, approval_id = _prepare_waiting_approval(
        repo,
        task_id="cancel-vs-grant",
        idempotency_key="postgres-verify-cancel-vs-grant",
    )
    outcomes = _run_two_connection_race(
        url=url,
        label="Cancel vs Grant",
        operations={
            "cancel": lambda worker_repo: worker_repo.transition(
                run_id,
                "cancelled",
                EventType.RUN_CANCELLED,
                from_statuses={"waiting_approval"},
                org_id=ORG_ID,
            ),
            "grant": lambda worker_repo: worker_repo.decide_approval_atomically(
                approval_id,
                status="granted",
                decided_by="grant-worker",
                reason=None,
                org_id=ORG_ID,
                run_id=run_id,
            ),
        },
    )
    cancel_success = bool(outcomes["cancel"]["result"])
    grant_success = bool(outcomes["grant"]["result"][1])
    if cancel_success == grant_success:
        raise RuntimeError(f"Cancel vs Grant expected exactly one winner: {outcomes}")
    _assert_race_invariants(
        repo,
        run_id=run_id,
        approval_id=approval_id,
        scenario="Cancel vs Grant",
        expected_statuses={("requested", "cancelled"), ("granted", "running")},
    )

def _verify_runtime_concurrency(url: str, engine: Engine) -> None:
    repo = RuntimeRepository(engine, create_schema=False)
    _verify_grant_vs_reject(url, repo)
    _verify_cancel_vs_grant(url, repo)
    print("[postgres-verify] Two-connection Runtime concurrency checks passed")


def run_verification() -> None:
    # This is intentionally a separate configuration path. The script performs
    # DROP SCHEMA and must never inherit the ordinary development URL.
    url = get_postgres_verify_url()
    assert url is not None
    parts = get_psql_connection_parts(url)
    verification_env = _verification_environment(url)

    engine = create_engine(
        url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 10},
        pool_size=1,
        max_overflow=0,
    )
    backup_path, temp_directory = _backup_path()
    try:
        print(f"[postgres-verify] Target: {redact_postgres_url(url)}")
        database, pid = _database_identity(engine)
        print(f"[postgres-verify] Verified current_database(): {database}; backend_pid={pid}")
        _reset_public_schema(engine)

        alembic = [sys.executable, "-m", "alembic", "-c", "alembic.ini"]
        _verify_runtime_migrations(engine, alembic, verification_env)
        if _marker(engine) != FOUNDATION_MARKER:
            raise RuntimeError("Foundation migration did not create the expected marker")
        print("[postgres-verify] Foundation marker verified")

        _run(alembic + ["upgrade", "head"], env=verification_env, label="Repeat final migration safely")
        _assert_revision(engine, REVISION_0004)

        _verify_runtime_concurrency(url, engine)
        _write_test_marker(engine)
        if _marker(engine) != EXPECTED_MARKER:
            raise RuntimeError("Could not write the backup/restore verification marker")
        print(f"[postgres-verify] Wrote test marker: {EXPECTED_MARKER}")

        dump = _required_binary("pg_dump", "PG_DUMP_BIN")
        restore = _required_binary("pg_restore", "PG_RESTORE_BIN")
        dump_env = _libpq_env(parts)
        _run(
            [dump, "--format=custom", "--no-owner", "--file", str(backup_path), *_client_args(parts)],
            env=dump_env,
            label=f"Create backup at {backup_path}",
        )
        if not backup_path.is_file() or backup_path.stat().st_size == 0:
            raise RuntimeError(f"Backup file was not created: {backup_path}")

        _reset_public_schema(engine)
        _run(
            [
                restore,
                "--clean",
                "--if-exists",
                "--no-owner",
                "--exit-on-error",
                *_client_args(parts),
                str(backup_path),
            ],
            env=dump_env,
            label="Restore backup into reset database",
        )
        if _marker(engine) != EXPECTED_MARKER:
            raise RuntimeError("Restored marker does not match the backup contents")
        _assert_revision(engine, REVISION_0004)
        print("[postgres-verify] Backup/restore data and schema check passed")
        print("[postgres-verify] ALL CHECKS PASSED")
    finally:
        engine.dispose()
        if temp_directory is not None:
            temp_directory.cleanup()


def main() -> int:
    try:
        run_verification()
    except Exception as exc:  # noqa: BLE001 - CLI must report a useful failure
        print(f"[postgres-verify] ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
