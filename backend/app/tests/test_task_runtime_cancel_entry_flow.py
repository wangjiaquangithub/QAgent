"""Vertical integration: the real cancel control, and the manual triggers after it.

AG-G2-AUTO-041.

The cancellation flow itself (a running task's cancel reaching the Runtime, and a
stale completion or failure not resurrecting it) is covered by AG-G2-AUTO-033.
What this file adds is the part a user can actually reach from the Task Center and
the states that were not exercised:

* cancelling a task that is **waiting for approval**, which is the state a user is
  most likely to cancel from;
* cancelling a task that has already settled — **completed**, **failed**, already
  **cancelled** — where the local control is unconditional but the Runtime's run is
  over, so what the Task Center keeps must stay explainable;
* the **manual triggers after a cancel**: the run-now control and the dispatch
  control must not restart the same attempt, and must not create a run;
* a frame kind that AG-G2-AUTO-033 did not send after a cancel (an approval
  decision) must not resurrect the task either;
* a **foreign linkage** must leave the cancel fail-closed: the other organization's
  run is never asked to cancel and never named in the response.

Everything goes through the real routes, and the Runtime is replaced at its public
boundary by a recording fake.
"""

from __future__ import annotations

import pytest
from _runtime_flow_support import (
    LINKAGE_TASK_KEY,
    RUN_ID,
    FakeRuntime,
    authorize_via_route,
    block_outbound_network,
    build_client,
    create_unattended_task,
    frame,
    push_frames,
    reset_home,
    run_contract,
    save_task,
    start_via_route,
    statuses_of,
    stored_task,
)

SWITCH = "EVOFLOW_AUTOMATION_UNATTENDED_RUNTIME"

