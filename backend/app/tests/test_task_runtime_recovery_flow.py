"""Vertical integration: recovering a lagging Task Center projection.

AG-G2-AUTO-034.

The situation being reproduced: the task is already linked to a run, the Runtime
has moved on (or had already moved on while nobody was watching), and the Task
Center's own projection is behind. That is what a restart, a dropped connection
or a lagging reader leaves behind.

Recovery goes through the paths that already exist — the real read route, the
read-only reconciliation, the reconnect view and the queue tick — and is asserted
to do exactly what those paths are allowed to do: read the Runtime, never execute
anything, never create a second run, and never append a record twice.
"""

from __future__ import annotations

import asyncio

import pytest
from _runtime_flow_support import (
    LINKAGE_TASK_KEY,
    RUN_ID,
    FakeRuntime,
    authorize,
    block_outbound_network,
    build_client,
    create_unattended_task,
    frame,
    history_of,
    identity,
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


def _lagging_task(
    client, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, FakeRuntime]:
    """A linked task whose projection stopped one stage behind the run.

    The link exists, the task's own history stops at ``running``, and the Runtime
    has since completed — the state a restart leaves behind.
    """
    fake = run_contract(
        FakeRuntime(frames=[frame("run.running", sequence=0)], status="completed"),
        monkeypatch,
    )
    task_id = create_unattended_task(client)
    authorize(task_id)
    assert tick(task_id)["action"] == "runtime"
    push_frames(task_id, fake.frames)
    assert stored_task(task_id)["status"] == "executing"
    return task_id, fake


def _context(task_id: str):
    from app.gateway.task_runtime_context import build_task_runtime_context

    return build_task_runtime_context(
        task=stored_task(task_id), authz=identity(), authorized=True
    )


# --- the lag is real, and visible -------------------------------------------------


def test_the_lag_is_visible_before_recovery(client, monkeypatch: pytest.MonkeyPatch) -> None:
    task_id, _ = _lagging_task(client, monkeypatch)

    body = client.get(f"/api/tasks/{task_id}/execution-history").json()

    assert [e["runtime_status"] for e in body["execution_history"]] == ["running"]
    assert client.get(f"/api/tasks/{task_id}").json()["status"] == "executing"


# --- read-only reconciliation catches the projection up ---------------------------


def test_reconciliation_catches_the_projection_up(client, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.gateway.task_runtime_reconcile import reconcile_linked_task_runtime

    task_id, _ = _lagging_task(client, monkeypatch)
    from _runtime_flow_support import save_task

    outcome = asyncio.run(
        reconcile_linked_task_runtime(
            stored_task(task_id), context=_context(task_id), contract=run_contract(FakeRuntime(status="completed"), monkeypatch)
        )
    )

    assert outcome.action == "reconciled", outcome
    assert outcome.updated_task is not None
    save_task(task_id, outcome.updated_task)

    stored = stored_task(task_id)
    assert stored["status"] == "completed"
    assert statuses_of(stored) == ["running", "completed"]


def test_reconciliation_spends_only_a_read(client, monkeypatch: pytest.MonkeyPatch) -> None:
    """Recovery reads the Runtime; it never executes or creates anything."""
    from app.gateway.task_runtime_reconcile import reconcile_linked_task_runtime

    task_id, _ = _lagging_task(client, monkeypatch)
    reader = run_contract(FakeRuntime(status="completed"), monkeypatch)

    asyncio.run(
        reconcile_linked_task_runtime(
            stored_task(task_id), context=_context(task_id), contract=reader
        )
    )

    assert reader.status_calls == [RUN_ID]
    assert reader.create_calls == []
    assert reader.cancel_calls == []


def test_reconciliation_is_idempotent(client, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.gateway.task_runtime_reconcile import reconcile_linked_task_runtime

    task_id, _ = _lagging_task(client, monkeypatch)
    reader = run_contract(FakeRuntime(status="completed"), monkeypatch)
    from _runtime_flow_support import save_task

    first = asyncio.run(
        reconcile_linked_task_runtime(
            stored_task(task_id), context=_context(task_id), contract=reader
        )
    )
    assert first.updated_task is not None
    save_task(task_id, first.updated_task)
    length_after_first = len(history_of(stored_task(task_id)))

    second = asyncio.run(
        reconcile_linked_task_runtime(
            stored_task(task_id), context=_context(task_id), contract=reader
        )
    )

    # A task that is already settled has nothing to reconcile, so the second pass
    # is answered as a no-op — the point is that no second record appears.
    assert second.action in {"terminal", "unchanged"}, second
    assert second.updated_task is None
    assert len(history_of(stored_task(task_id))) == length_after_first


# --- the reconnect view -----------------------------------------------------------


def test_the_reconnect_view_catches_up_without_re_executing(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    from _runtime_flow_support import save_task

    from app.gateway.task_runtime_recovery import recover_task_stream

    task_id, _ = _lagging_task(client, monkeypatch)
    reader = run_contract(FakeRuntime(status="completed"), monkeypatch)

    outcome = asyncio.run(
        recover_task_stream(
            stored_task(task_id), context=_context(task_id), contract=reader
        )
    )

    assert outcome.action == "caught_up", outcome
    assert outcome.view.status == "completed"
    assert outcome.updated_task is not None
    save_task(task_id, outcome.updated_task)
    assert reader.create_calls == []


def test_the_reconnect_view_can_avoid_the_runtime_entirely(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.gateway.task_runtime_recovery import recover_task_stream

    task_id, _ = _lagging_task(client, monkeypatch)
    reader = run_contract(FakeRuntime(status="completed"), monkeypatch)

    outcome = asyncio.run(
        recover_task_stream(
            stored_task(task_id), context=_context(task_id), contract=reader, catch_up=False
        )
    )

    assert outcome.action == "read"
    assert outcome.view.status == "executing"
    assert reader.status_calls == []


def test_a_reconnect_after_the_catch_up_reads_the_new_state(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    from _runtime_flow_support import save_task

    from app.gateway.task_runtime_recovery import recover_task_stream

    task_id, _ = _lagging_task(client, monkeypatch)
    reader = run_contract(FakeRuntime(status="completed"), monkeypatch)

    first = asyncio.run(
        recover_task_stream(stored_task(task_id), context=_context(task_id), contract=reader)
    )
    assert first.updated_task is not None
    save_task(task_id, first.updated_task)

    second = asyncio.run(
        recover_task_stream(stored_task(task_id), context=_context(task_id), contract=reader)
    )

    assert second.action in {"terminal", "unchanged"}, second
    assert second.view.status == "completed"
    assert second.updated_task is None


# --- the queue path ---------------------------------------------------------------


def test_the_queue_does_not_re_pick_a_task_that_already_has_a_run(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scheduler answers from the persisted linkage, so a restart is safe.

    AG-G2-AUTO-003-A01 moved where this lands, and only that: the tick now
    projects the linked run's own state back onto the row, so a run that has
    already settled settles the task with it and the scheduler afterwards reports
    ``task_settled`` instead of ``already_scheduled``. Every assertion this test
    exists for is kept, and the persisted-linkage guard is now asserted where it
    is observable — on the row the tick was handed — with the post-tick outcome
    asserted as what it now is.
    """
    from app.gateway.task_runtime_schedule import decide_runtime_pickup

    task_id, fake = _lagging_task(client, monkeypatch)
    creates_before = len(fake.create_calls)

    # The guard, on the state the tick is about to be given.
    decision = decide_runtime_pickup(stored_task(task_id))
    assert decision.should_skip
    assert decision.action == "already_scheduled"
    assert decision.runtime_run_id == RUN_ID

    # The real queue entry a scheduler tick uses.
    response = client.post("/api/tasks/queue/tick")
    assert response.status_code == 200, response.text

    assert len(fake.create_calls) == creates_before
    assert stored_task(task_id)[LINKAGE_TASK_KEY]["runtime_run_id"] == RUN_ID

    # The tick settled it from the run's own answer — it did not re-pick it, and
    # it created nothing.
    assert stored_task(task_id)["status"] == "completed"
    assert decide_runtime_pickup(stored_task(task_id)).action == "task_settled"


def test_recovery_never_creates_a_second_run(client, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.gateway.task_runtime_recovery import recover_task_stream

    task_id, fake = _lagging_task(client, monkeypatch)
    reader = run_contract(FakeRuntime(status="completed"), monkeypatch)

    asyncio.run(recover_task_stream(stored_task(task_id), context=_context(task_id), contract=reader))
    tick(task_id)

    assert len(fake.create_calls) == 1
    assert fake.cancel_calls == []
    assert stored_task(task_id)[LINKAGE_TASK_KEY]["runtime_run_id"] == RUN_ID


def test_the_caught_up_state_is_what_the_routes_serve(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    from _runtime_flow_support import save_task

    from app.gateway.task_runtime_recovery import recover_task_stream

    task_id, _ = _lagging_task(client, monkeypatch)
    reader = run_contract(FakeRuntime(status="completed"), monkeypatch)
    outcome = asyncio.run(
        recover_task_stream(stored_task(task_id), context=_context(task_id), contract=reader)
    )
    assert outcome.updated_task is not None
    save_task(task_id, outcome.updated_task)

    body = client.get(f"/api/tasks/{task_id}").json()
    history = client.get(f"/api/tasks/{task_id}/execution-history").json()

    assert body["status"] == "completed"
    assert [e["runtime_status"] for e in history["execution_history"]] == [
        "running",
        "completed",
    ]
