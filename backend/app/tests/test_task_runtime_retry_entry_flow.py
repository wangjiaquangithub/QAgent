"""Vertical integration: retry, through the real Task Center entries.

AG-G2-AUTO-040.

Two different things are called "retry" in this domain, and they must not be
confused:

* the **unattended attempt retry** — a failed unattended task whose next pickup
  requeues it as a *new attempt*. The real entry is the queue tick
  (``POST /api/tasks/queue/tick``): ``decide_runtime_pickup`` reports a settled task
  as ``task_settled`` and deliberately does **not** skip it, precisely so the
  pipeline's own retry branch still runs. A new attempt means a new Runtime run,
  and the link is superseded rather than rewritten in place.
* the **subtask retry** at ``POST /api/tasks/{task_id}/retry`` — a plan-level tool
  that resets failed subtasks. It is not an unattended attempt, so it must leave
  the Runtime link exactly as it found it.

Two behaviours of the existing scheduler are relied on here and are worth stating,
because they decide which entry can do what:

* the queue only *candidates* ``pending`` tasks (and failed-retry-due ones), so an
  authorized ``planned`` task is not picked up by a tick. That is why the new
  attempt's run is started by the run-now control, not by a second tick.

The run-now control (``POST /api/tasks/{task_id}/start``) is covered too, and its
behaviour is pinned rather than assumed. It moves a failed task out of its terminal
status *before* advancing it, so the pipeline's retry branch is no longer reachable
from that entry and the Runtime answers with the run this attempt already has
(``reused_terminal_run``) instead of starting a new attempt. That answer is safe —
no second run, no swallowed legacy execution — but it is why the queue, not the
run-now control, is the retry entry for an unattended attempt.

Nothing here hand-writes a final SQLite state; the Runtime is replaced at its
public boundary by a recording fake.
"""

from __future__ import annotations

import pytest
from _runtime_flow_support import (
    HISTORY_FIELD,
    LINKAGE_TASK_KEY,
    RUN_ID,
    FakeRuntime,
    authorize_via_route,
    block_outbound_network,
    build_client,
    create_unattended_task,
    frame,
    open_the_retry_window,
    park_other_unattended_tasks,
    push_frames,
    queue_tick_via_route,
    reset_home,
    run_contract,
    save_task,
    start_via_route,
    statuses_of,
    stored_task,
)

SWITCH = "EVOFLOW_AUTOMATION_UNATTENDED_RUNTIME"

FAILURE_FRAMES = [
    frame("run.running", sequence=0),
    frame(
        "run.failed",
        sequence=1,
        payload={"error": {"code": "provider_error", "message": "上游 502"}},
    ),
]


@pytest.fixture()
def client():
    reset_home()
    return build_client()


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch):
    """Runtime opt-in on, and no outbound network: the fake is the only runtime."""
    monkeypatch.setenv(SWITCH, "1")
    block_outbound_network(monkeypatch)


def _failed_run(client, monkeypatch: pytest.MonkeyPatch) -> tuple[str, FakeRuntime]:
    """Take a task all the way to a failed run, through the real controls."""
    fake = run_contract(FakeRuntime(frames=FAILURE_FRAMES, status="failed"), monkeypatch)
    task_id = create_unattended_task(client)
    authorize_via_route(client, task_id)
    assert start_via_route(client, task_id)["advance"]["action"] == "runtime"
    push_frames(task_id, fake.frames)
    assert stored_task(task_id)["status"] == "failed"
    return task_id, fake


def _tick(client, task_id: str, *, retry_window: bool = False) -> dict:
    """One real tick, with the two preconditions the queue itself relies on."""
    if retry_window:
        open_the_retry_window(task_id)
    park_other_unattended_tasks(task_id)
    return queue_tick_via_route(client)