APPROVAL_FRAMES = [
    frame("run.planning", sequence=0),
    frame("run.waiting_approval", sequence=1, payload={"approval_required": True}),
]
SUCCESS_FRAMES = [
    frame("run.running", sequence=0),
    frame("run.completed", sequence=1, payload={"result": {"summary": "完成"}}),
]
FAILURE_FRAMES = [
    frame("run.running", sequence=0),
    frame("run.failed", sequence=1, payload={"error": {"code": "provider_error", "message": "上游 502"}}),
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


def _linked(client, monkeypatch: pytest.MonkeyPatch, frames, *, status: str = "queued"):
    fake = run_contract(FakeRuntime(frames=frames, status=status), monkeypatch)
    task_id = create_unattended_task(client)
    authorize_via_route(client, task_id)
    assert start_via_route(client, task_id)["advance"]["action"] == "runtime"
    push_frames(task_id, fake.frames)
    return task_id, fake


def _cancel(client, task_id: str):
    response = client.post(f"/api/tasks/{task_id}/cancel")
    assert response.status_code == 200, response.text
    return response


# --- the state a user is most likely to cancel from -------------------------------


def test_cancelling_a_waiting_approval_task_reaches_the_runtime(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, fake = _linked(client, monkeypatch, APPROVAL_FRAMES)

    _cancel(client, task_id)

    assert fake.cancel_calls == [RUN_ID]
    assert stored_task(task_id)["status"] == "cancelled"


def test_the_waiting_approval_cancel_is_visible_in_the_history(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, _ = _linked(client, monkeypatch, APPROVAL_FRAMES)

    _cancel(client, task_id)

    statuses = statuses_of(stored_task(task_id))
    assert "waiting_approval" in statuses
    assert statuses[-1] == "cancelled"


def test_cancelling_a_waiting_approval_task_twice_is_idempotent(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, fake = _linked(client, monkeypatch, APPROVAL_FRAMES)

    _cancel(client, task_id)
    _cancel(client, task_id)

    assert len(fake.cancel_calls) == 1
    assert statuses_of(stored_task(task_id)).count("cancelled") == 1


# --- cancelling something that already settled ------------------------------------


def test_cancelling_a_completed_task_keeps_a_single_explainable_status(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The local control is unconditional; the history must not claim two outcomes."""
    task_id, fake = _linked(client, monkeypatch, SUCCESS_FRAMES, status="completed")
    assert stored_task(task_id)["status"] == "completed"

    _cancel(client, task_id)

    stored = stored_task(task_id)
    assert stored["status"] == "cancelled"
    statuses = statuses_of(stored)
    # The run really did complete; the cancel must not append a second, false outcome.
    assert statuses.count("cancelled") <= 1
    assert "completed" in statuses
    # The Runtime was told, and it is the Runtime that owns the run's final state.
    assert fake.cancel_calls == [RUN_ID]


def test_cancelling_a_failed_task_keeps_the_failure_readable(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, fake = _linked(client, monkeypatch, FAILURE_FRAMES, status="failed")
    assert stored_task(task_id)["status"] == "failed"

    _cancel(client, task_id)

    stored = stored_task(task_id)
    assert stored["status"] == "cancelled"
    statuses = statuses_of(stored)
    assert "failed" in statuses
    assert statuses.count("cancelled") <= 1
    assert fake.cancel_calls == [RUN_ID]


def test_cancelling_an_already_cancelled_task_spends_one_cancel(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, fake = _linked(client, monkeypatch, SUCCESS_FRAMES, status="completed")

    _cancel(client, task_id)
    _cancel(client, task_id)
    _cancel(client, task_id)

    assert len(fake.cancel_calls) == 1
    assert statuses_of(stored_task(task_id)).count("cancelled") == 1


# --- the manual triggers after a cancel -------------------------------------------


def test_dispatch_after_a_cancel_is_refused_and_creates_no_run(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelling revokes the authorization, so dispatch has nothing to dispatch."""
    task_id, fake = _linked(client, monkeypatch, APPROVAL_FRAMES)
    _cancel(client, task_id)

    response = client.post(f"/api/tasks/{task_id}/dispatch-execution")

    assert response.status_code >= 400, response.text
    assert len(fake.create_calls) == 1


def test_run_now_after_a_cancel_creates_no_run(client, monkeypatch: pytest.MonkeyPatch) -> None:
    task_id, fake = _linked(client, monkeypatch, APPROVAL_FRAMES)
    _cancel(client, task_id)

    start_via_route(client, task_id)

    assert len(fake.create_calls) == 1
    assert LINKAGE_TASK_KEY in stored_task(task_id)


def test_run_now_after_a_cancel_does_not_move_the_link(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, _ = _linked(client, monkeypatch, APPROVAL_FRAMES)
    _cancel(client, task_id)
    before = dict(stored_task(task_id)[LINKAGE_TASK_KEY])

    start_via_route(client, task_id)

    assert stored_task(task_id)[LINKAGE_TASK_KEY] == before


def test_the_cancelled_task_is_still_readable_afterwards(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, _ = _linked(client, monkeypatch, APPROVAL_FRAMES)
    _cancel(client, task_id)

    body = client.get(f"/api/tasks/{task_id}").json()

    assert body["status"] == "cancelled"
    assert "runtime_run_linkage" not in body


# --- a frame kind 033 did not send after a cancel ----------------------------------


def test_a_late_approval_decision_cannot_resurrect_a_cancelled_task(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, _ = _linked(client, monkeypatch, APPROVAL_FRAMES)
    _cancel(client, task_id)
    before = statuses_of(stored_task(task_id))

    push_frames(task_id, [frame("approval.granted", sequence=2, event_id="evt-late-approval")])

    stored = stored_task(task_id)
    assert stored["status"] == "cancelled"
    assert statuses_of(stored) == before


# --- fail-closed on a foreign linkage ---------------------------------------------


def test_a_foreign_linkage_never_asks_the_other_run_to_cancel(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, fake = _linked(client, monkeypatch, APPROVAL_FRAMES)
    stored = stored_task(task_id)
    stored[LINKAGE_TASK_KEY]["org_scope_key"] = "tc:org:someone-else"
    save_task(task_id, stored)

    _cancel(client, task_id)

    # The local cancellation still happens — that is what the user asked for — but
    # the other organization's run is never touched.
    assert fake.cancel_calls == []


def test_a_foreign_linkage_does_not_disclose_the_other_run(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, _ = _linked(client, monkeypatch, APPROVAL_FRAMES)
    stored = stored_task(task_id)
    stored[LINKAGE_TASK_KEY]["org_scope_key"] = "tc:org:someone-else"
    save_task(task_id, stored)

    response = _cancel(client, task_id)
    body = client.get(f"/api/tasks/{task_id}")

    # Only this task's own organization may appear. The other organization's scope
    # key must not, in the cancel response or in the task.
    assert "someone-else" not in response.text
    assert "someone-else" not in body.text
    assert "tc:org:someone-else" not in response.text
    assert "tc:org:someone-else" not in body.text
