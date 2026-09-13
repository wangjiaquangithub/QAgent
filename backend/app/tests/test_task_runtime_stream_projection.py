"""Runtime event-stream compatibility tests for the Task Center.

AG-G2-AUTO-029.

Runtime Event v1 frames reach the Task Center through the existing adapter, and
what a client sees is the task row's existing ``execution_history`` — the list the
existing endpoint ``GET /api/tasks/{task_id}/execution-history`` returns verbatim.
That adapter is therefore the closest direct boundary of the stream, and it is
what these tests drive: a sequence of frames is played in stream order and the
resulting history is asserted, including what a *reconnected* client reads back.

The global SSE infrastructure is not touched and not registered against — no
global event registry, cursor protocol or event schema is modified. Three stream
properties are covered that the frame-level tests do not:

- the named phases a user sees (started, waiting for approval, completed, failed,
  cancelled) each leave exactly one stable record;
- a duplicate frame, an out-of-order frame and a replayed frame after a
  cancellation cannot add a record or move the task;
- a reconnect re-reads the same history from persisted state, and the stream
  watermark tracks the last frame actually applied.
"""

from __future__ import annotations

import os
import tempfile
from typing import Any

import pytest

from app.gateway.task_runtime_context import build_task_runtime_context
from app.gateway.task_runtime_cursor import applied_watermark
from app.gateway.task_runtime_event_bridge import apply_runtime_event
from app.gateway.task_runtime_linkage import LINKAGE_TASK_KEY
from app.gateway.task_runtime_projection import HISTORY_FIELD
from app.gateway.task_runtime_recovery import read_task_stream_view, recover_task_stream

ORG_A = "org-a"
ORG_B = "org-b"
TASK_ID = "e4c1a2b0"
ORG_SCOPE_KEY = f"tc:org:{ORG_A}"
RUN_ID = "run-stream-1"


def _identity(org_id: str = ORG_A, owner: str = "webui:1") -> dict[str, Any]:
    return {
        "org_id": org_id,
        "scope_id": f"personal:{owner}",
        "principal": {"principal_id": owner, "principal_type": "internal"},
    }


def _linkage(*, org_scope_key: str = ORG_SCOPE_KEY, run_id: str = RUN_ID) -> dict[str, str]:
    return {
        "runtime_run_id": run_id,
        "org_scope_key": org_scope_key,
        "task_id": TASK_ID,
        "idempotency_key": "tc:" + "1" * 32,
    }


def _task(*, status: str = "inbox", **extra: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": TASK_ID,
        "name": "每周经营简报",
        "run_mode": "unattended",
        "status": status,
        "unattended_attempts": 0,
        "execution_history": [],
        LINKAGE_TASK_KEY: _linkage(),
    }
    task.update(extra)
    return task


def _frame(
    event_type: str,
    *,
    sequence: int,
    event_id: str | None = None,
    payload: dict[str, Any] | None = None,
    run_id: str = RUN_ID,
) -> dict[str, Any]:
    return {
        "type": event_type,
        "run_id": run_id,
        "sequence": sequence,
        "event_id": event_id or f"evt-{sequence}",
        "payload": payload or {},
    }


def _play(task: dict[str, Any], frames: list[dict[str, Any]], *, org_id: str = ORG_A) -> dict[str, Any]:
    """Apply frames in stream order, the way a subscribed client consumes them."""
    current = task
    for frame in frames:
        context = build_task_runtime_context(
            task=current, authz=_identity(org_id), authorized=True
        )
        current, _ = apply_runtime_event(current, context=context, event=frame)
    return current


def _statuses(task: dict[str, Any]) -> list[str]:
    return [str(entry.get("runtime_status")) for entry in task[HISTORY_FIELD]]


SUCCESS_STREAM = [
    _frame("run.created", sequence=0),
    _frame("run.queued", sequence=1),
    _frame("run.planning", sequence=2),
    _frame("run.waiting_approval", sequence=3),
    _frame("run.running", sequence=4),
    _frame("run.completed", sequence=5, payload={"result": {"summary": "周报已生成"}}),
]


# --- the phases a user sees ------------------------------------------------------


