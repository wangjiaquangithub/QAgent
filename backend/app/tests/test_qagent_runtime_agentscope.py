from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import StaticPool

from app.qagent_runtime.agentscope_adapter import AgentScopeAdapter
from app.qagent_runtime.events import EventType
from app.qagent_runtime.models import qagent_runs
from app.qagent_runtime.repository import RuntimeRepository
from app.qagent_runtime.service import RuntimeService


class RuntimeFakeChatModel:
    """Offline AgentScope ChatModelBase fake for the QAgent runtime schema."""

    def __init__(self, mode: str = "success") -> None:
        from agentscope.formatter import OpenAIChatFormatter
        from agentscope.model import ChatModelBase
        from pydantic import BaseModel

        class Parameters(BaseModel):
            temperature: float = 0.0

        class Model(ChatModelBase):
            def __init__(self, selected_mode: str) -> None:
                super().__init__(
                    credential=None,
                    model="offline-qagent-runtime",
                    parameters=Parameters(),
                    stream=False,
                    max_retries=0,
                )
                self.formatter = OpenAIChatFormatter()
                self.selected_mode = selected_mode
                self.calls = 0

            async def _call_api(
                self,
                model_name: str,
                messages: list[dict[str, Any]],
                tools: list[dict[str, Any]] | None = None,
                tool_choice: Any = None,
                **kwargs: Any,
            ) -> Any:
                del model_name, messages, tools, tool_choice, kwargs
                self.calls += 1
                from agentscope.message import ToolCallBlock
                from agentscope.model import ChatResponse

                if self.selected_mode == "error":
                    raise RuntimeError("offline provider secret must not leak")
                return ChatResponse(
                    content=[
                        ToolCallBlock(
                            id="runtime-result-1",
                            name="GenerateStructuredOutput",
                            input=(
                                '{"result":{"answer":"offline-ok"},'
                                '"assets":[{"asset_id":"asset-runtime-1",'
                                '"asset_type":"report","uri":"qagent://runtime/report",'
                                '"content_type":"text/plain"}]}'
                            ),
                        )
                    ],
                    is_last=True,
                )

        self.instance = Model(mode)


@pytest.fixture
def runtime_repository() -> RuntimeRepository:
    engine = sa.create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    repository = RuntimeRepository(engine, create_schema=True)
    yield repository
    engine.dispose()


def make_service(repository: RuntimeRepository, fake: RuntimeFakeChatModel) -> RuntimeService:
    adapter = AgentScopeAdapter(model=fake.instance)
    from app.qagent_runtime.executor import ServerSubtaskExecutor

    return RuntimeService(repository, planner=adapter, executor=ServerSubtaskExecutor(adapter))


@pytest.mark.asyncio
async def test_approved_run_executes_real_agentscope_and_persists_runtime_outputs(
    runtime_repository: RuntimeRepository,
) -> None:
    fake = RuntimeFakeChatModel()
    service = make_service(runtime_repository, fake)

    run = await service.create_run(org_id="org-a", task_id="task-1", input_payload={"prompt": "hello"})
    waiting = await service.start_run(run["run_id"], org_id="org-a")
    completed = await service.grant_approval(
        waiting["approval"]["approval_id"],
        org_id="org-a",
        decided_by="operator",
    )

    assert completed["status"] == "completed"
    assert fake.instance.calls == 1
    assert (await service.get_result(run["run_id"], org_id="org-a"))["result"] == {"answer": "offline-ok"}
    assert runtime_repository.list_assets(run["run_id"], org_id="org-a")[0]["uri"] == "qagent://runtime/report"
    assert [event["type"] for event in runtime_repository.list_events(run["run_id"], org_id="org-a")] == [
        EventType.RUN_CREATED,
        EventType.RUN_QUEUED,
        EventType.RUN_PLANNING,
        EventType.RUN_WAITING_APPROVAL,
        EventType.APPROVAL_GRANTED,
        EventType.RUN_RUNNING,
        EventType.RUN_EXECUTING,
        EventType.ASSET_AVAILABLE,
        EventType.RUN_COMPLETED,
    ]
    assert [point["checkpoint_key"] for point in runtime_repository.list_recovery_points(run["run_id"], org_id="org-a")] == [
        "planning-complete",
        "execution-started",
        "execution-completed",
    ]

    await service.grant_approval(waiting["approval"]["approval_id"], org_id="org-a")
    await service.resume_run(run["run_id"], org_id="org-a")
    assert fake.instance.calls == 1


@pytest.mark.asyncio
async def test_agentscope_failure_is_sanitized_and_has_stable_events(
    runtime_repository: RuntimeRepository,
) -> None:
    fake = RuntimeFakeChatModel("error")
    service = make_service(runtime_repository, fake)
    run = await service.create_run(org_id="org-a", task_id="task-2", input_payload={})
    waiting = await service.start_run(run["run_id"], org_id="org-a")
    failed = await service.grant_approval(waiting["approval"]["approval_id"], org_id="org-a")

    assert failed["status"] == "failed"
    result = await service.get_result(run["run_id"], org_id="org-a")
    assert result["error"] == {"code": "agentscope_execution", "message": "AgentScope execution failed"}
    event_types = [event["type"] for event in runtime_repository.list_events(run["run_id"], org_id="org-a")]
    assert event_types[-3:] == [EventType.RUN_RUNNING, EventType.RUN_EXECUTING, EventType.RUN_FAILED]
    assert "secret" not in str(result["error"])
    assert [point["checkpoint_key"] for point in runtime_repository.list_recovery_points(run["run_id"], org_id="org-a")] == [
        "planning-complete",
        "execution-started",
        "execution-failed",
    ]


@pytest.mark.asyncio
async def test_recovery_reclaims_only_expired_execution_claim_and_preserves_org_isolation(
    runtime_repository: RuntimeRepository,
) -> None:
    fake = RuntimeFakeChatModel()
    service = make_service(runtime_repository, fake)
    run = await service.create_run(org_id="org-a", task_id="task-3", input_payload={})
    waiting = await service.start_run(run["run_id"], org_id="org-a")
    approval_id = waiting["approval"]["approval_id"]
    approval, changed = runtime_repository.decide_approval_atomically(approval_id, status="granted", decided_by=None, reason=None, org_id="org-a", run_id=run["run_id"])
    assert approval and changed
    claim = runtime_repository.claim_execution(run["run_id"], org_id="org-a", emit_event=True)
    assert claim
    with runtime_repository.engine.begin() as conn:
        conn.execute(
            qagent_runs.update()
            .where(qagent_runs.c.run_id == run["run_id"])
            .values(execution_claimed_at=datetime.now(UTC) - timedelta(seconds=60))
        )

    recovered_service = make_service(runtime_repository, fake)
    recovered_service.execution_claim_timeout_seconds = 1
    recovered = await recovered_service.resume_run(run["run_id"], org_id="org-a")
    assert recovered["status"] == "completed"
    assert fake.instance.calls == 1
    assert runtime_repository.get_run(run["run_id"], org_id="org-b") is None
    assert runtime_repository.list_events(run["run_id"], org_id="org-b") == []
    assert runtime_repository.list_assets(run["run_id"], org_id="org-b") == []
    assert runtime_repository.list_recovery_points(run["run_id"], org_id="org-b") == []
