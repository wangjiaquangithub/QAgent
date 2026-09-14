"""Vertical integration: the pause entry, through its real route.

AG-G2-AUTO-047, the last adjacent card of this queue. ``POST /api/tasks/{id}/stop``
is the「停止」control and closes the manual lifecycle the other entry cards opened
(authorize → start → revoke → cancel → **stop**).

Pausing is, on purpose, **not** a Runtime cancellation. It does three things: sets
the task to ``paused``, withdraws the execution consent (the same revocation the
「修改计划」control performs), and stops the task being picked up by the queue. It
never asks the Runtime to cancel, never moves the linkage, and never creates a run —
the attempt's run keeps running until someone cancels it.

One thing this file does **not** cover, deliberately: ``POST /api/tasks/{id}/resume``.
Resuming an unattended task reaches ``advance_unattended_task``, and because the
pause withdrew the consent, that advance is no longer Runtime-eligible — it falls
through to the legacy planner. Isolating that would mean stubbing the LangGraph /
LLM boundary, which the other entry cards avoid; so resume is left uncovered rather
than covered by a test that proves nothing about the Runtime.

Nothing here hand-writes a final SQLite state; the Runtime is replaced at its public
boundary by a recording fake.
"""

from __future__ import annotations

import pytest
from _runtime_flow_support import (
    LINKAGE_TASK_KEY,
    RUN_ID,
    SWITCH,
    block_outbound_network,
    build_client,
    park_other_unattended_tasks,
    queue_tick_via_route,
    reset_home,
    schedule_a_run_via_route,
    stop_via_route,
    stored_task,
)


@pytest.fixture()
def client():
    reset_home()
    return build_client()


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch):
    """Runtime opt-in on, and no outbound network: the fake is the only runtime."""
    monkeypatch.setenv(SWITCH, "1")
    block_outbound_network(monkeypatch)


def _entry_for(tick_result: dict, task_id: str) -> dict | None:
    for entry in tick_result.get("results") or []:
        if str(entry.get("task_id") or "") == task_id:
            return dict(entry)
    return None


# --- what pausing does ------------------------------------------------------------


def test_stopping_pauses_the_task(client, monkeypatch: pytest.MonkeyPatch) -> None:
    task_id, _ = schedule_a_run_via_route(client, monkeypatch)

    body = stop_via_route(client, task_id)

    assert body.get("success") is True
    assert stored_task(task_id)["status"] == "paused"


def test_stopping_withdraws_the_execution_consent(client, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pause is a plan-level stop, so it clears the same consent a revoke does."""
    task_id, _ = schedule_a_run_via_route(client, monkeypatch)
    assert stored_task(task_id)["execution_authorized"] is True

    stop_via_route(client, task_id)

    assert stored_task(task_id)["execution_authorized"] is False


def test_stopping_twice_is_a_no_op(client, monkeypatch: pytest.MonkeyPatch) -> None:
    task_id, fake = schedule_a_run_via_route(client, monkeypatch)

    first = stop_via_route(client, task_id)
    second = stop_via_route(client, task_id)

    assert first.get("success") is True
    assert second.get("success") is True
    assert stored_task(task_id)["status"] == "paused"
    assert len(fake.create_calls) == 1


# --- what pausing is not ----------------------------------------------------------


def test_pausing_is_not_a_runtime_cancellation(client, monkeypatch: pytest.MonkeyPatch) -> None:
    """The run keeps running; only the Task Center stops working on the task."""
    task_id, fake = schedule_a_run_via_route(client, monkeypatch)

    stop_via_route(client, task_id)

    assert fake.cancel_calls == []


def test_pausing_leaves_the_linked_run_untouched(client, monkeypatch: pytest.MonkeyPatch) -> None:
    task_id, _ = schedule_a_run_via_route(client, monkeypatch)
    before = dict(stored_task(task_id)[LINKAGE_TASK_KEY])

    stop_via_route(client, task_id)

    assert stored_task(task_id)[LINKAGE_TASK_KEY] == before


def test_pausing_creates_no_run(client, monkeypatch: pytest.MonkeyPatch) -> None:
    task_id, fake = schedule_a_run_via_route(client, monkeypatch)

    stop_via_route(client, task_id)

    assert len(fake.create_calls) == 1


# --- a paused task is out of the queue's reach ------------------------------------


def test_a_paused_task_is_not_picked_up_by_the_tick(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, fake = schedule_a_run_via_route(client, monkeypatch)
    stop_via_route(client, task_id)

    park_other_unattended_tasks(task_id)
    result = queue_tick_via_route(client)

    assert _entry_for(result, task_id) is None, "a paused task must not be walked"
    assert stored_task(task_id)["status"] == "paused"
    assert len(fake.create_calls) == 1


def test_a_paused_task_still_reads_and_reveals_nothing(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, _ = schedule_a_run_via_route(client, monkeypatch)
    stop_via_route(client, task_id)

    response = client.get(f"/api/tasks/{task_id}")

    assert response.status_code == 200
    assert LINKAGE_TASK_KEY not in response.text
    assert RUN_ID not in response.text