def test_a_success_stream_converges_the_task() -> None:
    task = _play(_task(), SUCCESS_STREAM)

    assert task["status"] == "completed"
    assert _statuses(task) == [
        "created",
        "queued",
        "planning",
        "waiting_approval",
        "running",
        "completed",
    ]


def test_the_waiting_approval_phase_does_not_move_the_task() -> None:
    task = _play(_task(), SUCCESS_STREAM[:4])

    record = task[HISTORY_FIELD][-1]
    assert record["runtime_status"] == "waiting_approval"
    assert record["approval_required"] is True
    assert task["status"] == "planning"


def test_the_completed_phase_states_whether_a_result_exists() -> None:
    task = _play(_task(), SUCCESS_STREAM)

    record = task[HISTORY_FIELD][-1]
    assert record["result_available"] is True
    assert record["result"] == {"summary": "周报已生成"}


def test_a_completed_stream_without_a_result_says_so() -> None:
    task = _play(_task(), [_frame("run.completed", sequence=0)])

    assert task[HISTORY_FIELD][-1]["result_available"] is False


def test_the_failed_phase_records_a_safe_failure() -> None:
    frames = [
        _frame("run.running", sequence=0),
        _frame(
            "run.failed",
            sequence=1,
            payload={"error": {"code": "provider_error", "message": "上游服务返回 502"}},
        ),
    ]

    task = _play(_task(), frames)

    record = task[HISTORY_FIELD][-1]
    assert task["status"] == "failed"
    assert record["runtime_status"] == "failed"
    assert record["error_code"] == "provider_error"
    assert record["reason"] == "上游服务返回 502"


def test_the_cancelled_phase_settles_the_task() -> None:
    task = _play(_task(), [_frame("run.cancelled", sequence=0)])

    assert task["status"] == "cancelled"
    assert _statuses(task) == ["cancelled"]


@pytest.mark.parametrize(
    ("event_type", "sequence", "expected_status"),
    [
        ("run.created", 0, "pending"),
        ("run.waiting_approval", 0, "inbox"),
        ("run.completed", 0, "completed"),
        ("run.failed", 0, "failed"),
        ("run.cancelled", 0, "cancelled"),
    ],
)
def test_each_named_phase_leaves_exactly_one_record(
    event_type: str, sequence: int, expected_status: str
) -> None:
    task = _play(_task(), [_frame(event_type, sequence=sequence)])

    assert len(task[HISTORY_FIELD]) == 1
    assert task["status"] == expected_status


# --- duplicate, out-of-order and replayed frames ---------------------------------


def test_a_duplicate_frame_in_the_stream_is_not_recorded_twice() -> None:
    task = _play(_task(), SUCCESS_STREAM)
    once = len(task[HISTORY_FIELD])

    replayed = _play(task, [SUCCESS_STREAM[-1]])

    assert len(replayed[HISTORY_FIELD]) == once
    assert _statuses(replayed) == _statuses(task)


def test_the_same_sequence_with_a_new_event_id_is_still_a_duplicate() -> None:
    task = _play(_task(), [_frame("run.running", sequence=3)])

    replayed = _play(task, [_frame("run.running", sequence=3, event_id="evt-3-b")])

    assert len(replayed[HISTORY_FIELD]) == 1


def test_an_out_of_order_frame_does_not_regress_the_task() -> None:
    task = _play(_task(), SUCCESS_STREAM)

    late = _play(task, [_frame("run.running", sequence=1, event_id="evt-late")])

    assert late["status"] == "completed"
    assert _statuses(late) == _statuses(task)


def test_a_replayed_frame_cannot_undo_a_cancellation() -> None:
    task = _play(_task(), [_frame("run.running", sequence=0)])
    task = _play(task, [_frame("run.cancelled", sequence=1)])

    replayed = _play(task, [_frame("run.completed", sequence=2)])

    assert replayed["status"] == "cancelled"
    assert "completed" not in _statuses(replayed)


def test_a_settled_task_is_not_moved_by_a_later_frame() -> None:
    task = _play(_task(), SUCCESS_STREAM)

    later = _play(task, [_frame("run.failed", sequence=6, payload={"error": "boom"})])

    assert later["status"] == "completed"
    assert len(later[HISTORY_FIELD]) == len(task[HISTORY_FIELD])


