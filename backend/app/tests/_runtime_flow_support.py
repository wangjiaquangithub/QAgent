"""Shared scaffolding for the Runtime <-> unattended Task Center flow tests.

Not a test module (the name does not start with ``test_``), and not production
code: it is the small amount of plumbing the vertical integration tests for
AG-G2-AUTO-030..035 share, so each of those files states only what it asserts.

Everything here goes through an entry that already exists — the real Task Center
routes, the existing authorization service, the unattended pipeline, the existing
event adapter and the existing project-storage save path. Nothing hand-writes a
final SQLite state, and the Runtime is always replaced at its public boundary by a
recording fake, so no test reads a real API key or makes a paid call.

Imported by sibling test modules as ``_runtime_flow_support``: pytest prepends
this directory (which has no ``__init__.py``) to ``sys.path`` before importing a
test module, so the bare module name resolves without a package.
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

# A throwaway home before anything resolves the real data directory, so importing
# this support module can never point a test at the developer's data.
os.environ.setdefault("EVOFLOW_HOME", tempfile.mkdtemp(prefix="qagent-flow-"))

from app.gateway import task_runtime_optin as optin  # noqa: E402
from app.gateway.deps.license import require_premium  # noqa: E402
from app.gateway.routers.tasks import router  # noqa: E402
from app.gateway.task_runtime_context import build_task_runtime_context  # noqa: E402
from app.gateway.task_runtime_event_bridge import apply_runtime_event  # noqa: E402
from app.gateway.task_runtime_linkage import LINKAGE_TASK_KEY  # noqa: E402
from app.gateway.task_runtime_projection import HISTORY_FIELD  # noqa: E402

__all__ = [
    "HISTORY_FIELD",
    "LINKAGE_TASK_KEY",
    "RUN_ID",
    "SWITCH",
    "FakeRuntime",
    "authorize",
    "build_client",
    "create_unattended_task",
    "frame",
    "history_of",
    "identity",
    "json_of",
    "patch_task",
    "push_frames",
    "reset_home",
    "run_contract",
    "save_task",
    "statuses_of",
    "storage",
    "stored_task",
    "tick",
]

SWITCH = "EVOFLOW_AUTOMATION_UNATTENDED_RUNTIME"
RUN_ID = "run-flow-1"
LOCAL_ORG = "local"
LOCAL_OWNER = "local-admin"


class FakeRuntime:
    """The Runtime's public boundary, recorded. No I/O, no key, no network.

    ``frames`` is what the run would emit; it is scripted by the test so the
    success, approval, failure and cancellation flows can each be driven from the
    same stand-in.
    """

    def __init__(
        self,
        *,
        frames: list[dict[str, Any]] | None = None,
        status: str = "queued",
        create_error: BaseException | None = None,
        status_error: BaseException | None = None,
    ) -> None:
        self.create_calls: list[dict[str, Any]] = []
        self.status_calls: list[str] = []
        self.cancel_calls: list[str] = []
        self._frames = list(frames or [])
        self._status = status
        self._create_error = create_error
        self._status_error = status_error

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
        if self._create_error is not None:
            raise self._create_error
        return {"run_id": RUN_ID, "status": "queued", "org_id": org_id}

    async def get_run_status(self, run_id: str) -> dict[str, Any]:
        self.status_calls.append(run_id)
        if self._status_error is not None:
            raise self._status_error
        return {
            "run_id": run_id,
            "status": self._status,
            "org_id": LOCAL_ORG,
            "result": self._result_of_last_frame(),
        }

    async def request_cancel(self, run_id: str) -> dict[str, Any]:
        self.cancel_calls.append(run_id)
        self._status = "cancelled"
        return {"run_id": run_id, "status": "cancelled"}

    def _result_of_last_frame(self) -> Any:
        for frame in reversed(self._frames):
            payload = frame.get("payload")
            if isinstance(payload, dict) and "result" in payload:
                return payload["result"]
        return None

    @property
    def frames(self) -> list[dict[str, Any]]:
        return list(self._frames)


def run_contract(fake: FakeRuntime, monkeypatch: pytest.MonkeyPatch) -> FakeRuntime:
    """Point the real call site at the fake, at the Runtime's public boundary."""
    monkeypatch.setattr(optin, "default_runtime_contract", lambda: fake)
    return fake


