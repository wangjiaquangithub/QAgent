"""Vertical integration: the approval phase of a Runtime-backed unattended task.

AG-G2-AUTO-031.

Uses the same real entries as the success flow (real routes, the existing
authorization service, the unattended pipeline, the existing event adapter), and
drives the approval phase with Runtime frames rather than by writing an approval
result anywhere: the Task Center keeps no approval state of its own, so there is
nothing to fake and nothing to converge by hand.

Frozen semantics this asserts, as implemented: ``waiting_approval`` makes the
decision visible without moving the task; a decision frame is recorded once and
never moves the task either; the task converges from the run status the Runtime
reports next, and a rejected approval must never be presented as a success.
"""

from __future__ import annotations

from typing import Any

import pytest
from _runtime_flow_support import (
    HISTORY_FIELD,
    LINKAGE_TASK_KEY,
    FakeRuntime,
    authorize,
    block_outbound_network,
    build_client,
    create_unattended_task,
    frame,
    history_of,
    json_of,
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


def _started(client, fake: FakeRuntime, monkeypatch: pytest.MonkeyPatch) -> str:
    """Create, authorize and opt in — the same path a user's run-now takes."""
    run_contract(fake, monkeypatch)
    task_id = create_unattended_task(client)
    authorize(task_id)
    step = tick(task_id)
    assert step["action"] == "runtime", step
    return task_id


def _waiting(fake: FakeRuntime) -> list[dict[str, Any]]:
    """The frames a run emits on its way to the approval gate.

    The Runtime's own order: a run plans, then asks for approval, and only runs
    once it has it. (The projection's stage order encodes the same thing, so a
    ``waiting_approval`` after ``running`` would be refused as a regression.)
    """
    return [
        frame("run.planning", sequence=0),
        frame("run.waiting_approval", sequence=1),
    ]


# --- reaching the approval gate ---------------------------------------------------


def test_an_opted_in_task_reaches_the_approval_gate(client, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRuntime()
    task_id = _started(client, fake, monkeypatch)

    push_frames(task_id, _waiting(fake))
    stored = stored_task(task_id)

    record = history_of(stored)[-1]
    assert record["runtime_status"] == "waiting_approval"
    assert record["approval_required"] is True
    assert record["hint"]


def test_the_approval_gate_does_not_move_the_task(client, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRuntime()
    task_id = _started(client, fake, monkeypatch)

    before = push_frames(task_id, [frame("run.planning", sequence=0)])["status"]
    after = push_frames(task_id, [frame("run.waiting_approval", sequence=1)])["status"]

    assert after == before
    assert after != "pending"


def test_the_approval_gate_is_visible_through_the_real_route(client, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRuntime()
    task_id = _started(client, fake, monkeypatch)

    push_frames(task_id, _waiting(fake))

    body = client.get(f"/api/tasks/{task_id}/execution-history").json()
    assert [e["runtime_status"] for e in body["execution_history"]][-1] == "waiting_approval"


# --- granting --------------------------------------------------------------------


def test_a_granted_approval_is_recorded_and_then_converges(client, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRuntime()
    task_id = _started(client, fake, monkeypatch)

    task = push_frames(
        task_id,
        [
            *_waiting(fake),
            frame("approval.granted", sequence=2),
            frame("run.running", sequence=3),
            frame("run.completed", sequence=4, payload={"result": {"summary": "已生成"}}),
        ],
    )

    assert statuses_of(task) == [
        "planning",
        "waiting_approval",
        "approval_granted",
        "running",
        "completed",
    ]
    assert task["status"] == "completed"


def test_the_decision_frame_does_not_move_the_task_by_itself(client, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRuntime()
    task_id = _started(client, fake, monkeypatch)

    before = push_frames(task_id, _waiting(fake))["status"]
    after_decision = push_frames(task_id, [frame("approval.granted", sequence=2)])

    assert after_decision["status"] == before
    assert statuses_of(after_decision)[-1] == "approval_granted"


def test_a_repeated_decision_frame_is_recorded_once(client, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRuntime()
    task_id = _started(client, fake, monkeypatch)

    push_frames(task_id, [*_waiting(fake), frame("approval.granted", sequence=2)])
    task = push_frames(task_id, [frame("approval.granted", sequence=2)])

    assert statuses_of(task).count("approval_granted") == 1


# --- rejecting -------------------------------------------------------------------


def test_a_rejected_approval_is_never_presented_as_a_success(client, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRuntime(status="cancelled")
    task_id = _started(client, fake, monkeypatch)

    task = push_frames(
        task_id,
        [
            *_waiting(fake),
            frame("approval.rejected", sequence=2),
            frame("run.cancelled", sequence=3),
        ],
    )

    assert task["status"] == "cancelled"
    assert "completed" not in statuses_of(task)
    assert "approval_rejected" in statuses_of(task)


def test_a_rejection_leaves_no_result_behind(client, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRuntime(
        status="cancelled",
        frames=[frame("run.completed", sequence=9, payload={"result": {"summary": "不该出现"}})],
    )
    task_id = _started(client, fake, monkeypatch)

    task = push_frames(
        task_id,
        [
            *_waiting(fake),
            frame("approval.rejected", sequence=2),
            frame("run.cancelled", sequence=3),
        ],
    )

    assert "不该出现" not in json_of(task)
    for record in history_of(task):
        assert record.get("result_available") is not True


def test_a_rejection_cannot_be_replaced_by_a_late_completion(client, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRuntime(status="cancelled")
    task_id = _started(client, fake, monkeypatch)

    push_frames(
        task_id,
        [*_waiting(fake), frame("approval.rejected", sequence=2), frame("run.cancelled", sequence=3)],
    )
    late = push_frames(task_id, [frame("run.completed", sequence=4)])

    assert late["status"] == "cancelled"
    assert "completed" not in statuses_of(late)


# --- the record itself -----------------------------------------------------------


def test_the_approval_history_stays_a_projection_of_the_run(client, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRuntime()
    task_id = _started(client, fake, monkeypatch)

    task = push_frames(task_id, [*_waiting(fake), frame("approval.granted", sequence=2)])

    assert all(entry.get("source") == "qagent_runtime" for entry in history_of(task))
    # No Task-owned approval state is invented: the task row carries the history
    # and the linkage, nothing else.
    assert not any(
        key in task
        for key in ("approval", "approval_status", "approved_by", "approval_required")
    )
    assert LINKAGE_TASK_KEY in task
    assert HISTORY_FIELD in task
