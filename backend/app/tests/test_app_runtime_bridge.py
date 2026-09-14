"""Targeted tests for the AG-G2-APP-003-A01 App Runtime bridge.

Covers the first closed loop contract:

1. the switch is inert by default and the legacy path is signalled unchanged;
2. Runtime create/start use the stable ``app-run:{run_id}`` idempotency
   association derived from the existing App Run id, and a retried trigger
   cannot create a second Runtime run;
3. Runtime completed / failed / timed_out / cancelled project onto the
   existing App Run terminal fields through ``update_run_status`` only;
4. a terminal App Run is never regressed;
5. a Runtime call failure lands on the existing failed-terminal write path;
6. ``_sync_run_from_task`` never regresses a terminal App Run (bridge-aware
   Task Center polling guard).

Only public ``RuntimeService``-shaped fakes are used; the Runtime kernel is
never imported at test runtime beyond what the bridge lazily resolves.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.gateway import app_runtime_bridge as bridge

# ---------------------------------------------------------------------------
# Fake RuntimeService (public contract shape only)
# ---------------------------------------------------------------------------


class FakeRuntimeService:
    def __init__(
        self,
        *,
        final_status: str = "completed",
        result_payload: Any = None,
        error_payload: Any = None,
        fail_on: str | None = None,
    ) -> None:
        self.runs: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[Any, ...]] = []
        self._next = 0
        self.final_status = final_status
        self.result_payload = result_payload
        self.error_payload = error_payload
        self.fail_on = fail_on

    def _find(self, run_id: str) -> dict[str, Any]:
        for run in self.runs.values():
            if run["run_id"] == run_id:
                return run
        raise KeyError(run_id)

    async def create_run(
        self,
        *,
        org_id: str = "local",
        task_id: str,
        input_payload: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("create_run", org_id, task_id, idempotency_key))
        if self.fail_on == "create_run":
            raise RuntimeError("provider configuration invalid")
        key = idempotency_key or ""
        if key in self.runs:
            return self.runs[key]
        self._next += 1
        run = {
            "run_id": f"runtime-run-{self._next}",
            "org_id": org_id,
            "task_id": task_id,
            "input_payload": input_payload,
            "status": "created",
        }
        self.runs[key] = run
        return run

    async def start_run(self, run_id: str, org_id: str | None = None) -> dict[str, Any]:
        self.calls.append(("start_run", run_id))
        if self.fail_on == "start_run":
            raise RuntimeError("planner unavailable")
        run = self._find(run_id)
        run["status"] = "waiting_approval"
        run["approval"] = {"approval_id": f"approval-{run_id}", "status": "pending"}
        return {"run_id": run_id, "status": run["status"]}

    async def get_run_status(self, run_id: str, *, org_id: str | None = None) -> dict[str, Any]:
        run = self._find(run_id)
        return {"run_id": run_id, "status": run["status"], "approval": run.get("approval")}

    async def grant_approval(
        self,
        approval_id: str,
        *,
        decided_by: str | None = None,
        reason: str | None = None,
        org_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("grant_approval", approval_id, run_id))
        if self.fail_on == "grant_approval":
            raise RuntimeError("approval store unavailable")
        run = self._find(str(run_id))
        run["status"] = self.final_status
        run["result_payload"] = self.result_payload
        run["error_payload"] = self.error_payload
        run["approval"] = None
        return {"run_id": run_id, "status": run["status"]}

    async def get_result(self, run_id: str, org_id: str | None = None) -> dict[str, Any]:
        if self.fail_on == "get_result":
            raise RuntimeError("result store unavailable")
        run = self._find(run_id)
        return {
            "run_id": run_id,
            "status": run["status"],
            "result": run.get("result_payload"),
            "assets": [],
            "error": run.get("error_payload"),
        }


# ---------------------------------------------------------------------------
# App Run record double (monkeypatched app_repositories)
# ---------------------------------------------------------------------------


@pytest.fixture()
def app_run_store(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[tuple[Any, ...]]]:
    from evoflow.persistence import app_repositories

    runs: dict[str, dict[str, Any]] = {}
    writes: list[tuple[Any, ...]] = []

    def fake_load_run(run_id: str) -> dict[str, Any] | None:
        return runs.get(run_id)

    def fake_update_run_status(run_id: str, status: str, **fields: Any) -> None:
        writes.append((run_id, status, fields))
        run = runs.setdefault(run_id, {"id": run_id, "status": "planned", "progress": 0})
        run["status"] = status
        run.update(fields)

    monkeypatch.setattr(app_repositories, "load_run", fake_load_run)
    monkeypatch.setattr(app_repositories, "update_run_status", fake_update_run_status)
    store: dict[str, list[tuple[Any, ...]]] = {"runs": runs, "writes": writes}  # type: ignore[assignment]
    return store


def _seed_run(store: dict[str, Any], run_id: str, status: str = "planned") -> None:
    store["runs"][run_id] = {"id": run_id, "status": status, "progress": 0}


# ---------------------------------------------------------------------------
# 1. Opt-in switch is inert by default
# ---------------------------------------------------------------------------


def test_bridge_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EVOFLOW_APP_WORKFLOW_RUNTIME", raising=False)
    assert bridge.app_runtime_bridge_enabled() is False


def test_trigger_without_opt_in_reports_not_applicable(
    monkeypatch: pytest.MonkeyPatch, app_run_store: dict[str, Any]
) -> None:
    monkeypatch.delenv("EVOFLOW_APP_WORKFLOW_RUNTIME", raising=False)
    triggered = bridge.trigger_workflow_app_runtime_run(
        run_id="apprun-1",
        app_id="app-1",
        app_version=None,
        parameters={"q": "hi"},
        task_id="task-1",
        org_id="org-1",
        service=FakeRuntimeService(),
    )
    assert triggered is False
    assert app_run_store["writes"] == []


def test_trigger_without_org_context_reports_not_applicable(
    monkeypatch: pytest.MonkeyPatch, app_run_store: dict[str, Any]
) -> None:
    monkeypatch.setenv("EVOFLOW_APP_WORKFLOW_RUNTIME", "1")
    triggered = bridge.trigger_workflow_app_runtime_run(
        run_id="apprun-1",
        app_id="app-1",
        app_version=None,
        parameters={},
        task_id="task-1",
        org_id="",
        service=FakeRuntimeService(),
    )
    assert triggered is False
    assert app_run_store["writes"] == []


# ---------------------------------------------------------------------------
# 2. Stable idempotency association; retry cannot create a second Runtime run
# ---------------------------------------------------------------------------


async def test_idempotency_key_derived_from_app_run_id() -> None:
    svc = FakeRuntimeService(final_status="completed", result_payload={"summary": "done"})
    for _ in range(2):
        await bridge.run_app_workflow_on_runtime(
            app_run_id="apprun-7",
            app_id="app-1",
            app_version=None,
            parameters={"q": "hi"},
            task_id="task-7",
            org_id="org-1",
            service=svc,
        )
    create_calls = [c for c in svc.calls if c[0] == "create_run"]
    assert len(create_calls) == 2
    assert all(c[3] == "app-run:apprun-7" for c in create_calls)
    # The fake mirrors the Runtime's own (org_id, idempotency_key) uniqueness.
    assert len(svc.runs) == 1
    assert next(iter(svc.runs.values()))["run_id"] == "runtime-run-1"


async def test_create_and_start_use_trusted_org_and_task_id() -> None:
    svc = FakeRuntimeService(final_status="completed", result_payload={"summary": "ok"})
    await bridge.run_app_workflow_on_runtime(
        app_run_id="apprun-8",
        app_id="app-2",
        app_version=3,
        parameters={"q": "hi"},
        task_id="task-8",
        org_id="org-9",
        service=svc,
    )
    create_call = next(c for c in svc.calls if c[0] == "create_run")
    assert create_call[1] == "org-9"
    assert create_call[2] == "task-8"
    run = svc.runs["app-run:apprun-8"]
    assert run["input_payload"] == {
        "app_id": "app-2",
        "app_version": 3,
        "parameters": {"q": "hi"},
        "execution_mode": "workflow",
    }


# ---------------------------------------------------------------------------
# 3. Waiting approval → auto grant (workflow mode is already auto-authorized)
# ---------------------------------------------------------------------------


async def test_waiting_approval_is_granted_then_result_projected(
    app_run_store: dict[str, Any],
) -> None:
    _seed_run(app_run_store, "apprun-9")
    svc = FakeRuntimeService(final_status="completed", result_payload={"summary": "done"})
    outcome = await bridge.run_app_workflow_on_runtime(
        app_run_id="apprun-9",
        app_id="app-1",
        app_version=None,
        parameters={},
        task_id="task-9",
        org_id="org-1",
        service=svc,
    )
    assert ("grant_approval", "approval-runtime-run-1", "runtime-run-1") in svc.calls
    assert outcome["runtime_status"] == "completed"
    assert outcome["app_run_status"] == "completed"
    assert ("apprun-9", "completed", {"progress": 100, "result_summary": "done"}) in (
        app_run_store["writes"]
    )


# ---------------------------------------------------------------------------
# 4. Terminal projection onto existing App Run fields
# ---------------------------------------------------------------------------


async def test_completed_projection(app_run_store: dict[str, Any]) -> None:
    _seed_run(app_run_store, "apprun-1")
    svc = FakeRuntimeService(final_status="completed", result_payload={"summary": "ok"})
    outcome = await bridge.run_app_workflow_on_runtime(
        app_run_id="apprun-1",
        app_id="app-1",
        app_version=None,
        parameters={},
        task_id="task-1",
        org_id="org-1",
        service=svc,
    )
    assert outcome["app_run_status"] == "completed"
    assert ("apprun-1", "completed", {"progress": 100, "result_summary": "ok"}) in (
        app_run_store["writes"]
    )


async def test_failed_projection_writes_existing_error_field(
    app_run_store: dict[str, Any],
) -> None:
    _seed_run(app_run_store, "apprun-2")
    svc = FakeRuntimeService(
        final_status="failed",
        error_payload={"code": "agentscope_timeout", "message": "execution timed out"},
    )
    outcome = await bridge.run_app_workflow_on_runtime(
        app_run_id="apprun-2",
        app_id="app-1",
        app_version=None,
        parameters={},
        task_id="task-2",
        org_id="org-1",
        service=svc,
    )
    assert outcome["app_run_status"] == "failed"
    assert ("apprun-2", "failed", {"error": "execution timed out"}) in (
        app_run_store["writes"]
    )


async def test_timed_out_maps_to_existing_failed_enum(
    app_run_store: dict[str, Any],
) -> None:
    _seed_run(app_run_store, "apprun-3")
    svc = FakeRuntimeService(final_status="timed_out", error_payload={"code": "timeout"})
    outcome = await bridge.run_app_workflow_on_runtime(
        app_run_id="apprun-3",
        app_id="app-1",
        app_version=None,
        parameters={},
        task_id="task-3",
        org_id="org-1",
        service=svc,
    )
    # No new status enum is invented: timed_out → the existing failed terminal.
    assert outcome["app_run_status"] == "failed"
    assert ("apprun-3", "failed", {"error": "timeout"}) in app_run_store["writes"]


async def test_cancelled_projection(app_run_store: dict[str, Any]) -> None:
    _seed_run(app_run_store, "apprun-4")
    svc = FakeRuntimeService(final_status="cancelled", error_payload="user cancelled")
    outcome = await bridge.run_app_workflow_on_runtime(
        app_run_id="apprun-4",
        app_id="app-1",
        app_version=None,
        parameters={},
        task_id="task-4",
        org_id="org-1",
        service=svc,
    )
    assert outcome["app_run_status"] == "cancelled"
    assert ("apprun-4", "cancelled", {"error": "user cancelled"}) in (
        app_run_store["writes"]
    )


async def test_terminal_app_run_is_never_regressed(app_run_store: dict[str, Any]) -> None:
    _seed_run(app_run_store, "apprun-5", status="completed")
    svc = FakeRuntimeService(final_status="failed", error_payload={"code": "x"})
    outcome = await bridge.run_app_workflow_on_runtime(
        app_run_id="apprun-5",
        app_id="app-1",
        app_version=None,
        parameters={},
        task_id="task-5",
        org_id="org-1",
        service=svc,
    )
    assert outcome["app_run_status"] == "completed"
    assert app_run_store["writes"] == []


# ---------------------------------------------------------------------------
# 5. Runtime failure → existing failed-terminal write path, no legacy fallback
# ---------------------------------------------------------------------------


async def test_runtime_failure_propagates_and_marks_failed_terminal(
    monkeypatch: pytest.MonkeyPatch, app_run_store: dict[str, Any]
) -> None:
    _seed_run(app_run_store, "apprun-6")
    svc = FakeRuntimeService(fail_on="create_run")
    with pytest.raises(RuntimeError):
        await bridge.run_app_workflow_on_runtime(
            app_run_id="apprun-6",
            app_id="app-1",
            app_version=None,
            parameters={},
            task_id="task-6",
            org_id="org-1",
            service=svc,
        )
    bridge.mark_app_run_failed_terminal("apprun-6", "Runtime bridge execution failed: boom")
    assert ("apprun-6", "failed", {"error": "Runtime bridge execution failed: boom"}) in (
        app_run_store["writes"]
    )


def test_mark_failed_terminal_never_overwrites_terminal(
    app_run_store: dict[str, Any],
) -> None:
    _seed_run(app_run_store, "apprun-10", status="cancelled")
    bridge.mark_app_run_failed_terminal("apprun-10", "late failure")
    assert app_run_store["writes"] == []


# ---------------------------------------------------------------------------
# 6. Task Center polling never regresses a terminal App Run
# ---------------------------------------------------------------------------


def test_sync_run_from_task_does_not_regress_terminal(
    monkeypatch: pytest.MonkeyPatch, app_run_store: dict[str, Any]
) -> None:
    from evoflow.collab.app_runner import _sync_run_from_task
    from evoflow.persistence import app_repositories

    run = {"id": "apprun-11", "status": "completed", "progress": 100}
    status = _sync_run_from_task(run, task_status="planned", progress=0)
    assert status == "completed"
    assert run["status"] == "completed"
    # The real write point must not have been reached either.
    assert app_repositories.update_run_status is not None

    run_failed = {"id": "apprun-12", "status": "failed", "progress": 40}
    status = _sync_run_from_task(run_failed, task_status="executing", progress=50)
    assert status == "failed"
    assert run_failed["status"] == "failed"


def test_sync_run_from_task_still_fixes_completed_progress(
    monkeypatch: pytest.MonkeyPatch, app_run_store: dict[str, Any]
) -> None:
    from evoflow.collab.app_runner import _sync_run_from_task

    recorded: list[tuple[Any, ...]] = []
    from evoflow.persistence import app_repositories

    def fake_update(run_id: str, status: str, **fields: Any) -> None:
        recorded.append((run_id, status, fields))

    monkeypatch.setattr(app_repositories, "update_run_status", fake_update)

    run = {"id": "apprun-13", "status": "completed", "progress": 99}
    status = _sync_run_from_task(run, task_status="planned", progress=10)
    assert status == "completed"
    assert recorded == [("apprun-13", "completed", {"progress": 100})]
    assert run["progress"] == 100


# ---------------------------------------------------------------------------
# 7. Detached trigger path owns the run only when enabled
# ---------------------------------------------------------------------------


def test_trigger_enabled_schedules_and_reports_ownership(
    monkeypatch: pytest.MonkeyPatch, app_run_store: dict[str, Any]
) -> None:
    monkeypatch.setenv("EVOFLOW_APP_WORKFLOW_RUNTIME", "1")
    scheduled: list[str] = []

    import evoflow.subagents.detached_poll_scheduler as dps

    def fake_schedule(coro: Any, *, name: str | None = None) -> None:
        scheduled.append(str(name))
        coro.close()  # do not execute in this unit test

    monkeypatch.setattr(dps, "schedule_detached_poll", fake_schedule)

    triggered = bridge.trigger_workflow_app_runtime_run(
        run_id="apprun-14",
        app_id="app-1",
        app_version=None,
        parameters={"q": "hi"},
        task_id="task-14",
        org_id="org-1",
        service=FakeRuntimeService(),
    )
    assert triggered is True
    assert scheduled == ["app-runtime-apprun-14"]
    asyncio.run(asyncio.sleep(0))


# ---------------------------------------------------------------------------
# 8. AG-G2-APP-005-A01 — results readable through the *existing* App Run
#    query path (real evoflow_app_runs sqlite row, no new API/table/field).
# ---------------------------------------------------------------------------


@pytest.fixture()
def real_run_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolated temp evoflow_app_runs sqlite database per test."""
    import tempfile

    from evoflow.persistence.db import reset_db_for_tests

    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("EVOFLOW_HOME", tmp)
        reset_db_for_tests()
        yield
        reset_db_for_tests()
        import gc

        gc.collect()


