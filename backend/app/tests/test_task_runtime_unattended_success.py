"""Vertical integration: an unattended Task Center task driven by the Runtime.

AG-G2-AUTO-030.

Every step goes through an entry that already exists, in the order a real user
would cause them:

1. the task is created through the real Task Center route (``POST /api/tasks``);
2. execution is authorized through the existing authorization service, which is
   the same gate the "start execution" control uses;
3. the unattended pipeline advances the task through
   ``advance_unattended_task`` — the function the queue tick calls — with the
   Runtime opt-in switch on;
4. the Runtime's own frames are pushed through the existing event adapter;
5. the result is read back through the real routes.

Nothing hand-writes a final SQLite state, and nothing here touches the network: a
recording contract stands in for the Runtime at its public boundary, so no real
API key is read and no paid call can happen. The Runtime remains the source of
truth for run state — the Task Center only keeps the projection of it.
"""

from __future__ import annotations

import json

import pytest
from _runtime_flow_support import (
    HISTORY_FIELD,
    LINKAGE_TASK_KEY,
    FakeRuntime,
    authorize,
    build_client,
    create_unattended_task,
    frame,
    reset_home,
    run_contract,
    save_task,
    storage,
    stored_task,
    tick,
)

SWITCH = "EVOFLOW_AUTOMATION_UNATTENDED_RUNTIME"
RUN_ID = "run-flow-1"

SUCCESS_FRAMES = [
    frame("run.running", sequence=0),
    frame("run.completed", sequence=1, payload={"result": {"summary": "周报已生成"}}),
]


@pytest.fixture()
def client():
    reset_home()
    return build_client()


@pytest.fixture(autouse=True)
def _switch_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(SWITCH, "1")


def _push(task_id: str, fake: FakeRuntime) -> dict:
    from _runtime_flow_support import push_frames

    return push_frames(task_id, fake.frames)


# --- the happy path --------------------------------------------------------------


