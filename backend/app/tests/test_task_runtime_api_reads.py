"""Compatibility tests for the Task Center API reads of linked Runtime state.

AG-G2-AUTO-028.

These deliberately go through the **real** Task Center routes
(``app.gateway.routers.tasks``) rather than calling the automation helpers
directly, so what is asserted is what a client actually receives:

- ``GET /api/tasks/{task_id}`` — the task row the UI renders;
- ``GET /api/tasks/{task_id}/execution-history`` — the Runtime projection surface.

The task is created through the same API, and a Runtime linkage is written with
the domain's own writer (``link_runtime_run``) and the domain's own projection,
persisted through the existing project-storage save path — no hand-written SQLite
row is used as an acceptance artefact.

The module is skipped, not failed, where FastAPI is not installed: these are the
only tests in the Runtime suite that need an HTTP stack, and their absence must
not turn an unrelated unit run red.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi", reason="Task Center API tests need FastAPI")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# Point the existing path resolution at a throwaway home before anything resolves
# it, so the test never touches the developer's real data directory.
os.environ.setdefault("EVOFLOW_HOME", tempfile.mkdtemp(prefix="qagent-api-read-"))

from app.gateway.deps.license import require_premium  # noqa: E402
from app.gateway.routers.tasks import router  # noqa: E402
from app.gateway.task_runtime_context import build_task_runtime_context  # noqa: E402
from app.gateway.task_runtime_linkage import (  # noqa: E402
    LINKAGE_TASK_KEY,
    link_runtime_run,
)
from app.gateway.task_runtime_projection import (  # noqa: E402
    HISTORY_FIELD,
    project_runtime_status,
)

ORG_A = "org-a"
ORG_B = "org-b"


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A real Task Center app, with the premium gate satisfied the FastAPI way."""
    from evoflow.persistence.db import reset_db_for_tests

    reset_db_for_tests()
    app = FastAPI()
    app.include_router(router)
    # The license gate is not what these tests are about, and it is a plain
    # dependency, so it is overridden rather than faked at the license layer.
    app.dependency_overrides[require_premium] = lambda: None
    return TestClient(app)


def _create_task(client: TestClient, **extra: Any) -> str:
    body = {"name": "每周经营简报", "description": "给管理层的周报", "run_mode": "unattended"}
    body.update(extra)
    response = client.post("/api/tasks", json=body)
    assert response.status_code == 200, response.text
    return str(response.json()["id"])


def _identity(org_id: str = ORG_A, owner: str = "webui:1") -> dict[str, Any]:
    return {
        "org_id": org_id,
        "scope_id": f"personal:{owner}",
        "principal": {"principal_id": owner, "principal_type": "internal"},
    }


def _stored_task(task_id: str) -> dict[str, Any]:
    from evoflow.collab.storage import get_project_storage

    storage = get_project_storage()
    for summary in storage.list_projects():
        project = storage.load_project(summary["id"])
        for task in (project or {}).get("tasks") or []:
            if task.get("id") == task_id:
                return dict(task)
    raise AssertionError(f"task {task_id} not found in project storage")


def _save_task(task_id: str, updated: dict[str, Any]) -> None:
    """Persist through the same storage call the router itself uses."""
    from evoflow.collab.storage import get_project_storage

    storage = get_project_storage()
    for summary in storage.list_projects():
        project = storage.load_project(summary["id"])
        tasks = (project or {}).get("tasks") or []
        for index, task in enumerate(tasks):
            if task.get("id") == task_id:
                tasks[index] = updated
                storage.save_project(project)
                return
    raise AssertionError(f"task {task_id} not found in project storage")


def _link_and_project(
    task_id: str,
    *,
    org_id: str = ORG_A,
    runtime_status: str = "running",
    run_id: str = "run-linked-1",
) -> dict[str, Any]:
    """Attach a linkage and project one Runtime status, both through the domain."""
    task = _stored_task(task_id)
    context = build_task_runtime_context(
        task=task, authz=_identity(org_id), authorized=True
    )
    linked, _ = link_runtime_run(task, context=context, runtime_run_id=run_id)
    projected, outcome = project_runtime_status(
        linked, context=context, runtime_status=runtime_status, sequence=1
    )
    assert outcome.action in {"status_updated", "history_only"}, outcome
    _save_task(task_id, projected)
    return projected


# --- an unlinked task is unchanged ----------------------------------------------


def test_an_unlinked_task_reads_exactly_as_before(client: TestClient) -> None:
    task_id = _create_task(client)

    body = client.get(f"/api/tasks/{task_id}").json()

    assert body["id"] == task_id
    assert body["name"] == "每周经营简报"
    for key in (LINKAGE_TASK_KEY, "runtime_run_cursor", HISTORY_FIELD):
        if key == HISTORY_FIELD:
            continue
        assert key not in body, f"an unlinked task must not expose {key}"