def _persist_run(run_id: str, *, status: str = "planned") -> None:
    from evoflow.persistence import app_repositories

    # evoflow_app_runs carries a FK to evoflow_apps — seed the app first.
    app_repositories.save_app(
        "app-obs-1",
        {
            "name": "Obs Demo",
            "steps": [{"ref": "1", "goal": "g", "tools": "a,b", "depends_on": []}],
            "parameters": [],
            "version": 1,
            "status": "published",
        },
    )
    app_repositories.save_run(
        run_id,
        {
            "app_id": "app-obs-1",
            "task_id": f"task-{run_id}",
            "status": status,
            "progress": 0,
        },
    )


async def _execute_bridge(run_id: str, svc: FakeRuntimeService) -> dict[str, Any]:
    return await bridge.run_app_workflow_on_runtime(
        app_run_id=run_id,
        app_id="app-obs-1",
        app_version=None,
        parameters={},
        task_id=f"task-{run_id}",
        org_id="org-1",
        service=svc,
    )


async def test_completed_result_readable_via_existing_query_path(
    real_run_db: None,
) -> None:
    from evoflow.persistence import app_repositories

    _persist_run("apprun-obs-1")
    outcome = await _execute_bridge(
        "apprun-obs-1",
        FakeRuntimeService(final_status="completed", result_payload={"summary": "季度汇总完成"}),
    )
    assert outcome["app_run_status"] == "completed"
    run = app_repositories.load_run("apprun-obs-1")
    assert run is not None
    assert run["status"] == "completed"
    assert int(run["progress"]) == 100
    assert run["result_summary"] == "季度汇总完成"
    assert run["completed_at"]


