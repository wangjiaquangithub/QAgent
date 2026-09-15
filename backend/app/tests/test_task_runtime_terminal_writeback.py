"""Vertical integration: a linked Runtime run settles the Task Center task.

AG-G2-AUTO-003-A01.

The gap this pins is narrow and specific. Establishing a run already works: the
unattended pipeline creates (or reuses) exactly one run for a trigger and writes
the linkage onto the task row. What did not work is everything after that. The
queue picks work by task *status*, and while a run is linked the Runtime owns that
status — so a task whose run had already finished could sit in neither the
candidate nor the in-progress list, and nothing ever asked the Runtime what
happened to it. The run's terminal state therefore never reached
``GET /api/tasks/{id}`` or ``GET /api/tasks/{id}/execution-history``.

These tests drive the **real** entries in the order a real deployment causes them:

1. the task is created through ``POST /api/tasks`` with ``run_mode=unattended``;
2. execution consent is given through the existing authorization route;
3. the run is established through ``POST /api/tasks/{id}/start`` — the existing
   convergence point, unchanged by this card;
4. the Runtime's own answer is read back through the **real** queue tick
   (``POST /api/tasks/queue/tick`` → ``task_queue_tick``), which is the entry that
   now projects it;
5. the result is read back through the real Task Center reads.

Nothing is asserted against a hand-written row: the Runtime is replaced at its
public boundary by a recording fake, so no key is read and no paid call happens,
and the projection is always the domain's own. The Runtime stays the source of
truth — the Task Center only keeps the projection of it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from _runtime_flow_support import (
    HISTORY_FIELD,
    LINKAGE_TASK_KEY,
    RUN_ID,
    SWITCH,
    FakeRuntime,
    authorize_via_route,
    block_outbound_network,
    build_client,
    create_unattended_task,
    park_other_unattended_tasks,
    queue_tick_via_route,
    reset_home,
    run_contract,
    schedule_a_run_via_route,
    start_via_route,
    stored_task,
)

QUEUE_SWITCH = "EVOFLOW_TASK_QUEUE_ENABLED"


@pytest.fixture()
def client():
    reset_home()
    return build_client()


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch):
    """Runtime opt-in on, the queue left at its default, no outbound network."""
    monkeypatch.setenv(SWITCH, "1")
    monkeypatch.delenv(QUEUE_SWITCH, raising=False)
    block_outbound_network(monkeypatch)


def _settle(fake: FakeRuntime, status: str) -> FakeRuntime:
    """Make the fake Runtime answer with a settled status from now on.

    ``FakeRuntime`` is the stand-in for the Runtime at its public boundary, so
    moving it on is exactly how a real run moving on is expressed here.
    """
    fake._status = status
    return fake


def _tick(client, task_id: str) -> dict[str, Any]:
    park_other_unattended_tasks(task_id)
    return queue_tick_via_route(client)


def _projection_for(tick_result: dict[str, Any], task_id: str) -> dict[str, Any] | None:
    """The tick's report about the Runtime projection of one task, if it made one."""
    for entry in tick_result.get("results") or []:
        if str(entry.get("task_id") or "") != task_id:
            continue
        if str(entry.get("action") or "") == "runtime_projected":
            return dict(entry)
    return None


def _history(task_id: str) -> list[dict[str, Any]]:
    stored = stored_task(task_id)
    entries = stored.get(HISTORY_FIELD)
    return list(entries) if isinstance(entries, list) else []


# --- the four settled outcomes reach the task ------------------------------------


