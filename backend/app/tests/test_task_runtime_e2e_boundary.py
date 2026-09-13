"""End-to-end boundary test for the implemented Task Center → Runtime slice.

AG-G2-AUTO-006-B02.

Everything runs against an in-process fake Runtime contract: no network, no API
key, no provider call and no model cost. The test walks the implemented loop

    unattended task -> opt-in -> runtime run + linkage
                    -> runtime event -> task status + execution_history
                    -> cancel -> runtime public cancel

and asserts the boundaries that must hold at every step. The real Runtime
service is deliberately never constructed, so the test cannot reach PostgreSQL,
a provider or a credential even by accident.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.gateway import task_runtime_optin as optin
from app.gateway.task_runtime_cancel import RuntimeCancelError, cancel_linked_runtime_run
from app.gateway.task_runtime_context import build_task_runtime_context
from app.gateway.task_runtime_event_bridge import RuntimeEventBridgeError, apply_runtime_event
from app.gateway.task_runtime_linkage import LINKAGE_TASK_KEY
from app.gateway.task_runtime_projection import HISTORY_FIELD, project_runtime_status

SWITCH = "EVOFLOW_AUTOMATION_UNATTENDED_RUNTIME"
ORG_A = "org-a"
ORG_B = "org-b"
TASK_ID = "e4c1a2b0"
RUN_ID = "run-1"


class FakeRuntimeContract:
    """A local stand-in for the Runtime public contract. Never performs I/O."""

    def __init__(self) -> None:
        self.create_calls: list[dict[str, Any]] = []
        self.status_calls: list[str] = []
        self.cancel_calls: list[str] = []
        self._runs: dict[str, dict[str, Any]] = {}

    async def create_run(
        self, *, task_id: str, input_payload: dict[str, Any], idempotency_key: str | None = None
    ) -> dict[str, Any]:
        self.create_calls.append(
            {"task_id": task_id, "input_payload": input_payload, "idempotency_key": idempotency_key}
        )
        run_id = f"run-{len(self.create_calls)}"
        self._runs[run_id] = {"run_id": run_id, "status": "created", "task_id": task_id}
        return dict(self._runs[run_id])

    async def get_run_status(self, run_id: str) -> dict[str, Any]:
        self.status_calls.append(run_id)
        return dict(self._runs.get(run_id, {"run_id": run_id, "status": "unknown"}))

    async def request_cancel(self, run_id: str) -> dict[str, Any]:
        self.cancel_calls.append(run_id)
        self._runs.setdefault(run_id, {"run_id": run_id})
        self._runs[run_id]["status"] = "cancelled"
        return dict(self._runs[run_id])


def _identity(org_id: str = ORG_A) -> dict[str, Any]:
    return {
        "org_id": org_id,
        "scope_id": "personal:webui:1",
        "principal": {"principal_id": "webui:1", "principal_type": "internal"},
    }


def _task(*, status: str = "inbox") -> dict[str, Any]:
    return {
        "id": TASK_ID,
        "name": "每周经营简报",
        "description": "汇总区域销量并给出异常清单",
        "run_mode": "unattended",
        "status": status,
        "unattended_attempts": 0,
        "execution_history": [],
    }


def _frame(
    event_type: str, *, event_id: str, sequence: int, run_id: str = RUN_ID, payload: Any = None
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "run_id": run_id,
        "sequence": sequence,
        "occurred_at": "2026-09-14T00:00:00Z",
        "type": event_type,
        "payload": {} if payload is None else payload,
    }


def _history(task: dict[str, Any]) -> list[dict[str, Any]]:
    return list(task.get(HISTORY_FIELD) or [])


async def _linked_task(
    contract: FakeRuntimeContract, *, status: str = "inbox", org_id: str = ORG_A
) -> tuple[dict[str, Any], Any]:
    linked = await optin.establish_runtime_run(
        _task(status=status), identity=_identity(org_id), authorized=True, contract=contract
    )
    assert linked.use_runtime and linked.updated_task is not None
    task = linked.updated_task
    context = build_task_runtime_context(task=task, authz=_identity(org_id), authorized=True)
    return task, context


# --- offline guarantees ----------------------------------------------------


async def test_switch_off_is_offline_and_never_resolves_a_runtime_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> Any:
        raise AssertionError("the real runtime service must never be constructed with the switch off")

    monkeypatch.delenv(SWITCH, raising=False)
    monkeypatch.setattr(optin, "default_runtime_contract", _boom)
    contract = FakeRuntimeContract()

    result = await optin.establish_runtime_run(
        _task(), identity=_identity(), authorized=True, contract=contract
    )

    assert result.decision == "legacy"
    assert contract.create_calls == [] and contract.status_calls == [] and contract.cancel_calls == []


@pytest.mark.parametrize("switch", [None, "0", "false", "off"])
async def test_every_non_enabling_switch_value_keeps_the_legacy_path(
    monkeypatch: pytest.MonkeyPatch, switch: str | None
) -> None:
    if switch is None:
        monkeypatch.delenv(SWITCH, raising=False)
    else:
        monkeypatch.setenv(SWITCH, switch)
    contract = FakeRuntimeContract()

    result = await optin.establish_runtime_run(
        _task(), identity=_identity(), authorized=True, contract=contract
    )

    assert result.decision == "legacy"
    assert contract.create_calls == []


# --- the implemented loop --------------------------------------------------


async def test_full_loop_from_opt_in_to_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SWITCH, "1")
    contract = FakeRuntimeContract()

    # 1. opt in: exactly one run is created and linked to the task
    task, ctx = await _linked_task(contract)
    assert len(contract.create_calls) == 1
    assert task[LINKAGE_TASK_KEY]["runtime_run_id"] == RUN_ID

    # 2. a duplicate trigger reuses the same run id, never a second run
    again = await optin.establish_runtime_run(
        task, identity=_identity(), authorized=True, contract=contract
    )
    assert again.action == "reused"
    assert again.run_id == RUN_ID
    assert len(contract.create_calls) == 1, "repeat trigger must reuse the same runtime_run_id"

    # 3. waiting_approval: the task status is untouched, the history shows it
    waiting, waiting_outcome = apply_runtime_event(
        task, context=ctx, event=_frame("run.waiting_approval", event_id="ev-1", sequence=1)
    )
    assert waiting_outcome.action == "history_only"
    assert waiting["status"] == "inbox"
    assert _history(waiting)[-1]["runtime_status"] == "waiting_approval"
    assert _history(waiting)[-1]["approval_required"] is True

    # 4. running, then completed
    running, _ = apply_runtime_event(
        waiting, context=ctx, event=_frame("run.running", event_id="ev-2", sequence=2)
    )
    assert running["status"] == "executing"

    done, _ = apply_runtime_event(
        running, context=ctx, event=_frame("run.completed", event_id="ev-3", sequence=3)
    )
    assert done["status"] == "completed"

    # 5. a late, older frame cannot move it back
    after, stale = apply_runtime_event(
        done, context=ctx, event=_frame("run.running", event_id="ev-9", sequence=9)
    )
    assert stale.action == "stale"
    assert after["status"] == "completed"


async def test_failed_outcome_stops_at_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SWITCH, "1")
    contract = FakeRuntimeContract()
    task, ctx = await _linked_task(contract, status="executing")

    updated, outcome = apply_runtime_event(
        task,
        context=ctx,
        event=_frame(
            "run.failed",
            event_id="ev-1",
            sequence=4,
            payload={"error": {"code": "provider.error", "message": "upstream failed"}},
        ),
    )

    assert outcome.action == "status_updated"
    assert updated["status"] == "failed"
    assert _history(updated)[-1]["error_code"] == "provider.error"


async def test_timed_out_ends_as_failed_with_the_runtime_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    contract = FakeRuntimeContract()
    task, ctx = await _linked_task(contract, status="executing")

    updated, outcome = project_runtime_status(
        task,
        context=ctx,
        runtime_status="timed_out",
        sequence=5,
        error_code="run.timeout",
        reason="exceeded budget",
    )

    assert outcome.action == "status_updated"
    assert updated["status"] == "failed"
    record = _history(updated)[-1]
    assert record["runtime_status"] == "timed_out"
    assert record["error_code"] == "run.timeout"
    assert record["reason"] == "exceeded budget"


async def test_cancel_uses_the_runtime_boundary_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    contract = FakeRuntimeContract()
    task, ctx = await _linked_task(contract, status="executing")

    first = await cancel_linked_runtime_run(task, context=ctx, contract=contract)
    assert first.action == "runtime_cancelled"
    assert contract.cancel_calls == [RUN_ID]
    assert first.updated_task is not None

    second = await cancel_linked_runtime_run(first.updated_task, context=ctx, contract=contract)
    assert second.action == "already_cancelled"
    assert contract.cancel_calls == [RUN_ID]

    # A cancelled task can never be moved back to completed by a late frame.
    locally_cancelled = dict(first.updated_task)
    locally_cancelled["status"] = "cancelled"
    after, projection = project_runtime_status(
        locally_cancelled, context=ctx, runtime_status="completed", sequence=99
    )
    assert projection.action == "noop"
    assert after["status"] == "cancelled"


# --- isolation -------------------------------------------------------------


async def test_another_organization_cannot_read_project_or_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    contract = FakeRuntimeContract()
    task, _ = await _linked_task(contract, status="executing", org_id=ORG_A)
    other = build_task_runtime_context(
        task=_task(status="executing"), authz=_identity(ORG_B), authorized=True
    )

    with pytest.raises(RuntimeEventBridgeError, match="another organization"):
        apply_runtime_event(
            task, context=other, event=_frame("run.completed", event_id="ev-x", sequence=1)
        )
    with pytest.raises(RuntimeCancelError, match="another organization"):
        await cancel_linked_runtime_run(task, context=other, contract=contract)

    assert contract.cancel_calls == []


async def test_frames_for_another_run_cannot_be_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SWITCH, "1")
    contract = FakeRuntimeContract()
    task, ctx = await _linked_task(contract, status="executing")

    # The trusted run id comes from the stored linkage, so a frame naming a
    # different run is still applied to this task's own run - never a foreign one.
    updated, outcome = apply_runtime_event(
        task, context=ctx, event=_frame("run.running", event_id="ev-1", sequence=1, run_id="run-999")
    )
    assert outcome.action == "history_only"
    assert _history(updated)[-1]["runtime_run_id"] == RUN_ID


def test_the_slice_adds_no_task_status_values() -> None:
    from evoflow.collab.models import TaskStatus

    assert {member.value for member in TaskStatus} == {
        "inbox",
        "pending",
        "planning",
        "planned",
        "executing",
        "paused",
        "reviewed",
        "completed",
        "failed",
        "cancelled",
    }
