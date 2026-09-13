"""Vertical integration: an unattended Task Center task driven by the Runtime.

AG-G2-AUTO-030.

Every step goes through an entry that already exists, in the order a real user
would cause them:

1. the task is created through the real Task Center route (``POST /api/tasks``);
2. execution is authorized through the existing authorization service, which is
   the same gate the "start execution" control uses;
3. the unattended pipeline advances the task through
   ``advance_unattended_task`` — the function the queue tick calls — with the
   Runtime opt-in switch on;
4. the Runtime's own frames are pushed through the existing event adapter;
5. the result is read back through the real routes.

Nothing hand-writes a final SQLite state, and nothing here touches the network: a
recording contract stands in for the Runtime at its public boundary, so no real
API key is read and no paid call can happen. The Runtime remains the source of
truth for run state — the Task Center only keeps the projection of it.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="the real Task Center routes need FastAPI")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# A throwaway home before anything resolves the real data directory.
os.environ.setdefault("EVOFLOW_HOME", tempfile.mkdtemp(prefix="qagent-unattended-"))

from app.gateway import task_runtime_optin as optin  # noqa: E402
from app.gateway.deps.license import require_premium  # noqa: E402
from app.gateway.routers.tasks import router  # noqa: E402
from app.gateway.task_runtime_context import build_task_runtime_context  # noqa: E402
from app.gateway.task_runtime_event_bridge import apply_runtime_event  # noqa: E402
from app.gateway.task_runtime_linkage import LINKAGE_TASK_KEY  # noqa: E402
from app.gateway.task_runtime_projection import HISTORY_FIELD  # noqa: E402

SWITCH = "EVOFLOW_AUTOMATION_UNATTENDED_RUNTIME"
RUN_ID = "run-e2e-success"


class FakeRuntime:
    """The Runtime's public boundary, recorded. No I/O, no key, no network."""

    def __init__(self, *, result: dict[str, Any] | None = None) -> None:
        self.create_calls: list[dict[str, Any]] = []
        self.status_calls: list[str] = []
        self._result = result if result is not None else {"summary": "周报已生成"}
        self._status = "running"

    async def create_run(
        self,
        *,
        org_id: str,
        task_id: str,
        input_payload: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self.create_calls.append(
            {
                "org_id": org_id,
                "task_id": task_id,
                "input_payload": dict(input_payload),
                "idempotency_key": idempotency_key,
            }
        )
        return {"run_id": RUN_ID, "status": "queued", "org_id": org_id}

    async def get_run_status(self, run_id: str) -> dict[str, Any]:
        self.status_calls.append(run_id)
        return {"run_id": run_id, "status": self._status}

    def complete(self) -> list[dict[str, Any]]:
        """The frames the Runtime would emit for a successful run."""
        self._status = "completed"
        return [
            {"type": "run.running", "run_id": RUN_ID, "sequence": 0, "event_id": "evt-0"},
            {
                "type": "run.completed",
                "run_id": RUN_ID,
                "sequence": 1,
                "event_id": "evt-1",
                "payload": {"result": self._result},
            },
        ]


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_premium] = lambda: None
    return TestClient(app)


@pytest.fixture(autouse=True)
def _fresh_home(monkeypatch: pytest.MonkeyPatch) -> None:
    from evoflow.persistence.db import reset_db_for_tests

    monkeypatch.setenv(SWITCH, "1")
    reset_db_for_tests()


def _storage():
    from evoflow.collab.storage import get_project_storage

    return get_project_storage()


def _stored_task(task_id: str) -> dict[str, Any]:
    for summary in _storage().list_projects():
        project = _storage().load_project(summary["id"])
        for task in (project or {}).get("tasks") or []:
            if task.get("id") == task_id:
                return dict(task)
    raise AssertionError(f"task {task_id} not found")


def _save_task(task_id: str, updated: dict[str, Any]) -> None:
    storage = _storage()
    for summary in storage.list_projects():
        project = storage.load_project(summary["id"])
        tasks = (project or {}).get("tasks") or []
        for index, task in enumerate(tasks):
            if task.get("id") == task_id:
                tasks[index] = updated
                storage.save_project(project)
                return
    raise AssertionError(f"task {task_id} not found")


def _create_unattended_task(client: TestClient) -> str:
    response = client.post(
        "/api/tasks",
        json={"name": "每周经营简报", "description": "给管理层的周报", "run_mode": "unattended"},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["id"])


def _authorize(task_id: str) -> None:
    from evoflow.collab.authorize_execution import authorize_main_task_execution

    ok, message = authorize_main_task_execution(_storage(), task_id, "user")
    assert ok, message


def _tick(task_id: str) -> dict[str, Any]:
    from app.gateway.unattended_task_pipeline import advance_unattended_task

    return asyncio.run(advance_unattended_task(task_id))


def _push_frames(task_id: str, frames: list[dict[str, Any]]) -> dict[str, Any]:
    """Project the Runtime's frames the way the stream consumer does."""
    from evoflow.collab.storage import find_main_task

    row = find_main_task(_storage(), task_id)
    assert row is not None
    task = dict(row[1])
    for frame in frames:
        context = build_task_runtime_context(
            task=task, authz=_identity(), authorized=True
        )
        task, _ = apply_runtime_event(task, context=context, event=frame)
    _save_task(task_id, task)
    return task


def _identity() -> dict[str, Any]:
    return {
        "org_id": "local",
        "scope_id": "personal:local-admin",
        "principal": {"principal_id": "local-admin", "principal_type": "internal"},
    }