def test_a_completed_run_settles_the_task_as_completed(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, fake = schedule_a_run_via_route(client, monkeypatch)
    _settle(fake, "completed")

    entry = _projection_for(_tick(client, task_id), task_id)

    assert entry is not None, "the tick did not project the linked run"
    assert entry["runtime_run_id"] == RUN_ID
    assert stored_task(task_id)["status"] == "completed"
    assert _history(task_id)[-1]["runtime_status"] == "completed"


def test_a_failed_run_settles_the_task_as_failed(client, monkeypatch: pytest.MonkeyPatch) -> None:
    task_id, fake = schedule_a_run_via_route(client, monkeypatch)
    _settle(fake, "failed")

    _tick(client, task_id)

    stored = stored_task(task_id)
    assert stored["status"] == "failed"
    assert _history(task_id)[-1]["runtime_status"] == "failed"


def test_a_timed_out_run_settles_the_task_as_failed_and_keeps_the_runtime_word(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``timed_out`` is not a TaskStatus; the frozen mapping converges it to ``failed``."""
    task_id, fake = schedule_a_run_via_route(client, monkeypatch)
    _settle(fake, "timed_out")

    _tick(client, task_id)

    stored = stored_task(task_id)
    assert stored["status"] == "failed"
    assert _history(task_id)[-1]["runtime_status"] == "timed_out"


def test_a_cancelled_run_settles_the_task_as_cancelled(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, fake = schedule_a_run_via_route(client, monkeypatch)
    _settle(fake, "cancelled")

    _tick(client, task_id)

    stored = stored_task(task_id)
    assert stored["status"] == "cancelled"
    assert _history(task_id)[-1]["runtime_status"] == "cancelled"


def test_the_settled_task_reads_back_through_the_real_routes(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, fake = schedule_a_run_via_route(client, monkeypatch)
    _settle(fake, "completed")

    _tick(client, task_id)

    body = client.get(f"/api/tasks/{task_id}").json()
    history = client.get(f"/api/tasks/{task_id}/execution-history").json()

    assert body["status"] == "completed"
    assert history["total_executions"] >= 1
    assert history["execution_history"][-1]["runtime_status"] == "completed"
    assert all(e["runtime_run_id"] == RUN_ID for e in history["execution_history"])
    # The Runtime stays the source of truth; the bookkeeping stays hidden.
    assert LINKAGE_TASK_KEY not in json.dumps(body, ensure_ascii=False)


# --- the task the queue would otherwise never look at again ----------------------


def test_a_task_in_neither_queue_list_is_still_reached(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression this card exists for.

    A run in ``running`` moves the task to ``executing``. With no subtasks and
    live consent, the task is then in neither the candidate list nor the
    in-progress list — so before this card nothing would ever ask the Runtime how
    the run ended, and the task would stay ``executing`` forever.
    """
    from app.gateway.unattended_task_pipeline import (
        list_unattended_candidates,
        list_unattended_in_progress,
    )

    task_id, fake = schedule_a_run_via_route(client, monkeypatch)

    _settle(fake, "running")
    _tick(client, task_id)
    assert stored_task(task_id)["status"] == "executing"

    ids = lambda rows: {str(r.get("id") or "") for r in rows}  # noqa: E731
    assert task_id not in ids(list_unattended_candidates())
    assert task_id not in ids(list_unattended_in_progress())

    _settle(fake, "completed")
    entry = _projection_for(_tick(client, task_id), task_id)

    assert entry is not None, "a linked task outside both queue lists was never reached"
    assert stored_task(task_id)["status"] == "completed"


def test_the_running_stage_is_projected_once_and_not_repeated(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The projection is idempotent, so a second tick adds no second record."""
    task_id, fake = schedule_a_run_via_route(client, monkeypatch)
    _settle(fake, "running")

    _tick(client, task_id)
    after_first = _history(task_id)
    _tick(client, task_id)

    assert _history(task_id) == after_first


# --- a settled task is not moved again -------------------------------------------


def test_a_settled_task_is_not_reopened_by_a_later_run_state(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, fake = schedule_a_run_via_route(client, monkeypatch)
    _settle(fake, "completed")
    _tick(client, task_id)
    settled = _history(task_id)

    # The Runtime now answers something older. The task must not go backwards.
    _settle(fake, "running")
    _tick(client, task_id)

    assert stored_task(task_id)["status"] == "completed"
    assert _history(task_id) == settled


def test_a_settled_task_is_left_to_the_existing_retry_semantics(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A terminal task is not re-driven by the projection either."""
    task_id, fake = schedule_a_run_via_route(client, monkeypatch)
    _settle(fake, "failed")
    _tick(client, task_id)

    stored = stored_task(task_id)
    assert stored["status"] == "failed"
    assert int(stored.get("unattended_attempts") or 0) == 0


# --- nothing is created, and nothing happens when the switch is off ---------------


def test_the_projection_never_creates_a_second_run(client, monkeypatch: pytest.MonkeyPatch) -> None:
    task_id, fake = schedule_a_run_via_route(client, monkeypatch)
    _settle(fake, "completed")

    _tick(client, task_id)
    _tick(client, task_id)

    assert len(fake.create_calls) == 1
    assert fake.status_calls, "the projection must read the run, not guess it"


def test_an_unlinked_task_is_not_projected(client, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = run_contract(FakeRuntime(status="completed"), monkeypatch)
    task_id = create_unattended_task(client)
    authorize_via_route(client, task_id)

    result = _tick(client, task_id)

    assert _projection_for(result, task_id) is None
    assert fake.create_calls == []
    assert fake.status_calls == []
    assert _history(task_id) == []


def test_with_the_switch_off_the_linked_sweep_is_empty(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The opt-in switch is the master gate for the sweep, and it costs no scan."""
    from app.gateway.unattended_task_pipeline import list_unattended_runtime_linked

    task_id, fake = schedule_a_run_via_route(client, monkeypatch)
    _settle(fake, "completed")
    before_row = stored_task(task_id)
    before_calls = (len(fake.create_calls), len(fake.status_calls))
    monkeypatch.delenv(SWITCH, raising=False)

    assert list_unattended_runtime_linked() == []

    assert stored_task(task_id) == before_row
    assert (len(fake.create_calls), len(fake.status_calls)) == before_calls, (
        "with the switch off the Runtime must not be called at all"
    )


def test_with_the_switch_off_the_tick_projects_nothing(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """And the tick keeps handing the task to the legacy pipeline, exactly as before."""
    task_id, fake = schedule_a_run_via_route(client, monkeypatch)
    _settle(fake, "completed")
    before_calls = (len(fake.create_calls), len(fake.status_calls))
    monkeypatch.delenv(SWITCH, raising=False)

    result = _tick(client, task_id)

    assert _projection_for(result, task_id) is None
    assert (len(fake.create_calls), len(fake.status_calls)) == before_calls


def test_the_manual_run_now_control_also_projects_the_run(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other real entry into the same convergence point."""
    fake = run_contract(FakeRuntime(status="queued"), monkeypatch)
    task_id = create_unattended_task(client)
    authorize_via_route(client, task_id)
    assert start_via_route(client, task_id)["advance"]["action"] == "runtime"

    _settle(fake, "completed")
    step = start_via_route(client, task_id)["advance"]

    assert step["action"] == "runtime"
    assert step["runtime_projection"] == "reconciled"
    assert stored_task(task_id)["status"] == "completed"
