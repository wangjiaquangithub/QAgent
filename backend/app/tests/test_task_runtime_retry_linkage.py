"""Protective tests for Runtime linkage across a Task Center retry.

Covers AG-G2-AUTO-025: the existing unattended queue does not *resume* a failed
task, it runs it afresh — ``_advance_unattended_task_impl``'s requeue path raises
``unattended_attempts``, clears the bound plan / subtasks / authorization and
re-queues the task as ``pending``. So a retry is a new attempt, and the Runtime
linkage must follow it to a new run instead of reusing the finished run of the
previous attempt.

The tests pin three things that matter:

- a retry (a new attempt) links to a **new** run, once, with the linkage moved to
  the new attempt rather than the old run being reused;
- a repeat within the **same** attempt still reuses, so the retry handling cannot
  regress the duplicate-trigger guarantee of AG-G2-AUTO-020;
- a stored linkage that is *not* recognisably from an earlier attempt is refused,
  never silently superseded, so a tampered or mis-scoped extras slot cannot move
  a task's run binding by itself.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.gateway import task_runtime_optin as optin
from app.gateway.task_runtime_context import build_task_runtime_context, idempotency_key_for
from app.gateway.task_runtime_linkage import (
    LINKAGE_TASK_KEY,
    RuntimeRunLinkageError,
    read_linked_runtime_run,
)

ORG_A = "org-a"
SWITCH = "EVOFLOW_AUTOMATION_UNATTENDED_RUNTIME"
TASK_ID = "e4c1a2b0"
ORG_SCOPE_KEY = f"tc:org:{ORG_A}"


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


class RefusingCreateContract(RecordingContract):
    """A contract that must never have ``create_run`` called at all."""

    async def create_run(self, **kwargs: Any) -> dict[str, Any]:  # type: ignore[override]
        raise AssertionError("no run may be created for this trigger")


def _identity(org_id: str = ORG_A, owner: str = "webui:1") -> dict[str, Any]:
    return {
        "org_id": org_id,
        "scope_id": f"personal:{owner}",
        "principal": {"principal_id": owner, "principal_type": "internal"},
    }


def _task(*, attempt: int = 0, **extra: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": TASK_ID,
        "name": "每周经营简报",
        "run_mode": "unattended",
        "status": "inbox",
        "unattended_attempts": attempt,
    }
    task.update(extra)
    return task


def _linkage_for(attempt: int, *, run_id: str = "run-old") -> dict[str, str]:
    """A stored linkage exactly as the Runtime-less path would have written it."""
    return {
        "runtime_run_id": run_id,
        "org_scope_key": ORG_SCOPE_KEY,
        "task_id": TASK_ID,
        "idempotency_key": idempotency_key_for(ORG_SCOPE_KEY, TASK_ID, attempt),
    }


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SWITCH, raising=False)


def _establish(task: dict[str, Any], contract: Any) -> Any:
    return asyncio.run(
        optin.establish_runtime_run(
            task, identity=_identity(), authorized=True, contract=contract
        )
    )


def test_a_retry_relinks_to_a_new_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """A new attempt is a new execution, so it gets a new run."""
    monkeypatch.setenv(SWITCH, "1")
    contract = RecordingContract()
    # The previous attempt already had a run; the queue has now bumped the
    # attempt counter and re-queued the task.
    task = _task(attempt=1, **{LINKAGE_TASK_KEY: _linkage_for(0)})

    result = _establish(task, contract)

    assert result.use_runtime
    assert result.reason == "retried"
    assert result.run_id == "run-1"
    assert result.context is not None
    assert contract.create_calls == [result.context.idempotency_key]


def test_a_retry_creates_exactly_one_new_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """The old run is never reused and exactly one new run is created."""
    monkeypatch.setenv(SWITCH, "1")
    contract = RecordingContract()
    task = _task(attempt=1, **{LINKAGE_TASK_KEY: _linkage_for(0)})

    result = _establish(task, contract)

    assert len(contract.create_calls) == 1
    assert contract.status_calls == []  # nothing was read back for reuse
    assert result.run_id != "run-old"


def test_the_linkage_moves_to_the_new_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """The persisted row now pins the new attempt to the new run."""
    monkeypatch.setenv(SWITCH, "1")
    contract = RecordingContract()
    task = _task(attempt=1, **{LINKAGE_TASK_KEY: _linkage_for(0)})

    result = _establish(task, contract)

    assert result.updated_task is not None
    context = build_task_runtime_context(
        task=result.updated_task, authz=_identity(), authorized=True
    )
    stored = read_linked_runtime_run(result.updated_task, context=context)
    assert stored is not None
    assert stored.runtime_run_id == "run-1"
    assert stored.idempotency_key == idempotency_key_for(ORG_SCOPE_KEY, TASK_ID, 1)


def test_a_same_attempt_repeat_still_reuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry handling must not break the duplicate-trigger guarantee."""
    monkeypatch.setenv(SWITCH, "1")
    contract = RecordingContract()
    task = _task(attempt=0, **{LINKAGE_TASK_KEY: _linkage_for(0)})

    result = _establish(task, contract)

    assert result.reason == "reused"
    assert result.run_id == "run-old"
    assert contract.create_calls == []
    assert contract.status_calls == ["run-old"]