# --- the happy path --------------------------------------------------------------


def test_an_unattended_task_reaches_the_runtime_once(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRuntime()
    monkeypatch.setattr(optin, "default_runtime_contract", lambda: fake)

    task_id = _create_unattended_task(client)
    _authorize(task_id)
    step = _tick(task_id)

    assert step["action"] == "runtime", step
    assert step["runtime_run_id"] == RUN_ID
    assert len(fake.create_calls) == 1


def test_the_run_carries_the_trusted_identity_and_a_deterministic_trigger(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRuntime()
    monkeypatch.setattr(optin, "default_runtime_contract", lambda: fake)

    task_id = _create_unattended_task(client)
    _authorize(task_id)
    _tick(task_id)

    call = fake.create_calls[0]
    assert call["org_id"] == "local"
    assert call["idempotency_key"]
    # No client-supplied field may leak into the identity/trigger.
    assert "org_id" not in call["input_payload"]
    assert "tenant_id" not in call["input_payload"]


def test_the_linkage_is_persisted_with_the_task(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRuntime()
    monkeypatch.setattr(optin, "default_runtime_contract", lambda: fake)

    task_id = _create_unattended_task(client)
    _authorize(task_id)
    _tick(task_id)

    stored = _stored_task(task_id)

    assert stored[LINKAGE_TASK_KEY]["runtime_run_id"] == RUN_ID
    # Establishing the run writes the linkage and nothing else: the task's own
    # status is the one the existing authorization left it in, and no legacy
    # execution has written history.
    assert stored["status"] == "planned"
    assert stored[HISTORY_FIELD] == []


def test_a_second_tick_does_not_create_a_second_run(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRuntime()
    monkeypatch.setattr(optin, "default_runtime_contract", lambda: fake)

    task_id = _create_unattended_task(client)
    _authorize(task_id)
    _tick(task_id)
    _tick(task_id)

    assert len(fake.create_calls) == 1


def test_the_projection_completes_the_task_with_a_sanitised_result(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRuntime(
        result={"summary": "周报已生成", "internal_path": "/var/private/report.csv"}
    )
    monkeypatch.setattr(optin, "default_runtime_contract", lambda: fake)

    task_id = _create_unattended_task(client)
    _authorize(task_id)
    _tick(task_id)
    _push_frames(task_id, fake.complete())

    stored = _stored_task(task_id)

    assert stored["status"] == "completed"
    record = stored[HISTORY_FIELD][-1]
    assert record["runtime_status"] == "completed"
    assert record["result_available"] is True
    assert record["result"] == {"summary": "周报已生成"}
    assert "/var/private/report.csv" not in json.dumps(stored, ensure_ascii=False)


def test_the_completed_task_reads_back_through_the_real_routes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRuntime()
    monkeypatch.setattr(optin, "default_runtime_contract", lambda: fake)

    task_id = _create_unattended_task(client)
    _authorize(task_id)
    _tick(task_id)
    _push_frames(task_id, fake.complete())

    history = client.get(f"/api/tasks/{task_id}/execution-history").json()
    body = client.get(f"/api/tasks/{task_id}").json()

    assert history["total_executions"] == 2
    assert [e["runtime_status"] for e in history["execution_history"]] == [
        "running",
        "completed",
    ]
    assert body["status"] == "completed"
    # The Runtime is the source of truth; the task only carries the projection of it.
    assert "runtime_run_linkage" not in body


def test_the_whole_flow_leaves_a_consistent_terminal_task(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRuntime()
    monkeypatch.setattr(optin, "default_runtime_contract", lambda: fake)

    task_id = _create_unattended_task(client)
    _authorize(task_id)
    _tick(task_id)
    _push_frames(task_id, fake.complete())

    stored = _stored_task(task_id)

    assert stored["status"] == "completed"
    assert stored[HISTORY_FIELD], "the projection must be visible"
    assert all(
        entry.get("runtime_run_id") == RUN_ID for entry in stored[HISTORY_FIELD]
    )


# --- the switch, which keeps the legacy path intact -------------------------------


def test_with_the_switch_off_the_legacy_path_runs_and_no_run_is_created(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRuntime()
    monkeypatch.setattr(optin, "default_runtime_contract", lambda: fake)
    monkeypatch.setenv(SWITCH, "0")

    task_id = _create_unattended_task(client)
    _authorize(task_id)
    step = _tick(task_id)

    assert step.get("action") != "runtime"
    assert fake.create_calls == []
    assert LINKAGE_TASK_KEY not in _stored_task(task_id)


def test_an_unauthorized_task_never_reaches_the_runtime(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRuntime()
    monkeypatch.setattr(optin, "default_runtime_contract", lambda: fake)

    task_id = _create_unattended_task(client)
    step = _tick(task_id)  # not authorized

    assert fake.create_calls == []
    assert step.get("action") != "runtime"


def test_a_handwritten_organization_on_the_task_is_ignored(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client-writable field must not be able to choose the run's org."""
    fake = FakeRuntime()
    monkeypatch.setattr(optin, "default_runtime_contract", lambda: fake)

    task_id = _create_unattended_task(client)
    _authorize(task_id)
    stored = _stored_task(task_id)
    stored["org_id"] = "someone-elses-org"
    stored["tenant_id"] = "someone-elses-tenant"
    _save_task(task_id, stored)

    _tick(task_id)

    assert fake.create_calls[0]["org_id"] == "local"
