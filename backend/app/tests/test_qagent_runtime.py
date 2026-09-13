from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import StaticPool

from app.qagent_runtime.contract import AssetMetadata, ExecutionResult, PlanResult
from app.qagent_runtime.events import EventType
from app.qagent_runtime.repository import RuntimeRepository
from app.qagent_runtime.service import RuntimeService, recover_incomplete_runs


class CountingPlanner:
    def __init__(self) -> None:
        self.calls = 0

    def plan(self, *, task_id: str, input_payload: dict[str, Any]) -> PlanResult:
        self.calls += 1
        return PlanResult(
            plan={
                "version": "test-plan-v1",
                "task_id": task_id,
                "steps": [{"step_id": "server-subtask-1", "kind": "server_only"}],
                "input": input_payload,
            },
            recovery_context={"next_step": "server-subtask-1"},
        )


class CountingExecutor:
    def __init__(self) -> None:
        self.calls = 0

    def execute(
        self,
        *,
        run_id: str,
        task_id: str,
        input_payload: dict[str, Any],
        plan: dict[str, Any],
    ) -> ExecutionResult:
        self.calls += 1
        return ExecutionResult(
            result={"ok": True, "run_id": run_id, "task_id": task_id, "input": input_payload},
            assets=[
                AssetMetadata(
                    asset_id=f"asset_{run_id}",
                    asset_type="test_result",
                    uri=f"qagent://runs/{run_id}/test-result",
                    metadata={"test": True},
                )
            ],
        )


@dataclass
class RuntimeFixture:
    repository: RuntimeRepository
    planner: CountingPlanner
    executor: CountingExecutor
    service: RuntimeService


@pytest.fixture
def runtime() -> RuntimeFixture:
    engine = sa.create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    repository = RuntimeRepository(engine, create_schema=True)
    planner = CountingPlanner()
    executor = CountingExecutor()
    yield RuntimeFixture(repository, planner, executor, RuntimeService(repository, planner=planner, executor=executor))
    engine.dispose()


@pytest.mark.asyncio
async def test_create_plan_approval_execution_persists_events_result_and_assets(runtime: RuntimeFixture) -> None:
    run = await runtime.service.create_run(task_id="task-1", input_payload={"prompt": "hello"})
    assert run["status"] == "queued"
    assert runtime.repository.get_run(run["run_id"])["status"] == "queued"
    assert [event["type"] for event in runtime.repository.list_events(run["run_id"])] == [
        EventType.RUN_CREATED,
        EventType.RUN_QUEUED,
    ]

    waiting = await runtime.service.start_run(run["run_id"])
    assert waiting["status"] == "waiting_approval"
    assert waiting["approval"]["status"] == "requested"
    assert runtime.planner.calls == 1
    assert runtime.executor.calls == 0

    completed = await runtime.service.grant_approval(
        waiting["approval"]["approval_id"],
        decided_by="operator-1",
        reason="safe server-only task",
    )
    assert completed["status"] == "completed"
    assert runtime.executor.calls == 1
    result = await runtime.service.get_result(run["run_id"])
    assert result["status"] == "completed"
    assert result["result"]["ok"] is True
    assert result["assets"][0]["asset_type"] == "test_result"

    event_types = [event["type"] for event in runtime.repository.list_events(run["run_id"])]
    assert event_types == [
        EventType.RUN_CREATED,
        EventType.RUN_QUEUED,
        EventType.RUN_PLANNING,
        EventType.RUN_WAITING_APPROVAL,
        EventType.APPROVAL_GRANTED,
        EventType.RUN_RUNNING,
        EventType.ASSET_AVAILABLE,
        EventType.RUN_COMPLETED,
    ]


@pytest.mark.asyncio
async def test_approval_is_required_and_duplicate_start_or_approval_is_idempotent(runtime: RuntimeFixture) -> None:
    run = await runtime.service.create_run(task_id="task-2", input_payload={})
    waiting = await runtime.service.start_run(run["run_id"])
    approval_id = waiting["approval"]["approval_id"]

    assert runtime.executor.calls == 0
    duplicate_start = await runtime.service.start_run(run["run_id"])
    assert duplicate_start["status"] == "waiting_approval"
    assert runtime.planner.calls == 1
    assert runtime.executor.calls == 0

    completed = await runtime.service.grant_approval(approval_id)
    duplicate_grant = await runtime.service.grant_approval(approval_id)
    assert completed["status"] == "completed"
    assert duplicate_grant["status"] == "completed"
    assert runtime.executor.calls == 1
    assert len(runtime.repository.list_assets(run["run_id"])) == 1