def test_a_frame_for_another_run_never_enters_the_stream() -> None:
    from app.gateway.task_runtime_event_bridge import RuntimeEventBridgeError

    task = _task()

    with pytest.raises(RuntimeEventBridgeError):
        _play(task, [_frame("run.running", sequence=0, run_id="run-someone-else")])


# --- reconnect -------------------------------------------------------------------


def test_a_reconnect_reads_back_the_same_stream() -> None:
    task = _play(_task(), SUCCESS_STREAM)
    context = build_task_runtime_context(task=task, authz=_identity(), authorized=True)

    view = read_task_stream_view(task, context=context)

    assert view.status == "completed"
    assert view.runtime_run_id == RUN_ID
    assert [entry["runtime_status"] for entry in view.history] == _statuses(task)


def test_a_reconnect_does_not_duplicate_the_history() -> None:
    task = _play(_task(), SUCCESS_STREAM)
    once = len(task[HISTORY_FIELD])

    replayed = _play(task, SUCCESS_STREAM)

    assert len(replayed[HISTORY_FIELD]) == once


def test_the_stream_watermark_tracks_the_last_applied_frame() -> None:
    task = _play(_task(), SUCCESS_STREAM)
    context = build_task_runtime_context(task=task, authz=_identity(), authorized=True)

    watermark = applied_watermark(
        task, context=context, run_id=RUN_ID, history=task[HISTORY_FIELD]
    )

    assert watermark == 5


def test_a_reconnect_without_a_runtime_call_reports_read_from_stored_state() -> None:
    import asyncio

    task = _play(_task(), SUCCESS_STREAM)
    context = build_task_runtime_context(task=task, authz=_identity(), authorized=True)

    outcome = asyncio.run(
        recover_task_stream(task, context=context, contract=None, catch_up=False)
    )

    assert outcome.action == "read"
    assert outcome.reason == "read_from_stored_state"
    assert outcome.view.status == "completed"


# --- the same stream as a client receives it over the real route ------------------


def test_the_history_route_serves_the_stream_records() -> None:
    """The existing endpoint returns the stream records verbatim."""
    pytest.importorskip("fastapi", reason="Task Center API tests need FastAPI")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.gateway.deps.license import require_premium
    from app.gateway.routers.tasks import router

    os.environ.setdefault("EVOFLOW_HOME", tempfile.mkdtemp(prefix="qagent-stream-"))
    from evoflow.collab.storage import get_project_storage
    from evoflow.persistence.db import reset_db_for_tests

    reset_db_for_tests()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_premium] = lambda: None
    client = TestClient(app)

    created = client.post(
        "/api/tasks",
        json={"name": "流测试", "description": "d", "run_mode": "unattended"},
    )
    assert created.status_code == 200, created.text
    task_id = str(created.json()["id"])

    storage = get_project_storage()
    for summary in storage.list_projects():
        project = storage.load_project(summary["id"])
        tasks = (project or {}).get("tasks") or []
        for index, row in enumerate(tasks):
            if row.get("id") != task_id:
                continue
            stored = dict(row)
            stored[LINKAGE_TASK_KEY] = {
                "runtime_run_id": RUN_ID,
                "org_scope_key": ORG_SCOPE_KEY,
                "task_id": task_id,
                "idempotency_key": "tc:" + "1" * 32,
            }
            stored["execution_history"] = []
            tasks[index] = _play(stored, SUCCESS_STREAM)
            storage.save_project(project)
            break

    body = client.get(f"/api/tasks/{task_id}/execution-history").json()

    assert body["total_executions"] == 6
    assert [entry["runtime_status"] for entry in body["execution_history"]] == _statuses(
        _play(_task(), SUCCESS_STREAM)
    )
    assert client.get(f"/api/tasks/{task_id}").json()["status"] == "completed"
    # The stream must not have published the linkage itself.
    assert "runtime_run_linkage" not in client.get(f"/api/tasks/{task_id}").text


def test_a_foreign_linkage_has_no_stream_to_read() -> None:
    """A linkage owned by another organization cannot be driven or read."""
    from app.gateway.task_runtime_event_bridge import RuntimeEventBridgeError

    task = _task(**{LINKAGE_TASK_KEY: _linkage(org_scope_key=f"tc:org:{ORG_B}")})

    with pytest.raises(RuntimeEventBridgeError):
        _play(task, [_frame("run.running", sequence=0)])