def _retried_attempt(client, monkeypatch: pytest.MonkeyPatch) -> tuple[str, FakeRuntime]:
    """Drive the real attempt retry.

    Failing → the queue tick requeues the task as a new attempt → the user
    re-authorizes and presses run-now, which is what starts the new attempt's run.
    (The queue's candidate list covers ``pending`` tasks, not an authorized
    ``planned`` one, so run-now is the entry that starts the new attempt.)
    """
    task_id, fake = _failed_run(client, monkeypatch)
    _tick(client, task_id, retry_window=True)  # the tick hands the settled task over
    assert stored_task(task_id)["unattended_attempts"] == 1
    authorize_via_route(client, task_id)
    step = start_via_route(client, task_id)["advance"]
    assert step["action"] == "runtime", step
    assert stored_task(task_id)[LINKAGE_TASK_KEY]["runtime_run_id"] == "run-flow-2"
    return task_id, fake


# --- the queue tick is the real attempt-retry entry -------------------------------


def test_the_queue_tick_requeues_a_failed_task_as_a_new_attempt(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, _ = _failed_run(client, monkeypatch)

    _tick(client, task_id, retry_window=True)

    stored = stored_task(task_id)
    assert stored["unattended_attempts"] == 1
    assert stored["status"] == "pending"


def test_the_requeue_clears_the_authorization_before_anything_runs(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new attempt needs the user's consent again — the requeue withdraws it."""
    task_id, _ = _failed_run(client, monkeypatch)
    assert stored_task(task_id)["execution_authorized"] is True

    _tick(client, task_id, retry_window=True)

    assert stored_task(task_id)["execution_authorized"] is False


def test_the_requeue_creates_no_second_run(client, monkeypatch: pytest.MonkeyPatch) -> None:
    task_id, fake = _failed_run(client, monkeypatch)

    _tick(client, task_id, retry_window=True)

    assert len(fake.create_calls) == 1
    assert stored_task(task_id)[LINKAGE_TASK_KEY]["runtime_run_id"] == RUN_ID


def test_the_failed_attempts_run_id_survives_the_requeue(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The link still points at the old run until a new run actually exists."""
    task_id, _ = _failed_run(client, monkeypatch)

    _tick(client, task_id, retry_window=True)

    assert stored_task(task_id)[LINKAGE_TASK_KEY]["runtime_run_id"] == RUN_ID


# --- the new attempt gets its own run ---------------------------------------------


def test_the_requeued_attempt_gets_a_new_run_after_reauthorizing(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, fake = _retried_attempt(client, monkeypatch)

    assert len(fake.create_calls) == 2
    stored = stored_task(task_id)
    assert stored[LINKAGE_TASK_KEY]["runtime_run_id"] == "run-flow-2"
    assert stored[LINKAGE_TASK_KEY]["idempotency_key"] == fake.create_calls[1]["idempotency_key"]


def test_the_superseded_run_id_survives_in_the_history(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Superseding writes a new link; it does not rewrite what already happened."""
    task_id, _ = _retried_attempt(client, monkeypatch)

    stored = stored_task(task_id)

    assert statuses_of(stored) == ["running", "failed"]
    assert [e["runtime_run_id"] for e in stored[HISTORY_FIELD]] == [RUN_ID, RUN_ID]


def test_a_second_trigger_for_the_new_attempt_creates_no_second_run(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, fake = _retried_attempt(client, monkeypatch)

    start_via_route(client, task_id)

    assert len(fake.create_calls) == 2
    assert stored_task(task_id)[LINKAGE_TASK_KEY]["runtime_run_id"] == "run-flow-2"


# --- the old run cannot write over the new attempt --------------------------------


def test_a_late_completion_from_the_old_run_is_refused(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, _ = _retried_attempt(client, monkeypatch)
    before = stored_task(task_id)

    from app.gateway.task_runtime_event_bridge import RuntimeEventBridgeError

    with pytest.raises(RuntimeEventBridgeError):
        push_frames(
            task_id,
            [frame("run.completed", sequence=2, payload={"result": {"summary": "旧结果"}})],
        )

    assert stored_task(task_id) == before
    assert "旧结果" not in str(stored_task(task_id))


def test_a_late_failure_from_the_old_run_is_refused(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, _ = _retried_attempt(client, monkeypatch)
    before = stored_task(task_id)

    from app.gateway.task_runtime_event_bridge import RuntimeEventBridgeError

    with pytest.raises(RuntimeEventBridgeError):
        push_frames(
            task_id,
            [
                frame(
                    "run.failed",
                    sequence=2,
                    payload={"error": {"code": "provider_error", "message": "旧回写"}},
                )
            ],
        )

    assert stored_task(task_id) == before
    assert "旧回写" not in str(stored_task(task_id))


def test_the_new_attempt_projects_under_its_own_run(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, _ = _retried_attempt(client, monkeypatch)

    push_frames(
        task_id,
        [
            frame("run.running", sequence=0, run_id="run-flow-2"),
            frame(
                "run.completed",
                sequence=1,
                run_id="run-flow-2",
                payload={"result": {"summary": "第二轮完成"}},
            ),
        ],
    )

    stored = stored_task(task_id)

    assert stored["status"] == "completed"
    assert stored[HISTORY_FIELD][-1]["runtime_run_id"] == "run-flow-2"
    assert stored[HISTORY_FIELD][-1]["result"] == {"summary": "第二轮完成"}


# --- the run-now control on a failed task: pinned, not assumed --------------------


def test_the_run_now_control_on_a_failed_task_reuses_the_linked_run(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run-now control cannot reach the requeue: it clears `failed` first."""
    task_id, _ = _failed_run(client, monkeypatch)

    step = start_via_route(client, task_id)["advance"]

    assert step["action"] == "runtime", step
    assert step["runtime_action"] == "reused_terminal_run"
    assert step["runtime_run_id"] == RUN_ID


def test_the_run_now_control_creates_no_second_run(client, monkeypatch: pytest.MonkeyPatch) -> None:
    task_id, fake = _failed_run(client, monkeypatch)

    start_via_route(client, task_id)
    start_via_route(client, task_id)

    assert len(fake.create_calls) == 1


def test_the_run_now_control_does_not_overwrite_the_failed_runs_id(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, _ = _failed_run(client, monkeypatch)

    start_via_route(client, task_id)

    assert stored_task(task_id)[LINKAGE_TASK_KEY]["runtime_run_id"] == RUN_ID


def test_the_run_now_control_does_not_rerun_the_old_attempt_legacy_style(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Being answered with the linked run is why no legacy execution is started."""
    task_id, fake = _failed_run(client, monkeypatch)

    start_via_route(client, task_id)

    stored = stored_task(task_id)
    # No legacy planning happened: no plan, no thread, and only the frames' history.
    assert stored.get("plan_bound_at") in (None, "")
    assert stored.get("thread_id") in (None, "")
    assert "provider_error" in str(stored[HISTORY_FIELD])


# --- the subtask retry route is not an attempt retry ------------------------------


def test_the_subtask_retry_route_leaves_the_runtime_link_untouched(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, _ = _failed_run(client, monkeypatch)
    before = dict(stored_task(task_id)[LINKAGE_TASK_KEY])

    response = client.post(f"/api/tasks/{task_id}/retry", json={"retry_all_failed": True})

    assert response.status_code >= 400  # no failed subtasks: safely rejected
    assert stored_task(task_id)[LINKAGE_TASK_KEY] == before


def test_the_subtask_retry_route_creates_no_run(client, monkeypatch: pytest.MonkeyPatch) -> None:
    task_id, fake = _failed_run(client, monkeypatch)

    client.post(f"/api/tasks/{task_id}/retry", json={"retry_all_failed": True})

    assert len(fake.create_calls) == 1
    assert stored_task(task_id)[LINKAGE_TASK_KEY]["runtime_run_id"] == RUN_ID


def test_the_subtask_retry_route_does_not_reauthorize_the_task(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resetting subtasks is not the user's consent to run again."""
    task_id, _ = _failed_run(client, monkeypatch)
    stored = stored_task(task_id)
    stored["execution_authorized"] = False
    save_task(task_id, stored)

    client.post(f"/api/tasks/{task_id}/retry", json={"retry_all_failed": True})

    assert stored_task(task_id)["execution_authorized"] is False


def test_a_subtask_retry_does_not_resurrect_a_failed_run(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old run stays failed; the retry only affects the plan's subtasks."""
    task_id, fake = _failed_run(client, monkeypatch)

    client.post(f"/api/tasks/{task_id}/retry", json={"retry_all_failed": True})

    assert stored_task(task_id)["status"] == "failed"
    assert len(fake.create_calls) == 1