@pytest.mark.asyncio
async def test_event_stream_is_qagent_protocol_and_sequence_is_monotonic(runtime: RuntimeFixture) -> None:
    run = await runtime.service.create_run(task_id="task-3", input_payload={})
    waiting = await runtime.service.start_run(run["run_id"])
    await runtime.service.grant_approval(waiting["approval"]["approval_id"])

    events = [event async for event in runtime.service.stream_events(run["run_id"])]
    sequences = [event["sequence"] for event in events]
    assert sequences == list(range(1, len(events) + 1))
    assert all(set(event) == {"event_id", "run_id", "sequence", "occurred_at", "type", "payload"} and event["run_id"] == run["run_id"] and event["event_id"].startswith("evt_") for event in events)
    assert not any("langgraph" in str(event).lower() for event in events)

    resumed_events = [event async for event in runtime.service.stream_events(run["run_id"], after_sequence=5)]
    assert [event["sequence"] for event in resumed_events] == list(range(6, len(events) + 1))


@pytest.mark.asyncio
async def test_restart_recovers_waiting_approval_without_replanning(runtime: RuntimeFixture) -> None:
    run = await runtime.service.create_run(task_id="task-4", input_payload={})
    waiting = await runtime.service.start_run(run["run_id"])

    planner_after_restart = CountingPlanner()
    executor_after_restart = CountingExecutor()
    restarted = RuntimeService(
        runtime.repository,
        planner=planner_after_restart,
        executor=executor_after_restart,
    )
    recovered = await recover_incomplete_runs(restarted)

    assert recovered == [run["run_id"]]
    assert restarted.status(run["run_id"])["status"] == "waiting_approval"
    assert planner_after_restart.calls == 0
    assert executor_after_restart.calls == 0
    assert restarted.status(run["run_id"])["resume_count"] == 1

    completed = await restarted.grant_approval(waiting["approval"]["approval_id"])
    assert completed["status"] == "completed"
    assert executor_after_restart.calls == 1


@pytest.mark.asyncio
async def test_restart_recovers_running_run_once_and_does_not_reexecute_completed_run(runtime: RuntimeFixture) -> None:
    run = await runtime.service.create_run(task_id="task-5", input_payload={})
    waiting = await runtime.service.start_run(run["run_id"])
    approval_id = waiting["approval"]["approval_id"]
    runtime.repository.decide_approval(approval_id, status="granted", decided_by="operator", reason=None)
    runtime.repository.transition(
        run["run_id"],
        "running",
        EventType.RUN_RUNNING,
        {"approval_id": approval_id, "recovered_test": True},
        from_statuses={"waiting_approval"},
    )

    executor_after_restart = CountingExecutor()
    restarted = RuntimeService(runtime.repository, planner=CountingPlanner(), executor=executor_after_restart)
    assert await recover_incomplete_runs(restarted) == [run["run_id"]]
    assert restarted.status(run["run_id"])["status"] == "completed"
    assert executor_after_restart.calls == 1

    second_restart = RuntimeService(runtime.repository, planner=CountingPlanner(), executor=CountingExecutor())
    assert await recover_incomplete_runs(second_restart) == []
    assert len(runtime.repository.list_events(run["run_id"])) == 8


@pytest.mark.asyncio
async def test_idempotency_keys_are_scoped_by_organization(runtime: RuntimeFixture) -> None:
    key = "same-key"
    org_a = "identity:user:org-a"
    org_b = "identity:user:org-b"

    first_a = await runtime.service.create_run(
        org_id=org_a, task_id="task-a", input_payload={"org": "a"}, idempotency_key=key
    )
    duplicate_a = await runtime.service.create_run(
        org_id=org_a, task_id="task-a-duplicate", input_payload={"org": "a-duplicate"}, idempotency_key=key
    )
    first_b = await runtime.service.create_run(
        org_id=org_b, task_id="task-b", input_payload={"org": "b"}, idempotency_key=key
    )

    assert duplicate_a["run_id"] == first_a["run_id"]
    assert duplicate_a["task_id"] == "task-a"
    assert first_b["run_id"] != first_a["run_id"]
    assert first_b["org_id"] == org_b


