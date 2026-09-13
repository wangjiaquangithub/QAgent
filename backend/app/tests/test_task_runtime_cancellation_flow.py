"""Vertical integration: cancelling a Runtime-backed task, and stale write-backs.

AG-G2-AUTO-033.

Cancellation goes through the real route (``POST /api/tasks/{task_id}/cancel``),
which is the control a user has, and the Runtime is cancelled through the existing
linked-cancel path with a controllable fake standing in for it. The Runtime is
never modified to suit the test.

The interesting half is what arrives afterwards. A cancelled run's frames can
still be in flight — a completion the Runtime emitted just before the cancel, a
failure from a worker that has not noticed yet — and none of them may turn a
cancelled task back into anything else. A cancellation is also a thing users do
twice, so it has to be idempotent.
"""

from __future__ import annotations

import pytest
from _runtime_flow_support import (
    HISTORY_FIELD,
    LINKAGE_TASK_KEY,
    RUN_ID,
    FakeRuntime,
    authorize,
    block_outbound_network,
    build_client,
    create_unattended_task,
    frame,
    history_of,
    push_frames,
    reset_home,
    run_contract,
    statuses_of,
    stored_task,
    tick,
)

SWITCH = "EVOFLOW_AUTOMATION_UNATTENDED_RUNTIME"


@pytest.fixture()
def client():
    reset_home()
    return build_client()


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch):
    """Runtime opt-in on, and no outbound network: the fake is the only runtime."""
    monkeypatch.setenv(SWITCH, "1")
    block_outbound_network(monkeypatch)


def _running_task(
    client, monkeypatch: pytest.MonkeyPatch, *, frames: list[dict] | None = None
) -> tuple[str, FakeRuntime]:
    """A linked, executing task — the state a cancel is issued from."""
    fake = run_contract(FakeRuntime(frames=frames or [frame("run.running", sequence=0)]), monkeypatch)
    task_id = create_unattended_task(client)
    authorize(task_id)
    assert tick(task_id)["action"] == "runtime"
    push_frames(task_id, fake.frames)
    assert stored_task(task_id)["status"] == "executing"
    return task_id, fake


def _cancel(client, task_id: str):
    response = client.post(f"/api/tasks/{task_id}/cancel")
    assert response.status_code == 200, response.text
    return response


# --- cancelling reaches the runtime ----------------------------------------------


def test_cancelling_a_linked_task_asks_the_runtime_to_cancel(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, fake = _running_task(client, monkeypatch)

    _cancel(client, task_id)

    assert fake.cancel_calls == [RUN_ID]
    assert stored_task(task_id)["status"] == "cancelled"


def test_the_cancellation_is_visible_in_the_history(client, monkeypatch: pytest.MonkeyPatch) -> None:
    task_id, _ = _running_task(client, monkeypatch)

    _cancel(client, task_id)

    assert "cancelled" in statuses_of(stored_task(task_id))


def test_cancelling_twice_is_idempotent(client, monkeypatch: pytest.MonkeyPatch) -> None:
    task_id, _ = _running_task(client, monkeypatch)

    _cancel(client, task_id)
    _cancel(client, task_id)

    stored = stored_task(task_id)
    assert stored["status"] == "cancelled"
    assert statuses_of(stored).count("cancelled") == 1
    assert statuses_of(stored) == ["running", "cancelled"]


def test_the_api_reports_the_cancelled_task(client, monkeypatch: pytest.MonkeyPatch) -> None:
    task_id, _ = _running_task(client, monkeypatch)

    _cancel(client, task_id)

    assert client.get(f"/api/tasks/{task_id}").json()["status"] == "cancelled"
    history = client.get(f"/api/tasks/{task_id}/execution-history").json()
    assert [e["runtime_status"] for e in history["execution_history"]][-1] == "cancelled"


# --- stale write-backs -----------------------------------------------------------


def test_a_stale_completion_cannot_resurrect_a_cancelled_task(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = run_contract(FakeRuntime(frames=[frame("run.running", sequence=0)]), monkeypatch)
    task_id = create_unattended_task(client)
    authorize(task_id)
    tick(task_id)
    push_frames(task_id, fake.frames)
    _cancel(client, task_id)

    # The completion the Runtime emitted just before the cancel finally arrives.
    stored = push_frames(task_id, [frame("run.completed", sequence=1)])

    assert stored["status"] == "cancelled"
    assert "completed" not in statuses_of(stored)
    assert history_of(stored)[-1]["runtime_status"] == "cancelled"


def test_a_stale_failure_cannot_resurrect_a_cancelled_task(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = run_contract(FakeRuntime(frames=[frame("run.running", sequence=0)]), monkeypatch)
    task_id = create_unattended_task(client)
    authorize(task_id)
    tick(task_id)
    push_frames(task_id, fake.frames)
    _cancel(client, task_id)

    stored = push_frames(
        task_id,
        [frame("run.failed", sequence=1, payload={"error": {"code": "e", "message": "m"}})],
    )

    assert stored["status"] == "cancelled"
    assert "failed" not in statuses_of(stored)


def test_a_stale_running_frame_cannot_resurrect_a_cancelled_task(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, _ = _running_task(client, monkeypatch)
    _cancel(client, task_id)

    stored = push_frames(task_id, [frame("run.running", sequence=1, event_id="evt-late")])

    assert stored["status"] == "cancelled"


def test_a_pre_cancel_frame_replayed_after_the_cancel_changes_nothing(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, _ = _running_task(client, monkeypatch)
    _cancel(client, task_id)
    before = len(history_of(stored_task(task_id)))

    stored = push_frames(task_id, [frame("run.planning", sequence=0)])

    assert stored["status"] == "cancelled"
    assert len(history_of(stored)) == before


def test_a_cancelled_task_is_not_picked_up_again(client, monkeypatch: pytest.MonkeyPatch) -> None:
    """The existing retry/terminal semantics decide, not the Runtime branch."""
    task_id, fake = _running_task(client, monkeypatch)
    _cancel(client, task_id)
    creates_before = len(fake.create_calls)

    step = tick(task_id)

    assert step.get("action") != "runtime", step
    assert len(fake.create_calls) == creates_before


# --- an unlinked task is untouched -----------------------------------------------


def test_cancelling_an_unlinked_task_is_unchanged(client, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = run_contract(FakeRuntime(), monkeypatch)
    task_id = create_unattended_task(client)
    authorize(task_id)

    _cancel(client, task_id)

    stored = stored_task(task_id)
    assert stored["status"] == "cancelled"
    assert fake.cancel_calls == []
    assert fake.create_calls == []
    assert LINKAGE_TASK_KEY not in stored


def test_a_cancel_does_not_create_a_run(client, monkeypatch: pytest.MonkeyPatch) -> None:
    task_id, fake = _running_task(client, monkeypatch)

    _cancel(client, task_id)

    # One run was created to drive the task; cancelling spends no creation.
    assert len(fake.create_calls) == 1
    assert HISTORY_FIELD in stored_task(task_id)
