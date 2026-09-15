"""Focused acceptance tests for the unattended Runtime execution drive.

The fake below is a public-contract stand-in. It models the Runtime's existing
queued -> start -> approval -> terminal flow, so these tests cover the missing
drive without credentials or a paid Provider call.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from _runtime_flow_support import (
    authorize_via_route,
    build_client,
    create_unattended_task,
    reset_home,
    start_via_route,
    stored_task,
)

from app.gateway import task_runtime_optin as optin
from app.gateway import unattended_task_pipeline as pipeline

SWITCH = "EVOFLOW_AUTOMATION_UNATTENDED_RUNTIME"
ORG = "local"
AUTOMATION_ORG = "org-automation"


class DriveRuntime:
    """A no-I/O Runtime fake with the real public execution shape."""

    def __init__(self, mode: str = "completed") -> None:
        self.mode = mode
        self.create_calls: list[dict[str, Any]] = []
        self.start_calls: list[dict[str, Any]] = []
        self.approval_calls: list[dict[str, Any]] = []
        self.status_calls: list[dict[str, Any]] = []
        self.provider_calls = 0
        self._runs: dict[str, dict[str, Any]] = {}

    async def create_run(
        self,
        *,
        org_id: str,
        task_id: str,
        input_payload: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self.create_calls.append(
            {
                "org_id": org_id,
                "task_id": task_id,
                "input_payload": dict(input_payload),
                "idempotency_key": idempotency_key,
            }
        )
        run_id = f"run-drive-{len(self._runs) + 1}"
        self._runs[run_id] = {
            "run_id": run_id,
            "org_id": org_id,
            "task_id": task_id,
            "status": "queued",
        }
        return dict(self._runs[run_id])

    async def start_run(self, run_id: str, *, org_id: str) -> dict[str, Any]:
        self.start_calls.append({"run_id": run_id, "org_id": org_id})
        run = self._runs[run_id]
        if self.mode == "start_failure":
            run.update(
                status="failed",
                error={"code": "runtime_start_failed", "message": "Runtime start failed"},
            )
            return dict(run)
        if self.mode in {"completed", "provider_failure"}:
            run.update(
                status="waiting_approval",
                approval={"approval_id": f"approval-{run_id}"},
            )
        else:
            run["status"] = self.mode
        return dict(run)

    async def grant_approval(
        self,
        approval_id: str,
        *,
        decided_by: str | None = None,
        reason: str | None = None,
        org_id: str,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        self.approval_calls.append(
            {
                "approval_id": approval_id,
                "decided_by": decided_by,
                "reason": reason,
                "org_id": org_id,
                "run_id": run_id,
            }
        )
        assert run_id is not None
        run = self._runs[run_id]
        self.provider_calls += 1
        if self.mode == "provider_failure":
            run.update(
                status="failed",
                error={"code": "provider_error", "message": "Provider failed"},
            )
        else:
            run.update(status="completed", result={"answer": "offline"})
        return dict(run)

    async def get_run_status(self, run_id: str, *, org_id: str) -> dict[str, Any]:
        self.status_calls.append({"run_id": run_id, "org_id": org_id})
        return dict(self._runs[run_id])

    async def request_cancel(self, run_id: str, *, org_id: str) -> dict[str, Any]:
        run = self._runs[run_id]
        run["status"] = "cancelled"
        return dict(run) | {"org_id": org_id}


@pytest.fixture()
def client():
    reset_home()
    return build_client()


@pytest.fixture(autouse=True)
def _runtime_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SWITCH, "1")


def _owned_task(client, monkeypatch: pytest.MonkeyPatch, runtime: DriveRuntime) -> str:
    monkeypatch.setattr(optin, "default_runtime_contract", lambda: runtime)
    task_id = create_unattended_task(client)
    from evoflow.persistence.task_repositories import set_root_task_owner_scope

    set_root_task_owner_scope(
        task_id,
        org_id=ORG,
        owner_scope_id="personal:automation-owner",
        created_by="automation-owner",
    )
    authorize_via_route(client, task_id)
    return task_id


def test_unattended_start_drives_runtime_and_projects_completion(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = DriveRuntime()
    task_id = _owned_task(client, monkeypatch, runtime)

    body = start_via_route(client, task_id)
    stored = stored_task(task_id)

    assert body["advance"]["action"] == "runtime"
    assert len(runtime.create_calls) == 1
    assert len(runtime.start_calls) == 1
    assert len(runtime.approval_calls) == 1
    assert runtime.approval_calls[0]["org_id"] == ORG
    assert runtime.approval_calls[0]["run_id"] == runtime.start_calls[0]["run_id"]
    assert runtime.provider_calls == 1
    assert stored["status"] == "completed"
    assert stored["execution_history"][-1]["runtime_status"] == "completed"


def test_same_task_run_is_not_created_or_started_twice(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = DriveRuntime(mode="running")
    task_id = _owned_task(client, monkeypatch, runtime)

    start_via_route(client, task_id)
    start_via_route(client, task_id)

    assert len(runtime.create_calls) == 1
    assert len(runtime.start_calls) == 1
    assert all(call["org_id"] == ORG for call in runtime.start_calls)


@pytest.mark.parametrize("mode", ["start_failure", "provider_failure"])
def test_runtime_start_or_provider_failure_is_projected_as_failed_without_legacy_dispatch(
    client, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    runtime = DriveRuntime(mode=mode)
    task_id = _owned_task(client, monkeypatch, runtime)
    legacy_calls: list[str] = []

    async def _legacy_must_not_run(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        legacy_calls.append("legacy")
        raise AssertionError("Runtime failure must not fall back to the legacy executor")

    monkeypatch.setattr(pipeline, "dispatch_authorized_main_task_execution", _legacy_must_not_run)

    start_via_route(client, task_id)
    stored = stored_task(task_id)

    assert stored["status"] == "failed"
    assert stored["execution_history"][-1]["runtime_status"] == "failed"
    assert legacy_calls == []


def test_create_start_query_and_projection_keep_one_trusted_org(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = DriveRuntime()
    task_id = _owned_task(client, monkeypatch, runtime)

    start_via_route(client, task_id)
    stored = stored_task(task_id)

    assert runtime.create_calls[0]["org_id"] == ORG
    assert runtime.start_calls[0]["org_id"] == ORG
    assert runtime.status_calls
    assert {call["org_id"] for call in runtime.status_calls} == {ORG}
    assert stored["execution_history"][-1]["org_scope_key"] == f"tc:org:{ORG}"


def test_terminal_cancelled_runtime_state_is_still_projected(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = DriveRuntime(mode="cancelled")
    task_id = _owned_task(client, monkeypatch, runtime)

    start_via_route(client, task_id)
    stored = stored_task(task_id)

    assert stored["status"] == "cancelled"
    assert stored["execution_history"][-1]["runtime_status"] == "cancelled"


def test_runtime_switch_off_performs_zero_runtime_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = DriveRuntime()
    monkeypatch.setattr(optin, "default_runtime_contract", lambda: runtime)
    monkeypatch.delenv(SWITCH, raising=False)

    result = asyncio.run(pipeline._maybe_run_via_runtime("task-off", {"id": "task-off"}))

    assert result is None
    assert runtime.create_calls == []
    assert runtime.start_calls == []
    assert runtime.approval_calls == []


def test_real_automation_owner_scope_is_copied_to_the_created_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.gateway.automation_runner import _enqueue_automation_as_unattended_task
    from evoflow.persistence import automation_repositories
    from evoflow.persistence.task_repositories import get_root_task_owner_scope

    reset_home()
    automation_repositories.save_automation(
        "auto-owned",
        {"name": "日报", "prompt": "生成日报", "schedule": "0 9 * * *", "status": "active"},
    )
    automation_repositories.set_automation_owner_scope(
        "auto-owned",
        org_id=AUTOMATION_ORG,
        owner_scope_id="personal:automation-owner",
        created_by="automation-owner",
    )

    result = _enqueue_automation_as_unattended_task(
        "auto-owned",
        {"name": "日报", "prompt": "生成日报"},
        run_id="automation-run-1",
        trigger_type="schedule",
        prompt="生成日报",
    )
    task_id = str(result["collab_task_id"])

    assert get_root_task_owner_scope(task_id) == (
        AUTOMATION_ORG,
        "personal:automation-owner",
        "automation-owner",
    )

    runtime = DriveRuntime()
    monkeypatch.setattr(optin, "default_runtime_contract", lambda: runtime)
    advance = asyncio.run(pipeline.advance_unattended_task(task_id))
    stored = stored_task(task_id)

    assert advance["action"] == "runtime"
    assert runtime.create_calls[0]["org_id"] == AUTOMATION_ORG
    assert runtime.start_calls[0]["org_id"] == AUTOMATION_ORG
    assert runtime.approval_calls[0]["org_id"] == AUTOMATION_ORG
    assert stored["status"] == "completed"
    assert stored["execution_history"][-1]["org_scope_key"] == f"tc:org:{AUTOMATION_ORG}"