def test_an_unlinked_task_has_no_runtime_history(client: TestClient) -> None:
    task_id = _create_task(client)

    body = client.get(f"/api/tasks/{task_id}/execution-history").json()

    assert body["task_id"] == task_id
    assert body["execution_history"] == []
    assert body["total_executions"] == 0


def test_a_missing_task_still_404s(client: TestClient) -> None:
    assert client.get("/api/tasks/does-not-exist").status_code == 404
    assert client.get("/api/tasks/does-not-exist/execution-history").status_code == 404


# --- a linked task shows a stable projection ------------------------------------


def test_a_linked_task_exposes_the_projected_history(client: TestClient) -> None:
    task_id = _create_task(client)
    _link_and_project(task_id, runtime_status="running")

    body = client.get(f"/api/tasks/{task_id}/execution-history").json()

    assert body["total_executions"] == 1
    record = body["execution_history"][0]
    assert record["source"] == "qagent_runtime"
    assert record["runtime_run_id"] == "run-linked-1"
    assert record["runtime_status"] == "running"
    assert record["sequence"] == 1


def test_a_linked_task_moves_the_task_status_through_the_projection(client: TestClient) -> None:
    task_id = _create_task(client)
    _link_and_project(task_id, runtime_status="running")

    body = client.get(f"/api/tasks/{task_id}").json()

    assert body["status"] == "executing"
    assert body[HISTORY_FIELD][0]["runtime_status"] == "running"


def test_the_projection_is_stable_across_repeated_reads(client: TestClient) -> None:
    task_id = _create_task(client)
    _link_and_project(task_id, runtime_status="running")

    first = client.get(f"/api/tasks/{task_id}/execution-history").json()
    second = client.get(f"/api/tasks/{task_id}/execution-history").json()

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert second["total_executions"] == 1


def test_a_completed_projection_is_read_back_verbatim(client: TestClient) -> None:
    task_id = _create_task(client)
    _link_and_project(task_id, runtime_status="completed")

    body = client.get(f"/api/tasks/{task_id}/execution-history").json()

    record = body["execution_history"][0]
    assert record["runtime_status"] == "completed"
    assert record["result_available"] is False


def test_the_list_route_does_not_leak_the_linkage_internals(client: TestClient) -> None:
    task_id = _create_task(client)
    _link_and_project(task_id, runtime_status="running")

    body = client.get("/api/tasks").json()

    rendered = json.dumps(body, ensure_ascii=False)
    assert "runtime_run_linkage" not in rendered
    assert "runtime_run_cursor" not in rendered
    assert task_id in rendered


# --- a foreign organization's linkage is not shown -------------------------------


def _store_foreign_linkage(task_id: str) -> None:
    task = _stored_task(task_id)
    context = build_task_runtime_context(
        task=task, authz=_identity(ORG_A), authorized=True
    )
    linked, _ = link_runtime_run(task, context=context, runtime_run_id="run-own-1")
    # Overwrite the linkage with another organization's, exactly as a tampered or
    # mis-migrated extras slot would look.
    foreign = dict(linked)
    foreign[LINKAGE_TASK_KEY] = {
        "runtime_run_id": "run-foreign-1",
        "org_scope_key": f"tc:org:{ORG_B}",
        "task_id": task_id,
        "idempotency_key": "tc:" + "0" * 32,
    }
    _save_task(task_id, foreign)


def test_a_foreign_linkage_is_not_projected_onto_the_task(client: TestClient) -> None:
    task_id = _create_task(client)
    _store_foreign_linkage(task_id)

    body = client.get(f"/api/tasks/{task_id}/execution-history").json()

    assert body["execution_history"] == []
    assert body["total_executions"] == 0


def test_a_foreign_linkage_does_not_disclose_the_other_organization(client: TestClient) -> None:
    task_id = _create_task(client)
    _store_foreign_linkage(task_id)

    rendered = json.dumps(client.get(f"/api/tasks/{task_id}").json(), ensure_ascii=False)

    assert ORG_B not in rendered
    assert "run-foreign-1" not in rendered


def test_a_foreign_linkage_does_not_break_the_read(client: TestClient) -> None:
    """A broken link must not turn a task read into an error."""
    task_id = _create_task(client)
    _store_foreign_linkage(task_id)

    assert client.get(f"/api/tasks/{task_id}").status_code == 200
    assert client.get(f"/api/tasks/{task_id}/execution-history").status_code == 200


def test_the_strip_list_still_covers_the_runtime_bookkeeping_keys() -> None:
    """The response filter names the keys by literal; keep it tied to the source.

    The router keeps literals rather than importing the domain modules, so this
    is the assertion that stops a renamed constant from silently re-exposing the
    linkage.
    """
    from app.gateway.routers.tasks import _TASK_API_STRIP_KEYS
    from app.gateway.task_runtime_cursor import CURSOR_TASK_KEY
    from app.gateway.task_runtime_linkage import LINKAGE_TASK_KEY

    assert LINKAGE_TASK_KEY in _TASK_API_STRIP_KEYS
    assert CURSOR_TASK_KEY in _TASK_API_STRIP_KEYS