def test_a_foreign_linkage_key_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A linkage that is not from any earlier attempt is never superseded."""
    monkeypatch.setenv(SWITCH, "1")
    contract = RefusingCreateContract()
    # Same attempt, but the key does not match this trigger -- a tampered slot.
    task = _task(
        attempt=0,
        **{LINKAGE_TASK_KEY: _linkage_for(0, run_id="run-x") | {"idempotency_key": "tc:bogus"}},
    )

    with pytest.raises(RuntimeRunLinkageError):
        _establish(task, contract)


def test_a_future_attempt_linkage_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A linkage from a *later* attempt than this trigger is refused, not adopted."""
    monkeypatch.setenv(SWITCH, "1")
    contract = RefusingCreateContract()
    task = _task(attempt=1, **{LINKAGE_TASK_KEY: _linkage_for(2)})

    with pytest.raises(RuntimeRunLinkageError):
        _establish(task, contract)


def test_a_linkage_from_an_earlier_attempt_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any earlier attempt is recognisable, not just the immediately preceding one."""
    monkeypatch.setenv(SWITCH, "1")
    contract = RecordingContract()
    task = _task(attempt=4, **{LINKAGE_TASK_KEY: _linkage_for(2)})

    result = _establish(task, contract)

    assert result.reason == "retried"
    assert len(contract.create_calls) == 1


def test_a_retry_never_reads_back_the_old_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """The old run's status is irrelevant to a new attempt."""
    monkeypatch.setenv(SWITCH, "1")
    contract = RecordingContract(status={"status": "failed"})
    task = _task(attempt=1, **{LINKAGE_TASK_KEY: _linkage_for(0)})

    result = _establish(task, contract)

    assert result.reason == "retried"
    assert contract.status_calls == []


def test_a_terminal_task_still_steps_aside(monkeypatch: pytest.MonkeyPatch) -> None:
    """A settled task is left to the existing semantics, retry included."""
    monkeypatch.setenv(SWITCH, "1")
    contract = RefusingCreateContract()
    task = _task(
        attempt=1, status="failed", **{LINKAGE_TASK_KEY: _linkage_for(0)}
    )

    result = _establish(task, contract)

    assert not result.use_runtime
    assert result.reason == "task_already_terminal"


def test_a_first_attempt_still_reports_created(monkeypatch: pytest.MonkeyPatch) -> None:
    """No linkage at all is the ordinary creation path, unchanged."""
    monkeypatch.setenv(SWITCH, "1")
    contract = RecordingContract()
    task = _task(attempt=0)

    result = _establish(task, contract)

    assert result.reason == "created"
    assert len(contract.create_calls) == 1


def test_the_retried_attempt_key_is_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    """The new run is bound to the new attempt's key, not a fresh random one."""
    monkeypatch.setenv(SWITCH, "1")
    task = _task(attempt=1, **{LINKAGE_TASK_KEY: _linkage_for(0)})

    first = _establish(dict(task), RecordingContract())
    second = _establish(dict(task), RecordingContract())

    assert first.context is not None and second.context is not None
    assert first.context.idempotency_key == second.context.idempotency_key
    assert first.context.idempotency_key == idempotency_key_for(ORG_SCOPE_KEY, TASK_ID, 1)