def test_an_unattended_task_reaches_the_runtime_once(client, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = run_contract(FakeRuntime(frames=SUCCESS_FRAMES), monkeypatch)

    task_id = create_unattended_task(client)
    authorize(task_id)
    step = tick(task_id)

    assert step["action"] == "runtime", step
    assert step["runtime_run_id"] == RUN_ID
    assert len(fake.create_calls) == 1


def test_the_run_carries_the_trusted_identity_and_a_deterministic_trigger(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = run_contract(FakeRuntime(frames=SUCCESS_FRAMES), monkeypatch)

    task_id = create_unattended_task(client)
    authorize(task_id)
    tick(task_id)

    call = fake.create_calls[0]
    assert call["org_id"] == "local"
    assert call["idempotency_key"]
    # No client-supplied identity may reach the Runtime through the input payload.
    assert "org_id" not in call["input_payload"]
    assert "tenant_id" not in call["input_payload"]


def test_the_linkage_is_persisted_with_the_task(client, monkeypatch: pytest.MonkeyPatch) -> None:
    run_contract(FakeRuntime(frames=SUCCESS_FRAMES), monkeypatch)

    task_id = create_unattended_task(client)
    authorize(task_id)
    tick(task_id)

    stored = stored_task(task_id)

    assert stored[LINKAGE_TASK_KEY]["runtime_run_id"] == RUN_ID
    # Establishing the run writes the linkage and nothing else: the task's own
    # status is the one the existing authorization left it in, and no legacy
    # execution has written history.
    assert stored["status"] == "planned"
    assert stored[HISTORY_FIELD] == []


def test_a_second_tick_does_not_create_a_second_run(client, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = run_contract(FakeRuntime(frames=SUCCESS_FRAMES), monkeypatch)

    task_id = create_unattended_task(client)
    authorize(task_id)
    tick(task_id)
    tick(task_id)

    assert len(fake.create_calls) == 1


def test_the_projection_completes_the_task_with_a_sanitised_result(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = run_contract(
        FakeRuntime(
            frames=[
                frame("run.running", sequence=0),
                frame(
                    "run.completed",
                    sequence=1,
                    payload={
                        "result": {
                            "summary": "周报已生成",
                            "internal_path": "/var/private/report.csv",
                        }
                    },
                ),
            ]
        ),
        monkeypatch,
    )

    task_id = create_unattended_task(client)
    authorize(task_id)
    tick(task_id)
    stored = _push(task_id, fake)

    assert stored["status"] == "completed"
    record = stored[HISTORY_FIELD][-1]
    assert record["runtime_status"] == "completed"
    assert record["result_available"] is True
    assert record["result"] == {"summary": "周报已生成"}
    assert "/var/private/report.csv" not in json.dumps(stored, ensure_ascii=False)


def test_the_completed_task_reads_back_through_the_real_routes(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = run_contract(FakeRuntime(frames=SUCCESS_FRAMES), monkeypatch)

    task_id = create_unattended_task(client)
    authorize(task_id)
    tick(task_id)
    _push(task_id, fake)

    history = client.get(f"/api/tasks/{task_id}/execution-history").json()
    body = client.get(f"/api/tasks/{task_id}").json()

    assert history["total_executions"] == 2
    assert [e["runtime_status"] for e in history["execution_history"]] == [
        "running",
        "completed",
    ]
    assert body["status"] == "completed"
    # The Runtime is the source of truth; the task only carries the projection of it.
    assert "runtime_run_linkage" not in body


def test_the_whole_flow_leaves_a_consistent_terminal_task(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = run_contract(FakeRuntime(frames=SUCCESS_FRAMES), monkeypatch)

    task_id = create_unattended_task(client)
    authorize(task_id)
    tick(task_id)
    stored = _push(task_id, fake)

    assert stored["status"] == "completed"
    assert stored[HISTORY_FIELD], "the projection must be visible"
    assert all(entry.get("runtime_run_id") == RUN_ID for entry in stored[HISTORY_FIELD])


# --- the switch, which keeps the legacy path intact -------------------------------


def test_with_the_switch_off_the_legacy_path_runs_and_no_run_is_created(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = run_contract(FakeRuntime(frames=SUCCESS_FRAMES), monkeypatch)
    monkeypatch.setenv(SWITCH, "0")

    task_id = create_unattended_task(client)
    authorize(task_id)
    step = tick(task_id)

    assert step.get("action") != "runtime"
    assert fake.create_calls == []
    assert LINKAGE_TASK_KEY not in stored_task(task_id)


def test_an_unauthorized_task_never_reaches_the_runtime(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = run_contract(FakeRuntime(frames=SUCCESS_FRAMES), monkeypatch)

    task_id = create_unattended_task(client)
    step = tick(task_id)  # not authorized

    assert fake.create_calls == []
    assert step.get("action") != "runtime"
    assert LINKAGE_TASK_KEY not in stored_task(task_id)


def test_a_handwritten_organization_on_the_task_is_ignored(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client-writable field must not be able to choose the run's org."""
    fake = run_contract(FakeRuntime(frames=SUCCESS_FRAMES), monkeypatch)

    task_id = create_unattended_task(client)
    authorize(task_id)
    stored = stored_task(task_id)
    stored["org_id"] = "someone-elses-org"
    stored["tenant_id"] = "someone-elses-tenant"
    save_task(task_id, stored)

    tick(task_id)

    assert fake.create_calls[0]["org_id"] == "local"


def test_the_storage_helpers_see_the_same_task_the_route_does(client) -> None:
    """A guard on the scaffolding itself, so a wrong helper cannot pass silently."""
    task_id = create_unattended_task(client)

    assert storage() is not None
    assert stored_task(task_id)["id"] == task_id
    assert client.get(f"/api/tasks/{task_id}").json()["id"] == task_id