async def test_failed_result_readable_with_error_via_existing_query_path(
    real_run_db: None,
) -> None:
    from evoflow.persistence import app_repositories

    _persist_run("apprun-obs-2")
    await _execute_bridge(
        "apprun-obs-2",
        FakeRuntimeService(
            final_status="failed",
            error_payload={"code": "boom", "message": "LLM provider unreachable"},
        ),
    )
    run = app_repositories.load_run("apprun-obs-2")
    assert run is not None
    assert run["status"] == "failed"
    assert run["error"] == "LLM provider unreachable"
    assert run["completed_at"]


async def test_timed_out_maps_to_failed_with_nonempty_error(real_run_db: None) -> None:
    from evoflow.persistence import app_repositories

    _persist_run("apprun-obs-3")
    await _execute_bridge(
        "apprun-obs-3",
        FakeRuntimeService(
            final_status="timed_out",
            error_payload={"code": "timeout", "message": "Runtime run timed out"},
        ),
    )
    run = app_repositories.load_run("apprun-obs-3")
    assert run is not None
    # Frozen semantics: timed_out is never a new enum, it is the existing failed.
    assert run["status"] == "failed"
    assert run["error"]
    assert run["completed_at"]


async def test_cancelled_survives_late_task_center_polling(real_run_db: None) -> None:
    from evoflow.collab.app_runner import _sync_run_from_task
    from evoflow.persistence import app_repositories

    _persist_run("apprun-obs-4")
    await _execute_bridge("apprun-obs-4", FakeRuntimeService(final_status="cancelled"))
    run = app_repositories.load_run("apprun-obs-4")
    assert run is not None and run["status"] == "cancelled"

    # A late Task Center poll claiming the task completed must not win.
    status = _sync_run_from_task(run, task_status="completed", progress=100)
    assert status == "cancelled"
    reread = app_repositories.load_run("apprun-obs-4")
    assert reread is not None
    assert reread["status"] == "cancelled"


