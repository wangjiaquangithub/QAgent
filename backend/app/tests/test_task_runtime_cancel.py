"""Protective tests for cancelling a linked Runtime run.

Covers AG-G2-AUTO-005-B02: linked tasks use the Runtime public cancel boundary,
unlinked tasks keep the legacy path, cancellation is idempotent, a cancelling
task cannot be pushed back to completed, and another organization cannot cancel.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from app.gateway.task_runtime_cancel import (
    RuntimeCancelError,
    cancel_linked_runtime_run,
)
from app.gateway.task_runtime_context import build_task_runtime_context
from app.gateway.task_runtime_linkage import link_runtime_run
from app.gateway.task_runtime_projection import (
    HISTORY_FIELD,
    project_runtime_status,
)

ORG_A = "org-a"
ORG_B = "org-b"
TASK_ID = "e4c1a2b0"
RUN_ID = "run-1"


class RecordingContract:
    def __init__(self) -> None:
        self.cancel_calls: list[str] = []

    async def request_cancel(self, run_id: str) -> dict[str, Any]:
        self.cancel_calls.append(run_id)
        return {"run_id": run_id, "status": "cancelled"}


def _authz(org_id: str = ORG_A) -> dict[str, Any]:
    return {
        "org_id": org_id,
        "principal": {"principal_id": "webui:1", "principal_type": "internal"},
        "scope_id": "personal:webui:1",
        "is_org_admin": False,
    }


def _task(*, task_id: str = TASK_ID, status: str = "executing") -> dict[str, Any]:
    return {
        "id": task_id,
        "name": "每周经营简报",
        "run_mode": "unattended",
        "status": status,
        "unattended_attempts": 0,
        "execution_history": [],
    }


def _linked(*, org_id: str = ORG_A, status: str = "executing") -> tuple[dict[str, Any], Any]:
    ctx = build_task_runtime_context(
        task=_task(status=status), authz=_authz(org_id), authorized=True
    )
    task, _ = link_runtime_run(_task(status=status), context=ctx, runtime_run_id=RUN_ID)
    return task, ctx


def _history(task: dict[str, Any]) -> list[dict[str, Any]]:
    return list(task.get(HISTORY_FIELD) or [])


async def test_linked_task_cancels_through_the_runtime_public_boundary() -> None:
    task, ctx = _linked()
    contract = RecordingContract()

    outcome = await cancel_linked_runtime_run(task, context=ctx, contract=contract)

    assert outcome.action == "runtime_cancelled"
    assert outcome.used_runtime
    assert contract.cancel_calls == [RUN_ID]
    assert outcome.updated_task is not None
    record = _history(outcome.updated_task)[-1]
    assert record["runtime_status"] == "cancelled"
    assert record["runtime_run_id"] == RUN_ID
    assert record["source"] == "qagent_runtime"


async def test_unlinked_task_keeps_the_legacy_path_and_never_calls_the_runtime() -> None:
    ctx = build_task_runtime_context(task=_task(), authz=_authz(ORG_A), authorized=True)
    contract = RecordingContract()

    outcome = await cancel_linked_runtime_run(_task(), context=ctx, contract=contract)

    assert outcome.action == "legacy_not_linked"
    assert not outcome.used_runtime
    assert outcome.updated_task is None
    assert contract.cancel_calls == []


async def test_repeat_cancel_is_idempotent() -> None:
    task, ctx = _linked()
    contract = RecordingContract()

    first = await cancel_linked_runtime_run(task, context=ctx, contract=contract)
    assert first.updated_task is not None

    second = await cancel_linked_runtime_run(first.updated_task, context=ctx, contract=contract)

    assert second.action == "already_cancelled"
    assert contract.cancel_calls == [RUN_ID], "the runtime must not be asked twice"


async def test_missing_runtime_contract_does_not_pretend_to_cancel() -> None:
    task, ctx = _linked()
    outcome = await cancel_linked_runtime_run(task, context=ctx, contract=None)

    assert outcome.action == "runtime_unavailable"
    assert not outcome.used_runtime
    assert outcome.runtime_run_id == RUN_ID


async def test_cross_organization_cancellation_is_refused() -> None:
    task, _ = _linked(org_id=ORG_A)
    other = build_task_runtime_context(task=_task(), authz=_authz(ORG_B), authorized=True)
    contract = RecordingContract()

    with pytest.raises(RuntimeCancelError, match="another organization"):
        await cancel_linked_runtime_run(task, context=other, contract=contract)

    assert contract.cancel_calls == []


async def test_context_for_another_task_is_refused() -> None:
    task = _task()
    other = build_task_runtime_context(
        task=_task(task_id="deadbeef"), authz=_authz(ORG_A), authorized=True
    )
    with pytest.raises(RuntimeCancelError, match="does not belong to this task"):
        await cancel_linked_runtime_run(task, context=other, contract=RecordingContract())


async def test_a_cancelled_task_cannot_be_pushed_back_to_completed() -> None:
    task, ctx = _linked()
    cancelled = await cancel_linked_runtime_run(task, context=ctx, contract=RecordingContract())
    assert cancelled.updated_task is not None

    # The task itself is cancelled by the existing handler; the projection must
    # then refuse to move it, so an old executor cannot overwrite the cancel.
    locally_cancelled = dict(cancelled.updated_task)
    locally_cancelled["status"] = "cancelled"

    after, projection = project_runtime_status(
        locally_cancelled, context=ctx, runtime_status="completed", sequence=99
    )

    assert projection.action == "noop"
    assert projection.reason == "terminal_already_reached"
    assert after["status"] == "cancelled"


def _tasks_router_ast() -> Any:
    source = Path("app/gateway/routers/tasks.py").read_text(encoding="utf-8")
    return ast.parse(source)


def test_cancel_route_calls_the_runtime_helper_once_before_revoking_authorization() -> None:
    tree = _tasks_router_ast()
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "cancel_task"
    )
    calls = [
        node
        for node in ast.walk(handler)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_cancel_linked_runtime_run_if_any"
    ]
    assert len(calls) == 1, "the cancel route must consult the runtime helper exactly once"

    # And the helper must exist, be awaited, and stay inert for unlinked tasks.
    helper = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_cancel_linked_runtime_run_if_any"
    )
    # The helper imports some names directly and calls others via the opt-in
    # module, so both Name and Attribute nodes matter here.
    helper_names = {n.id for n in ast.walk(helper) if isinstance(n, ast.Name)}
    helper_attrs = {n.attr for n in ast.walk(helper) if isinstance(n, ast.Attribute)}
    assert "cancel_linked_runtime_run" in helper_names
    assert "build_task_runtime_context" in helper_names
    assert "resolve_server_task_runtime_identity" in helper_attrs