def reset_home() -> None:
    from evoflow.persistence.db import reset_db_for_tests

    reset_db_for_tests()


def build_client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    # The license gate is not what these flows are about, and it is a plain
    # dependency, so it is overridden rather than faked at the license layer.
    app.dependency_overrides[require_premium] = lambda: None
    return TestClient(app)


def storage():
    from evoflow.collab.storage import get_project_storage

    return get_project_storage()


def stored_task(task_id: str) -> dict[str, Any]:
    store = storage()
    for summary in store.list_projects():
        project = store.load_project(summary["id"])
        for task in (project or {}).get("tasks") or []:
            if task.get("id") == task_id:
                return dict(task)
    raise AssertionError(f"task {task_id} not found")


def save_task(task_id: str, updated: dict[str, Any]) -> None:
    """Persist through the same storage call the router itself uses."""
    store = storage()
    for summary in store.list_projects():
        project = store.load_project(summary["id"])
        tasks = (project or {}).get("tasks") or []
        for index, task in enumerate(tasks):
            if task.get("id") == task_id:
                tasks[index] = updated
                store.save_project(project)
                return
    raise AssertionError(f"task {task_id} not found")


def patch_task(task_id: str, mutate) -> dict[str, Any]:
    updated = dict(stored_task(task_id))
    mutate(updated)
    save_task(task_id, updated)
    return updated


def create_unattended_task(client: TestClient, **extra: Any) -> str:
    body: dict[str, Any] = {
        "name": "每周经营简报",
        "description": "给管理层的周报",
        "run_mode": "unattended",
    }
    body.update(extra)
    response = client.post("/api/tasks", json=body)
    assert response.status_code == 200, response.text
    return str(response.json()["id"])


def authorize(task_id: str, *, actor: str = "user") -> None:
    from evoflow.collab.authorize_execution import authorize_main_task_execution

    ok, message = authorize_main_task_execution(storage(), task_id, actor)
    assert ok, message


def tick(task_id: str) -> dict[str, Any]:
    """One real unattended pipeline step — what the queue tick calls."""
    from app.gateway.unattended_task_pipeline import advance_unattended_task

    return asyncio.run(advance_unattended_task(task_id))


def identity(org_id: str = LOCAL_ORG, owner: str = LOCAL_OWNER) -> dict[str, Any]:
    return {
        "org_id": org_id,
        "scope_id": f"personal:{owner}",
        "principal": {"principal_id": owner, "principal_type": "internal"},
    }


def frame(
    event_type: str,
    *,
    sequence: int,
    payload: dict[str, Any] | None = None,
    event_id: str | None = None,
    run_id: str = RUN_ID,
) -> dict[str, Any]:
    return {
        "type": event_type,
        "run_id": run_id,
        "sequence": sequence,
        "event_id": event_id or f"evt-{sequence}",
        "payload": payload or {},
    }


def push_frames(task_id: str, frames: list[dict[str, Any]]) -> dict[str, Any]:
    """Project the Runtime's frames the way the stream consumer does."""
    from evoflow.collab.storage import find_main_task

    row = find_main_task(storage(), task_id)
    assert row is not None
    task = dict(row[1])
    for item in frames:
        context = build_task_runtime_context(task=task, authz=identity(), authorized=True)
        task, _ = apply_runtime_event(task, context=context, event=item)
    save_task(task_id, task)
    return task


def history_of(task: dict[str, Any]) -> list[dict[str, Any]]:
    entries = task.get(HISTORY_FIELD)
    return list(entries) if isinstance(entries, list) else []


def statuses_of(task: dict[str, Any]) -> list[str]:
    return [str(entry.get("runtime_status")) for entry in history_of(task)]


def json_of(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