def test_switch_off_creates_no_runtime_run_and_leaves_run_untouched(
    monkeypatch: pytest.MonkeyPatch, real_run_db: None
) -> None:
    from evoflow.persistence import app_repositories

    monkeypatch.delenv("EVOFLOW_APP_WORKFLOW_RUNTIME", raising=False)
    _persist_run("apprun-obs-5")
    svc = FakeRuntimeService()

    triggered = bridge.trigger_workflow_app_runtime_run(
        run_id="apprun-obs-5",
        app_id="app-obs-1",
        app_version=None,
        parameters={},
        task_id="task-apprun-obs-5",
        org_id="org-1",
        service=svc,
    )
    assert triggered is False
    assert svc.calls == []  # zero Runtime reads and zero Runtime writes
    run = app_repositories.load_run("apprun-obs-5")
    assert run is not None
    assert run["status"] == "planned"  # legacy path untouched by the bridge


# ---------------------------------------------------------------------------
# 9. AG-G2-APP-006-A01 — failure boundaries, retries and terminal monotonicity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fail_on", ["create_run", "start_run", "grant_approval", "get_result"]
)
def test_runtime_call_failure_lands_on_failed_terminal_never_legacy(
    monkeypatch: pytest.MonkeyPatch,
    app_run_store: dict[str, Any],
    fail_on: str,
) -> None:
    """Every Runtime call boundary that can fail ends in the existing failed
    terminal write path through the detached wrapper — never a silent legacy
    re-execution and never a stuck run."""
    monkeypatch.setenv("EVOFLOW_APP_WORKFLOW_RUNTIME", "1")
    run_id = f"apprun-fail-{fail_on}"
    _seed_run(app_run_store, run_id)
    svc = FakeRuntimeService(fail_on=fail_on)

    import evoflow.subagents.detached_poll_scheduler as dps

    captured: dict[str, Any] = {}

    def fake_schedule(coro: Any, *, name: str | None = None) -> None:
        captured["coro"] = coro

    monkeypatch.setattr(dps, "schedule_detached_poll", fake_schedule)

    triggered = bridge.trigger_workflow_app_runtime_run(
        run_id=run_id,
        app_id="app-1",
        app_version=None,
        parameters={},
        task_id=f"task-{run_id}",
        org_id="org-1",
        service=svc,
    )
    assert triggered is True
    asyncio.run(captured["coro"])

    run = app_run_store["runs"][run_id]
    assert run["status"] == "failed"
    assert "Runtime bridge execution failed" in str(run["error"])