@pytest.mark.asyncio
async def test_grant_decision_updates_approval_run_and_events_in_one_transaction(runtime: RuntimeFixture) -> None:
    run = await runtime.service.create_run(task_id="atomic-grant", input_payload={})
    waiting = await runtime.service.start_run(run["run_id"])
    approval_id = waiting["approval"]["approval_id"]
    before = runtime.repository.list_events(run["run_id"])

    approval, changed = runtime.repository.decide_approval_atomically(
        approval_id,
        status="granted",
        decided_by="operator",
        reason="approved",
    )

    assert changed is True
    assert approval["status"] == "granted"
    assert runtime.repository.get_run(run["run_id"])["status"] == "running"
    after = runtime.repository.list_events(run["run_id"])
    assert [event["type"] for event in after] == [
        *(event["type"] for event in before),
        EventType.APPROVAL_GRANTED,
        EventType.RUN_RUNNING,
    ]
    assert after[-2]["payload"]["approval_id"] == approval_id


@pytest.mark.asyncio
async def test_reject_decision_updates_error_and_events_in_one_transaction(runtime: RuntimeFixture) -> None:
    run = await runtime.service.create_run(task_id="atomic-reject", input_payload={})
    waiting = await runtime.service.start_run(run["run_id"])
    approval_id = waiting["approval"]["approval_id"]

    result = await runtime.service.reject_approval(
        approval_id,
        decided_by="operator",
        reason="unsafe",
    )

    assert result["status"] == "failed"
    assert result["approval"]["status"] == "rejected"
    assert result["error"] == {"code": "approval_rejected", "message": "unsafe"}
    events = runtime.repository.list_events(run["run_id"])
    assert [event["type"] for event in events[-2:]] == [
        EventType.APPROVAL_REJECTED,
        EventType.RUN_FAILED,
    ]
    assert events[-1]["payload"] == {"code": "approval_rejected", "message": "unsafe"}


@pytest.mark.asyncio
async def test_repeated_grant_and_reject_are_noops_without_duplicate_events(runtime: RuntimeFixture) -> None:
    grant_run = await runtime.service.create_run(task_id="retry-grant", input_payload={})
    grant_waiting = await runtime.service.start_run(grant_run["run_id"])
    grant_approval_id = grant_waiting["approval"]["approval_id"]
    await runtime.service.grant_approval(grant_approval_id)
    grant_event_count = len(runtime.repository.list_events(grant_run["run_id"]))

    duplicate_grant = await runtime.service.grant_approval(grant_approval_id)

    assert duplicate_grant["status"] == "completed"
    assert len(runtime.repository.list_events(grant_run["run_id"])) == grant_event_count

    reject_run = await runtime.service.create_run(task_id="retry-reject", input_payload={})
    reject_waiting = await runtime.service.start_run(reject_run["run_id"])
    reject_approval_id = reject_waiting["approval"]["approval_id"]
    await runtime.service.reject_approval(reject_approval_id, reason="no")
    reject_event_count = len(runtime.repository.list_events(reject_run["run_id"]))

    duplicate_reject = await runtime.service.reject_approval(reject_approval_id, reason="different")

    assert duplicate_reject["status"] == "failed"
    assert duplicate_reject["error"] == {"code": "approval_rejected", "message": "no"}
    assert len(runtime.repository.list_events(reject_run["run_id"])) == reject_event_count


@pytest.mark.asyncio
async def test_competing_grant_and_reject_have_one_winner_and_consistent_state(runtime: RuntimeFixture) -> None:
    run = await runtime.service.create_run(task_id="decision-race", input_payload={})
    waiting = await runtime.service.start_run(run["run_id"])
    approval_id = waiting["approval"]["approval_id"]
    grant_service = RuntimeService(runtime.repository, planner=CountingPlanner(), executor=CountingExecutor())
    reject_service = RuntimeService(runtime.repository, planner=CountingPlanner(), executor=CountingExecutor())

    grant_result, reject_result = await asyncio.gather(
        grant_service.grant_approval(approval_id, decided_by="grant"),
        reject_service.reject_approval(approval_id, decided_by="reject", reason="denied"),
    )

    persisted_run = runtime.repository.get_run(run["run_id"])
    persisted_approval = runtime.repository.get_approval(approval_id)
    event_types = [event["type"] for event in runtime.repository.list_events(run["run_id"])]
    assert persisted_approval["status"] in {"granted", "rejected"}
    if persisted_approval["status"] == "granted":
        assert persisted_run["status"] == "completed"
        assert event_types.count(EventType.APPROVAL_GRANTED) == 1
        assert event_types.count(EventType.RUN_RUNNING) == 1
        assert event_types.count(EventType.APPROVAL_REJECTED) == 0
    else:
        assert persisted_run["status"] == "failed"
        assert persisted_run["error_payload"]["code"] == "approval_rejected"
        assert event_types.count(EventType.APPROVAL_REJECTED) == 1
        assert event_types.count(EventType.RUN_FAILED) == 1
        assert event_types.count(EventType.APPROVAL_GRANTED) == 0
    assert {grant_result["status"], reject_result["status"]} <= {"running", "completed", "failed"}


