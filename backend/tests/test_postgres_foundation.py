"""Safe checks for the PostgreSQL foundation helpers and opt-in integration."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.postgres.config import (
    POSTGRES_VERIFY_URL_ENV,
    get_postgres_verify_url,
    is_allowed_postgres_verify_database,
    normalize_postgres_url,
    redact_postgres_url,
    validate_postgres_verify_database,
)
from app.qagent_runtime.events import EventType
from scripts import postgres_verify

BACKEND_DIR = Path(__file__).resolve().parents[1]


def test_normalize_postgres_url() -> None:
    assert normalize_postgres_url("postgres://user:pass@localhost/db").startswith("postgresql+psycopg://")
    assert normalize_postgres_url("postgresql+psycopg://user:pass@localhost/db") == ("postgresql+psycopg://user:pass@localhost/db")


def test_redact_postgres_url_hides_password() -> None:
    redacted = redact_postgres_url("postgresql://user:secret@localhost/db")
    assert "secret" not in redacted
    assert "user:" in redacted


def test_destructive_verification_allows_only_dedicated_database() -> None:
    assert is_allowed_postgres_verify_database("qagent_foundation_test")

    for database in (
        "qagent_dev",
        "qagent_prod",
        "qagent_production",
        "qagent_test",
        "qagent_shared",
        "QAGENT_FOUNDATION_TEST",
        "qagent_foundation_test_2",
    ):
        assert not is_allowed_postgres_verify_database(database)
        with pytest.raises(RuntimeError, match="Only the dedicated database"):
            validate_postgres_verify_database(database)


def test_verify_url_requires_dedicated_environment_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(POSTGRES_VERIFY_URL_ENV, raising=False)
    monkeypatch.setenv("QAGENT_POSTGRES_URL", "postgresql+psycopg://user:secret@localhost/qagent_dev")

    with pytest.raises(RuntimeError, match="never falls back"):
        get_postgres_verify_url()


def test_verify_url_rejects_qagent_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        POSTGRES_VERIFY_URL_ENV,
        "postgresql+psycopg://user:secret@localhost/qagent_dev",
    )

    with pytest.raises(RuntimeError, match="qagent_dev"):
        get_postgres_verify_url()


def test_verify_url_allows_qagent_foundation_test(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "postgresql+psycopg://user:secret@localhost/qagent_foundation_test"
    monkeypatch.setenv(POSTGRES_VERIFY_URL_ENV, url)

    assert get_postgres_verify_url() == url


def test_verify_cli_does_not_fallback_to_ordinary_postgres_url() -> None:
    env = os.environ.copy()
    env.pop(POSTGRES_VERIFY_URL_ENV, None)
    env["QAGENT_POSTGRES_URL"] = "postgresql+psycopg://user:secret@localhost/qagent_dev"
    env["PYTHONPATH"] = "."

    result = subprocess.run(
        [sys.executable, "scripts/postgres_verify.py"],
        cwd=BACKEND_DIR,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "requires QAGENT_POSTGRES_VERIFY_URL" in output
    assert "DROP SCHEMA" not in output


class _FakeResult:
    def __init__(self, value: str) -> None:
        self.value = value

    def scalar_one(self) -> str:
        return self.value


class _FakeConnection:
    def __init__(self, current_database: str) -> None:
        self.current_database = current_database
        self.statements: list[str] = []

    def execute(self, statement, *args, **kwargs):
        sql = str(statement)
        self.statements.append(sql)
        if sql == "SELECT current_database()":
            return _FakeResult(self.current_database)
        return _FakeResult("")


class _FakeBegin:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection

    def __enter__(self) -> _FakeConnection:
        return self.connection

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        return False


class _FakeEngine:
    def __init__(self, current_database: str) -> None:
        self.connection = _FakeConnection(current_database)

    def begin(self) -> _FakeBegin:
        return _FakeBegin(self.connection)


def test_reset_public_schema_rejects_database_before_drop() -> None:
    engine = _FakeEngine("qagent_dev")

    with pytest.raises(RuntimeError, match="qagent_dev"):
        postgres_verify._reset_public_schema(engine)

    assert engine.connection.statements == ["SELECT current_database()"]


def test_reset_public_schema_checks_database_on_same_connection_before_drop() -> None:
    engine = _FakeEngine("qagent_foundation_test")

    postgres_verify._reset_public_schema(engine)

    assert engine.connection.statements == [
        "SELECT current_database()",
        "DROP SCHEMA public CASCADE",
        "CREATE SCHEMA public",
    ]


def test_verification_environment_removes_all_ordinary_postgres_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    ordinary = {
        "QAGENT_POSTGRES_HOST": "ordinary-host",
        "QAGENT_POSTGRES_PORT": "54329",
        "QAGENT_POSTGRES_DB": "qagent_dev",
        "QAGENT_POSTGRES_USER": "ordinary-user",
        "QAGENT_POSTGRES_PASSWORD": "ordinary-password",
        "QAGENT_POSTGRES_URL": "postgresql+psycopg://ordinary:secret@ordinary-host/qagent_dev",
        "QAGENT_POSTGRES_VERIFY_URL": "postgresql+psycopg://verify:secret@verify-host/qagent_foundation_test",
    }
    for name, value in ordinary.items():
        monkeypatch.setenv(name, value)

    verify_url = "postgresql+psycopg://verify:secret@verify-host/qagent_foundation_test"
    verification_env = postgres_verify._verification_environment(verify_url)

    assert verification_env["QAGENT_POSTGRES_URL"] == verify_url
    assert all(not name.startswith("QAGENT_POSTGRES_") or name == "QAGENT_POSTGRES_URL" for name in verification_env)
    assert all(name not in verification_env for name in ordinary if name != "QAGENT_POSTGRES_URL")


@pytest.mark.postgres
def test_postgres_foundation_end_to_end() -> None:
    """Run the destructive integration check only when explicitly enabled."""
    if os.getenv("QAGENT_RUN_POSTGRES_TESTS") != "1":
        pytest.skip("set QAGENT_RUN_POSTGRES_TESTS=1 to run PostgreSQL integration tests")
    if not os.getenv(POSTGRES_VERIFY_URL_ENV, "").strip():
        pytest.skip("QAGENT_POSTGRES_VERIFY_URL is not configured")

    result = subprocess.run(
        [sys.executable, "scripts/postgres_verify.py"],
        cwd=BACKEND_DIR,
        env={**os.environ, "PYTHONPATH": "."},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr

@pytest.mark.parametrize("sequences", [[1, 2, 3]])
def test_assert_event_sequences_accepts_contiguous_sequences(sequences: list[int]) -> None:
    class FakeRepository:
        def list_events(self, run_id: str, *, org_id: str) -> list[dict[str, object]]:
            return [{"sequence": sequence, "type": "test"} for sequence in sequences]

    events = postgres_verify._assert_event_sequences(FakeRepository(), "run-1")

    assert [event["sequence"] for event in events] == sequences


@pytest.mark.parametrize("sequences", [[1, 3], [2, 3], [1, 1, 2]])
def test_assert_event_sequences_rejects_non_unique_or_non_contiguous_sequences(sequences: list[int]) -> None:
    class FakeRepository:
        def list_events(self, run_id: str, *, org_id: str) -> list[dict[str, object]]:
            return [{"sequence": sequence, "type": "test"} for sequence in sequences]

    with pytest.raises(RuntimeError, match="Event sequence invariant failed"):
        postgres_verify._assert_event_sequences(FakeRepository(), "run-1")


class _InvariantRepository:
    def __init__(self, approval_status: str, run_status: str, event_types: list[str]) -> None:
        self.approval_status = approval_status
        self.run_status = run_status
        self.event_types = event_types

    def get_run(self, run_id: str, *, org_id: str) -> dict[str, str]:
        return {"run_id": run_id, "status": self.run_status}

    def get_approval(self, approval_id: str, *, org_id: str, run_id: str) -> dict[str, str]:
        return {"approval_id": approval_id, "status": self.approval_status}

    def list_events(self, run_id: str, *, org_id: str) -> list[dict[str, object]]:
        return [
            {"sequence": sequence, "type": event_type}
            for sequence, event_type in enumerate(self.event_types, start=1)
        ]


@pytest.mark.parametrize(
    ("approval_status", "run_status", "event_types"),
    [
        ("granted", "running", [str(EventType.APPROVAL_GRANTED), str(EventType.RUN_RUNNING)]),
        ("rejected", "failed", [str(EventType.APPROVAL_REJECTED), str(EventType.RUN_FAILED)]),
        ("requested", "cancelled", [str(EventType.RUN_CANCELLED)]),
    ],
)
def test_assert_race_invariants_accepts_valid_terminal_states(
    approval_status: str,
    run_status: str,
    event_types: list[str],
) -> None:
    postgres_verify._assert_race_invariants(
        _InvariantRepository(approval_status, run_status, event_types),
        run_id="run-1",
        approval_id="approval-1",
        scenario="fake race",
        expected_statuses={("granted", "running"), ("rejected", "failed"), ("requested", "cancelled")},
    )


@pytest.mark.parametrize(
    ("approval_status", "run_status", "event_types", "message"),
    [
        ("granted", "failed", [str(EventType.APPROVAL_GRANTED), str(EventType.RUN_FAILED)], "invalid approval/run state"),
        ("rejected", "running", [str(EventType.APPROVAL_REJECTED), str(EventType.RUN_RUNNING)], "invalid approval/run state"),
        ("granted", "running", [str(EventType.APPROVAL_GRANTED)], "missing or duplicated required event"),
    ],
)
def test_assert_race_invariants_rejects_invalid_states_or_missing_events(
    approval_status: str,
    run_status: str,
    event_types: list[str],
    message: str,
) -> None:
    with pytest.raises(RuntimeError, match=message):
        postgres_verify._assert_race_invariants(
            _InvariantRepository(approval_status, run_status, event_types),
            run_id="run-1",
            approval_id="approval-1",
            scenario="fake race",
            expected_statuses={("granted", "running"), ("rejected", "failed"), ("requested", "cancelled")},
        )


def test_run_two_connection_race_uses_distinct_repositories_and_disposes_engines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeEngine:
        def __init__(self, index: int) -> None:
            self.index = index
            self.disposed = False

        def dispose(self) -> None:
            self.disposed = True

    engines: list[FakeEngine] = []

    def fake_create_engine(url: str, **kwargs: object) -> FakeEngine:
        engine = FakeEngine(len(engines))
        engines.append(engine)
        return engine

    repositories: list[object] = []

    class FakeRepository:
        def __init__(self, engine: FakeEngine, *, create_schema: bool) -> None:
            assert create_schema is False
            self.engine = engine
            repositories.append(self)

    calls: list[tuple[str, int]] = []

    def fake_race_identity(engine: FakeEngine) -> tuple[str, int]:
        return "qagent_foundation_test", engine.index + 100

    monkeypatch.setattr(postgres_verify, "create_engine", fake_create_engine)
    monkeypatch.setattr(postgres_verify, "RuntimeRepository", FakeRepository)
    monkeypatch.setattr(postgres_verify, "_race_identity", fake_race_identity)

    def operation(name: str):
        def run(worker_repo: FakeRepository) -> str:
            calls.append((name, worker_repo.engine.index))
            return f"{name}-done"

        return run

    outcomes = postgres_verify._run_two_connection_race(
        url="postgresql+psycopg://verify:secret@localhost/qagent_foundation_test",
        label="fake race",
        operations={"first": operation("first"), "second": operation("second")},
    )

    assert sorted(calls) == [("first", 0), ("second", 1)]
    assert len({id(repository) for repository in repositories}) == 2
    assert outcomes["first"]["result"] == "first-done"
    assert outcomes["second"]["result"] == "second-done"
    assert [outcome["pid"] for outcome in outcomes.values()] == [100, 101]
    assert all(engine.disposed for engine in engines)
