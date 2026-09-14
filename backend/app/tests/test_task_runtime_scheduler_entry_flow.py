"""Vertical integration: the scheduler / queue tick pickup, through its real route.

AG-G2-AUTO-043.

The scheduler is ``POST /api/tasks/queue/tick`` → ``task_queue_tick``. It walks the
unattended tasks the store already exposes and, for each, either continues it (the
in-progress list) or advances it (the candidate list). Two things decide what a tick
does to an opt-in Runtime task, and this file pins both through the real route:

* the **persisted linkage** (AG-G2-AUTO-021's guard) — once an attempt has a run,
  ``decide_runtime_pickup`` reports ``already_scheduled`` and the tick *skips* the
  task instead of advancing it. That is why a duplicate tick in the same window
  cannot start a second run, and why the answer is the same from any instance:
  nothing in-process is load-bearing, so there is no leader, lease or fencing here.
* the **two independent switches** — ``EVOFLOW_TASK_QUEUE_ENABLED`` gates the queue
  itself, ``EVOFLOW_AUTOMATION_UNATTENDED_RUNTIME`` gates only the Runtime opt-in.
  Neither one changes the other, and with the Runtime switch off the tick keeps
  handing tasks to the legacy pipeline exactly as it did before.

The reachable state that makes a tick matter is created through the real controls:
run-now (``POST /api/tasks/{id}/start``) on an authorized task with no bound plan
leaves the task ``pending`` — a queue *candidate* — with a live run linked. From
there a tick is the interesting case; before that a tick only ever plans.

Nothing here hand-writes a final SQLite state; the Runtime is replaced at its public
boundary by a recording fake, and the tick is exercised through its own route.
"""

from __future__ import annotations

import pytest
from _runtime_flow_support import (
    LINKAGE_TASK_KEY,
    RUN_ID,
    SWITCH,
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
    start_via_route,
    stored_task,
)

QUEUE_SWITCH = "EVOFLOW_TASK_QUEUE_ENABLED"

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
    """Runtime opt-in on, queue switch left at its default, no outbound network."""
    monkeypatch.setenv(SWITCH, "1")
    monkeypatch.delenv(QUEUE_SWITCH, raising=False)
    block_outbound_network(monkeypatch)


def _tick(client, task_id: str) -> dict:
    """One real scheduler tick, kept hermetic for the task under test."""
    park_other_unattended_tasks(task_id)
    return queue_tick_via_route(client)


def _entry_for(tick_result: dict, task_id: str) -> dict | None:
    """The tick's own report about one task, if it walked it at all."""
    for entry in tick_result.get("results") or []:
        if str(entry.get("task_id") or "") == task_id:
            return dict(entry)
    return None


def _scheduled(client, monkeypatch: pytest.MonkeyPatch) -> tuple[str, FakeRuntime]:
    """A task with a live run that is still a queue candidate.

    Authorizing promotes ``pending`` → ``planned``; run-now on a task with no bound
    plan puts it back to ``pending`` and immediately advances it, which is what
    establishes the run. So the task ends up authorized, linked, and ``pending`` —
    exactly the state the scheduler's candidate list picks up.
    """
    fake = run_contract(FakeRuntime(), monkeypatch)
    task_id = create_unattended_task(client)
    authorize_via_route(client, task_id)
    step = start_via_route(client, task_id)["advance"]
    assert step["action"] == "runtime", step
    stored = stored_task(task_id)
    assert stored["status"] == "pending", stored.get("status")
    assert stored["execution_authorized"] is True
    assert stored[LINKAGE_TASK_KEY]["runtime_run_id"] == RUN_ID
    return task_id, fake


def _failed(client, monkeypatch: pytest.MonkeyPatch) -> tuple[str, FakeRuntime]:
    """A failed task whose attempt already has a run — the retry entry's subject."""
    fake = run_contract(FakeRuntime(frames=FAILURE_FRAMES, status="failed"), monkeypatch)
    task_id = create_unattended_task(client)
    authorize_via_route(client, task_id)
    assert start_via_route(client, task_id)["advance"]["action"] == "runtime"
    push_frames(task_id, fake.frames)
    assert stored_task(task_id)["status"] == "failed"
    return task_id, fake


# --- a duplicate tick in the same window starts no second run ----------------------