@pytest.mark.parametrize("final_status", ["completed", "failed", "cancelled", "timed_out"])
async def test_repeated_terminal_projection_is_idempotent_and_monotonic(
    app_run_store: dict[str, Any], final_status: str
) -> None:
    """A repeated Runtime terminal projection neither regresses the App Run
    nor produces duplicate side-effect writes."""
    run_id = f"apprun-rep-{final_status}"
    _seed_run(app_run_store, run_id)
    svc = FakeRuntimeService(final_status=final_status)
    await bridge.run_app_workflow_on_runtime(
        app_run_id=run_id,
        app_id="app-1",
        app_version=None,
        parameters={},
        task_id=f"task-{run_id}",
        org_id="org-1",
        service=svc,
    )
    first_run = app_run_store["runs"][run_id]
    expected_terminal = "failed" if final_status == "timed_out" else final_status
    assert first_run["status"] == expected_terminal
    writes_after_first = len(app_run_store["writes"])

    # Repeat the exact same projection (e.g. a retried trigger reading the
    # same Runtime result).
    result = await svc.get_result(svc.runs[bridge.app_run_idempotency_key(run_id)]["run_id"])
    projected = bridge.project_runtime_result_to_app_run(run_id, result)
    assert projected == expected_terminal
    assert len(app_run_store["writes"]) == writes_after_first  # no duplicate write
    # An already-terminal App Run is never flipped back to executing/planned.
    late_poll = dict(app_run_store["runs"][run_id])
    from evoflow.collab.app_runner import _sync_run_from_task

    assert _sync_run_from_task(late_poll, task_status="executing", progress=50) == expected_terminal


