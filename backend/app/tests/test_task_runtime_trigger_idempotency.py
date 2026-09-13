"""Protective tests for Runtime trigger idempotence (run-now).

Covers AG-G2-AUTO-020: a repeated manual trigger — a double click, a retried HTTP
call, a second tick — never creates a second run for the same task, the protection
is persisted rather than held in this process, and a task that has already settled
is left to the existing retry / rerun semantics instead of being swallowed by the
Runtime branch.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.gateway import task_runtime_optin as optin
from app.gateway.task_runtime_context import build_task_runtime_context

ORG_A = "org-a"
SWITCH = "EVOFLOW_AUTOMATION_UNATTENDED_RUNTIME"
TASK_ID = "e4c1a2b0"


class RecordingContract:
    """Records every Runtime public-boundary call. Never performs I/O."""

    def __init__(self, *, status: Any = None) -> None:
        self.create_calls: list[str | None] = []
        self.status_calls: list[str] = []
        self._n = 0
        self._status = {"status": "pending"} if status is None else status

    async def create_run(
        self,
        *,
        org_id: str,
        task_id: str,
        input_payload: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self.create_calls.append(idempotency_key)
        self._n += 1
        return {"run_id": f"run-{self._n}", "status": "pending", "org_id": org_id}

    async def get_run_status(self, run_id: str) -> dict[str, Any]:
        self.status_calls.append(run_id)
        payload = dict(self._status) if isinstance(self._status, dict) else self._status
        if isinstance(payload, dict):
            payload.setdefault("run_id", run_id)
        return payload


class IdempotentContract(RecordingContract):
    """Honours the idempotency key the way the Runtime's own store does.

    ``RuntimeRepository.create_run`` returns the existing row for the same
    ``(org_id, idempotency_key)``, so two callers that race past the read still
    end up with one run. No process-local lock is involved.
    """

    def __init__(self) -> None:
        super().__init__()
        self._by_key: dict[tuple[str, str], dict[str, Any]] = {}

    async def create_run(
        self,
        *,
        org_id: str,
        task_id: str,
        input_payload: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self.create_calls.append(idempotency_key)
        key = (org_id, idempotency_key or "")
        if idempotency_key and key in self._by_key:
            return self._by_key[key]
        run = {
            "run_id": f"run-{len(self._by_key) + 1}",
            "status": "pending",
            "org_id": org_id,
        }
        if idempotency_key:
            self._by_key[key] = run
        return run


def _identity() -> dict[str, Any]:
    return {
        "org_id": ORG_A,
        "scope_id": "personal:webui:1",
        "principal": {"principal_id": "webui:1", "principal_type": "internal"},
    }


def _task(*, status: str = "inbox", **extra: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": TASK_ID,
        "name": "每周经营简报",
        "run_mode": "unattended",
        "status": status,
        "unattended_attempts": 0,
    }
    task.update(extra)
    return task


def _reload(task: dict[str, Any]) -> dict[str, Any]:
    """Round-trip a task row through the real persistence mapper (a restart)."""
    from evoflow.persistence import task_row_mappers as m

    known, extra_json = m._split_extra(dict(task), m._TASK_KNOWN)
    return m._merge_extra(known, extra_json)


def _ctx(task: dict[str, Any]) -> Any:
    return build_task_runtime_context(task=task, authz=_identity(), authorized=True)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SWITCH, raising=False)


# --- one run per trigger ---------------------------------------------------


async def test_a_double_click_creates_exactly_one_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    contract = RecordingContract()

    first = await optin.establish_runtime_run(
        _task(), identity=_identity(), authorized=True, contract=contract
    )
    second = await optin.establish_runtime_run(
        first.updated_task, identity=_identity(), authorized=True, contract=contract
    )

    assert first.action == "attached"
    assert second.action == "reused"
    assert second.run_id == first.run_id
    assert len(contract.create_calls) == 1, "the second trigger must not create a run"


async def test_a_repeat_after_a_restart_still_reuses_the_same_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    contract = RecordingContract()
    first = await optin.establish_runtime_run(
        _task(), identity=_identity(), authorized=True, contract=contract
    )

    # A new process loads the row from storage: the guard travels with it.
    after_restart = await optin.establish_runtime_run(
        _reload(first.updated_task or {}), identity=_identity(), authorized=True, contract=contract
    )

    assert after_restart.action == "reused"
    assert after_restart.run_id == first.run_id
    assert len(contract.create_calls) == 1


async def test_two_racing_callers_converge_on_one_run_without_a_process_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    contract = IdempotentContract()
    shared = _task()

    first, second = await asyncio.gather(
        optin.establish_runtime_run(
            shared, identity=_identity(), authorized=True, contract=contract
        ),
        optin.establish_runtime_run(
            shared, identity=_identity(), authorized=True, contract=contract
        ),
    )

    assert first.run_id == second.run_id == "run-1"
    # Both reached the Runtime, because nothing in this process serialised them...
    assert len(contract.create_calls) == 2
    # ...and the durable key is what made them converge on one run.
    assert set(contract.create_calls) == {_ctx(shared).idempotency_key}


async def test_the_same_trigger_always_carries_the_same_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    contract = RecordingContract()
    task = _task()

    await optin.establish_runtime_run(
        task, identity=_identity(), authorized=True, contract=contract
    )
    # A second caller that did not see the first write must still produce the same
    # key, because the key is derived from the trusted trigger, not from memory.
    await optin.establish_runtime_run(
        task, identity=_identity(), authorized=True, contract=contract
    )

    assert contract.create_calls == [_ctx(task).idempotency_key] * 2


# --- an existing run is reused, never doubled -----------------------------


async def test_an_active_linked_run_is_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SWITCH, "1")
    contract = RecordingContract(status={"status": "running"})
    first = await optin.establish_runtime_run(
        _task(), identity=_identity(), authorized=True, contract=contract
    )

    again = await optin.establish_runtime_run(
        first.updated_task, identity=_identity(), authorized=True, contract=contract
    )

    assert again.action == "reused"
    assert again.run_status == {"run_id": first.run_id, "status": "running"}
    assert len(contract.create_calls) == 1


async def test_a_terminal_linked_run_is_not_reported_as_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    contract = RecordingContract(status={"status": "completed"})
    first = await optin.establish_runtime_run(
        _task(status="executing"), identity=_identity(), authorized=True, contract=contract
    )

    again = await optin.establish_runtime_run(
        first.updated_task, identity=_identity(), authorized=True, contract=contract
    )

    assert again.action == "reused_terminal_run"
    assert again.reason == "linked_run_terminal"
    assert again.run_id == first.run_id
    assert again.updated_task is None, "a finished run must not be re-linked"
    assert len(contract.create_calls) == 1


@pytest.mark.parametrize("answer", [{}, {"run_id": "run-1"}, "not-a-mapping"])
async def test_an_unreadable_run_status_never_creates_a_second_run(
    monkeypatch: pytest.MonkeyPatch, answer: Any
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    contract = RecordingContract(status=answer)
    first = await optin.establish_runtime_run(
        _task(), identity=_identity(), authorized=True, contract=contract
    )

    again = await optin.establish_runtime_run(
        first.updated_task, identity=_identity(), authorized=True, contract=contract
    )

    assert again.decision == "runtime"
    assert again.action == "reused_unverified"
    assert again.run_id == first.run_id
    assert len(contract.create_calls) == 1, "an unreadable status must not read as 'nothing running'"


# --- a settled task is left to the existing semantics ---------------------


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
async def test_a_terminal_task_defers_to_the_existing_semantics(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    contract = RecordingContract()

    result = await optin.establish_runtime_run(
        _task(status=status), identity=_identity(), authorized=True, contract=contract
    )

    assert result.decision == "legacy", "a settled task must not be claimed by the Runtime branch"
    assert result.reason == "task_already_terminal"
    assert result.action == "deferred_to_existing_semantics"
    optin.assert_no_runtime_side_effects(result)
    assert contract.create_calls == []
    assert contract.status_calls == [], "a settled task costs no Runtime read"


async def test_a_failed_task_is_not_swallowed_so_its_retry_can_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    contract = RecordingContract()
    # A failed task that already carries a linkage: before this card the branch
    # answered "reused" and the pipeline never reached its requeue path.
    linked = await optin.establish_runtime_run(
        _task(), identity=_identity(), authorized=True, contract=RecordingContract()
    )
    failed = dict(linked.updated_task or {})
    failed["status"] = "failed"

    result = await optin.establish_runtime_run(
        failed, identity=_identity(), authorized=True, contract=contract
    )

    assert result.decision == "legacy"
    assert result.reason == "task_already_terminal"
    assert contract.create_calls == []


# --- the guard is durable, not a lock -------------------------------------


def test_no_in_process_lock_is_load_bearing() -> None:
    source = Path("app/gateway/task_runtime_optin.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imported |= {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "threading" not in imported
    assert "asyncio" not in imported, "idempotence must not depend on a process-local primitive"

    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not (called & {"acquire", "release", "Lock"})

    # The durable protection is instead declared on the Runtime contract and
    # always sent on the creation call.
    assert "idempotency_key" in _create_run_parameters(tree)


def _create_run_parameters(tree: ast.Module) -> set[str]:
    protocol = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "RuntimeRunContract"
    )
    method = next(
        node
        for node in protocol.body
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == "create_run"
    )
    return {arg.arg for arg in method.args.args} | {arg.arg for arg in method.args.kwonlyargs}