@pytest.mark.asyncio
async def test_cancel_and_grant_race_serializes_on_run_and_never_leaves_waiting_run(runtime: RuntimeFixture) -> None:
    run = await runtime.service.create_run(task_id="cancel-race", input_payload={})
    waiting = await runtime.service.start_run(run["run_id"])
    approval_id = waiting["approval"]["approval_id"]
    grant_service = RuntimeService(runtime.repository, planner=CountingPlanner(), executor=CountingExecutor())
    cancel_service = RuntimeService(runtime.repository, planner=CountingPlanner(), executor=CountingExecutor())

    await asyncio.gather(
        grant_service.grant_approval(approval_id),
        cancel_service.request_cancel(run["run_id"]),
    )

    persisted_run = runtime.repository.get_run(run["run_id"])
    persisted_approval = runtime.repository.get_approval(approval_id)
    assert persisted_run["status"] in {"completed", "cancelled"}
    if persisted_approval["status"] == "requested":
        assert persisted_run["status"] == "cancelled"
    elif persisted_approval["status"] == "granted":
        assert persisted_run["status"] in {"completed", "cancelled"}
    else:
        pytest.fail(f"unexpected approval status: {persisted_approval['status']}")


@pytest.mark.asyncio
async def test_decision_event_failure_rolls_back_approval_run_and_events(runtime: RuntimeFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    run = await runtime.service.create_run(task_id="atomic-failure", input_payload={})
    waiting = await runtime.service.start_run(run["run_id"])
    approval_id = waiting["approval"]["approval_id"]
    before_events = runtime.repository.list_events(run["run_id"])
    original_append_event = runtime.repository._append_event

    def fail_approval_event(conn: sa.Connection, run_id: str, event_type: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        if event_type == EventType.APPROVAL_GRANTED:
            raise RuntimeError("injected approval event failure")
        return original_append_event(conn, run_id, event_type, payload, **kwargs)

    monkeypatch.setattr(runtime.repository, "_append_event", fail_approval_event)
    with pytest.raises(RuntimeError, match="injected approval event failure"):
        await runtime.service.grant_approval(approval_id)

    assert runtime.repository.get_approval(approval_id)["status"] == "requested"
    assert runtime.repository.get_run(run["run_id"])["status"] == "waiting_approval"
    assert runtime.repository.get_run(run["run_id"])["error_payload"] is None
    assert runtime.repository.list_events(run["run_id"]) == before_events


@pytest.mark.asyncio
async def test_approval_run_and_event_access_remain_organization_scoped(runtime: RuntimeFixture) -> None:
    org_a = "identity:user:atomic-org-a"
    org_b = "identity:user:atomic-org-b"
    run = await runtime.service.create_run(org_id=org_a, task_id="org-a", input_payload={})
    waiting = await runtime.service.start_run(run["run_id"], org_id=org_a)
    approval_id = waiting["approval"]["approval_id"]

    assert runtime.repository.get_run(run["run_id"], org_id=org_b) is None
    assert runtime.repository.get_approval(approval_id, org_id=org_b) is None
    assert runtime.repository.list_events(run["run_id"], org_id=org_b) == []
    assert runtime.repository.decide_approval_atomically(
        approval_id,
        status="granted",
        decided_by="wrong-org",
        reason=None,
        org_id=org_b,
    ) == (None, False)
    assert runtime.repository.get_approval(approval_id, org_id=org_a)["status"] == "requested"
    assert runtime.repository.get_run(run["run_id"], org_id=org_a)["status"] == "waiting_approval"