def test_same_org_retry_reuses_runtime_run_in_real_repository() -> None:
    """Same org + same app_run_id → same idempotency key → the Runtime
    repository (real, in-memory sqlite) returns the existing run instead of
    creating a second one."""
    import sqlalchemy as sa
    from sqlalchemy.pool import StaticPool

    from app.qagent_runtime.repository import RuntimeRepository

    engine = sa.create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    repo = RuntimeRepository(engine, create_schema=True)
    key = bridge.app_run_idempotency_key("apprun-retry-1")

    first = repo.create_run(org_id="org-1", task_id="t1", input_payload={}, idempotency_key=key)
    retry = repo.create_run(org_id="org-1", task_id="t1", input_payload={}, idempotency_key=key)
    assert retry["run_id"] == first["run_id"]
    assert retry["task_id"] == "t1"
    engine.dispose()


def test_cross_org_same_app_run_id_is_isolated_in_real_repository() -> None:
    """Cross org with the identical app_run_id text: the Runtime repository
    scopes idempotency by org_id, so no run is shared or reused."""
    import sqlalchemy as sa
    from sqlalchemy.pool import StaticPool

    from app.qagent_runtime.repository import RuntimeRepository

    engine = sa.create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    repo = RuntimeRepository(engine, create_schema=True)
    key = bridge.app_run_idempotency_key("apprun-shared-text")

    org_a = repo.create_run(org_id="org-a", task_id="t", input_payload={}, idempotency_key=key)
    org_a_retry = repo.create_run(org_id="org-a", task_id="t", input_payload={}, idempotency_key=key)
    org_b = repo.create_run(org_id="org-b", task_id="t", input_payload={}, idempotency_key=key)
    assert org_a_retry["run_id"] == org_a["run_id"]
    assert org_b["run_id"] != org_a["run_id"]
    assert org_b["org_id"] == "org-b"
    engine.dispose()