def test_the_tick_does_not_start_a_second_run_for_a_scheduled_attempt(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, fake = _scheduled(client, monkeypatch)

    _tick(client, task_id)

    assert len(fake.create_calls) == 1


def test_two_ticks_in_the_same_window_still_start_only_one_run(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, fake = _scheduled(client, monkeypatch)

    _tick(client, task_id)
    _tick(client, task_id)

    assert len(fake.create_calls) == 1


def test_the_tick_reports_the_run_the_attempt_already_has(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The skip is explained in the tick's own report, not silently dropped."""
    task_id, _ = _scheduled(client, monkeypatch)

    entry = _entry_for(_tick(client, task_id), task_id)

    assert entry is not None, "the tick walked past the candidate without reporting it"
    assert entry["action"] == "already_scheduled"
    assert entry["runtime_run_id"] == RUN_ID


def test_the_tick_leaves_the_linked_run_id_untouched(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, _ = _scheduled(client, monkeypatch)
    before = dict(stored_task(task_id)[LINKAGE_TASK_KEY])

    _tick(client, task_id)

    assert stored_task(task_id)[LINKAGE_TASK_KEY] == before


def test_the_tick_does_not_replan_or_re_authorize_a_scheduled_task(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A skipped task is left alone: the run is driving it, not the queue."""
    task_id, _ = _scheduled(client, monkeypatch)

    _tick(client, task_id)

    stored = stored_task(task_id)
    assert stored["status"] == "pending"
    assert stored["execution_authorized"] is True
    assert stored.get("plan_bound_at") in (None, "")


# --- the two switches are independent ---------------------------------------------


def test_the_queue_switch_off_makes_the_tick_inert(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, fake = _scheduled(client, monkeypatch)
    monkeypatch.setenv(QUEUE_SWITCH, "0")

    result = _tick(client, task_id)

    assert result.get("skipped") is True
    assert result.get("reason") == "disabled"
    assert len(fake.create_calls) == 1
    # And with no queue there is no advancement at all, so the task is untouched.
    assert stored_task(task_id)["status"] == "pending"


def test_the_queue_switch_does_not_change_the_runtime_opt_in(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.gateway import task_runtime_optin as optin

    monkeypatch.setenv(QUEUE_SWITCH, "0")

    assert optin.runtime_unattended_enabled() is True


def test_the_runtime_switch_does_not_gate_the_queue(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.gateway.task_queue_runner import task_queue_enabled

    monkeypatch.delenv(SWITCH, raising=False)

    assert task_queue_enabled() is True


def test_with_the_runtime_off_the_tick_still_picks_the_task_up(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard is inert when the Runtime is off: a tick must pick what it always did."""
    from app.gateway.task_runtime_schedule import decide_runtime_pickup

    task_id, fake = _scheduled(client, monkeypatch)
    monkeypatch.delenv(SWITCH, raising=False)

    decision = decide_runtime_pickup(stored_task(task_id))

    assert decision.action == "disabled"
    assert decision.should_skip is False, "with the Runtime off the tick must not skip"
    assert len(fake.create_calls) == 1


# --- the default-off switch preserves the old retry path --------------------------


def test_a_failed_task_is_still_requeued_by_the_tick_with_the_runtime_off(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, fake = _failed(client, monkeypatch)  # the failure itself ran with the Runtime on
    monkeypatch.delenv(SWITCH, raising=False)
    open_the_retry_window(task_id)

    _tick(client, task_id)  # the queue's own retry: a settled task is handed to the pipeline

    stored = stored_task(task_id)
    assert stored["status"] == "pending"
    assert stored["unattended_attempts"] == 1
    assert len(fake.create_calls) == 1, "the tick must add no Runtime run"


def test_the_same_requeue_happens_with_the_runtime_on(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-pollution, the other way round: the Runtime does not change the retry."""
    task_id, fake = _failed(client, monkeypatch)
    open_the_retry_window(task_id)

    entry = _entry_for(_tick(client, task_id), task_id)

    stored = stored_task(task_id)
    assert stored["status"] == "pending"
    assert stored["unattended_attempts"] == 1
    assert len(fake.create_calls) == 1
    # A terminal task defers to the existing semantics rather than being claimed
    # by the Runtime branch — the retry is still the pipeline's, not the Runtime's.
    assert entry is not None
    assert entry["action"] == "requeued_for_retry"


# --- no leader / lease / fencing: any instance reaches the same persisted answer ---


def test_a_second_instance_reaches_the_same_persisted_answer(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two app instances over one store: the second sees the first's linkage.

    There is nothing to elect and nothing to fence — the guard reads the persisted
    task row, so a fresh instance answers ``already_scheduled`` and adds no run.
    """
    task_id, fake = _scheduled(client, monkeypatch)
    second_instance = build_client()

    entry = _entry_for(_tick(second_instance, task_id), task_id)

    assert entry is not None
    assert entry["action"] == "already_scheduled"
    assert entry["runtime_run_id"] == RUN_ID
    assert len(fake.create_calls) == 1


def test_a_rebuilt_app_does_not_re_trigger_the_run(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rebuilt app — the same store after a restart — adds no run either."""
    task_id, fake = _scheduled(client, monkeypatch)

    _tick(build_client(), task_id)

    assert len(fake.create_calls) == 1
    assert stored_task(task_id)[LINKAGE_TASK_KEY]["runtime_run_id"] == RUN_ID