# ---------------------------------------------------------------------------
# 7. Cancel propagation (AG-G2-APP-012-A01)
# ---------------------------------------------------------------------------


class CancelRecordingRuntimeService(FakeRuntimeService):
    """FakeRuntimeService plus the public ``request_cancel`` surface."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.cancel_calls: list[tuple[str, str | None]] = []
        self.fail_cancel = False

    async def request_cancel(self, run_id: str, org_id: str | None = None) -> dict[str, Any]:
        if self.fail_cancel:
            raise RuntimeError("runtime cancel unavailable")
        self.cancel_calls.append((run_id, org_id))
        run = self._find(run_id)
        run["status"] = "cancelled"
        return {"run_id": run_id, "status": run["status"]}


def _capture_detached(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    captured: list[Any] = []

    def fake_schedule(coro: Any, *, name: str | None = None) -> None:
        captured.append(coro)

    monkeypatch.setattr(
        "evoflow.subagents.detached_poll_scheduler.schedule_detached_poll", fake_schedule
    )
    return captured


def test_cancel_propagates_to_live_runtime_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc = CancelRecordingRuntimeService()
    bridge._register_inflight("apprun-cancel-1", "runtime-run-9", "org-3", svc)
    try:
        captured = _capture_detached(monkeypatch)
        assert bridge.cancel_app_run_runtime("apprun-cancel-1", "user cancelled") is True
        assert len(captured) == 1
        asyncio.run(captured[0])
        assert svc.cancel_calls == [("runtime-run-9", "org-3")]
    finally:
        bridge._forget_inflight("apprun-cancel-1")


def test_cancel_without_runtime_association_is_noop() -> None:
    assert bridge.cancel_app_run_runtime("apprun-never-bridged", "bye") is False


async def test_cancel_after_execution_finished_is_noop() -> None:
    """The in-process association lives exactly as long as the execution."""
    svc = CancelRecordingRuntimeService(final_status="completed", result_payload={"s": "x"})
    await bridge.run_app_workflow_on_runtime(
        app_run_id="apprun-cancel-2",
        app_id="app-1",
        app_version=None,
        parameters={},
        task_id="task-1",
        org_id="org-1",
        service=svc,
    )
    assert bridge.cancel_app_run_runtime("apprun-cancel-2", "late") is False
    assert svc.cancel_calls == []


async def test_propagate_swallows_runtime_cancel_failure() -> None:
    svc = CancelRecordingRuntimeService()
    svc.fail_cancel = True
    await bridge._propagate_runtime_cancel("runtime-run-x", "org-1", svc, "reason")
    assert svc.cancel_calls == []


def test_legacy_cancel_propagates_when_bridge_owns_run(
    monkeypatch: pytest.MonkeyPatch, app_run_store: dict[str, Any]
) -> None:
    from evoflow.collab.app_runner import cancel_run

    monkeypatch.setenv("EVOFLOW_APP_WORKFLOW_RUNTIME", "1")
    _seed_run(app_run_store, "apprun-cancel-3", status="running")
    recorded: dict[str, str] = {}

    def fake_cancel(run_id: str, reason: str = "") -> bool:
        recorded["run_id"] = run_id
        recorded["reason"] = reason
        return True

    monkeypatch.setattr(bridge, "cancel_app_run_runtime", fake_cancel)
    assert cancel_run("apprun-cancel-3", "user cancelled") is True
    assert recorded == {"run_id": "apprun-cancel-3", "reason": "user cancelled"}
    cancelled = [w for w in app_run_store["writes"] if w[1] == "cancelled"]
    assert cancelled and cancelled[0][0] == "apprun-cancel-3"


def test_legacy_cancel_without_bridge_association_skips_propagation(
    monkeypatch: pytest.MonkeyPatch, app_run_store: dict[str, Any]
) -> None:
    from evoflow.collab.app_runner import cancel_run

    monkeypatch.setenv("EVOFLOW_APP_WORKFLOW_RUNTIME", "1")
    _seed_run(app_run_store, "apprun-cancel-4", status="running")
    monkeypatch.setattr(
        bridge,
        "cancel_app_run_runtime",
        lambda run_id, reason="": (_ for _ in ()).throw(AssertionError("must not be called")),
    )
    assert cancel_run("apprun-cancel-4", "user cancelled") is True
    assert app_run_store["writes"][-1][1] == "cancelled"
