"""Protective tests for idempotent Runtime -> Task re-projection.

Covers AG-G2-AUTO-012: a linked task whose projection lagged (process restart,
dropped connection) is repaired from the Runtime's own state, without executing a
provider, without creating a run, and without duplicating history.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from app.gateway.task_runtime_context import build_task_runtime_context
from app.gateway.task_runtime_linkage import LINKAGE_TASK_KEY, link_runtime_run
from app.gateway.task_runtime_projection import HISTORY_FIELD
from app.gateway.task_runtime_reconcile import reconcile_linked_task_runtime

ORG_A = "org-a"
ORG_B = "org-b"
TASK_ID = "e4c1a2b0"
RUN_ID = "run-1"
SWITCH = "EVOFLOW_AUTOMATION_UNATTENDED_RUNTIME"


class RecordingContract:
    """Records every Runtime public-boundary call. Never performs I/O."""

    def __init__(self, *, status: Any = None) -> None:
        self.status_calls: list[str] = []
        self.create_calls: list[str] = []
        self.cancel_calls: list[str] = []
        self._status = {"run_id": RUN_ID, "status": "executing"} if status is None else status

    async def get_run_status(self, run_id: str, *, org_id: str | None = None) -> dict[str, Any]:
        self.status_calls.append(run_id)
        return self._status

    async def create_run(self, **kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        self.create_calls.append("called")
        raise AssertionError("reconciliation must never create a run")

    async def request_cancel(self, run_id: str, *, org_id: str | None = None) -> dict[str, Any]:  # pragma: no cover
        self.cancel_calls.append(run_id)
        raise AssertionError("reconciliation must never cancel a run")


def _authz(org_id: str = ORG_A) -> dict[str, Any]:
    return {
        "org_id": org_id,
        "principal": {"principal_id": "webui:1", "principal_type": "internal"},
        "scope_id": "personal:webui:1",
        "is_org_admin": False,
    }


def _task(*, status: str = "pending", **extra: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": TASK_ID,
        "name": "每周经营简报",
        "run_mode": "unattended",
        "status": status,
        "unattended_attempts": 0,
        "execution_history": [],
    }
    task.update(extra)
    return task


def _ctx(org_id: str = ORG_A, *, status: str = "pending", task_id: str = TASK_ID):
    return build_task_runtime_context(
        task=_task(status=status, id=task_id),
        authz=_authz(org_id),
        authorized=True,
    )


def _linked(*, status: str = "pending", org_id: str = ORG_A) -> tuple[dict[str, Any], Any]:
    ctx = _ctx(org_id, status=status)
    task, _ = link_runtime_run(_task(status=status), context=ctx, runtime_run_id=RUN_ID)
    return task, ctx


def _history(task: dict[str, Any]) -> list[dict[str, Any]]:
    return list(task.get(HISTORY_FIELD) or [])


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SWITCH, raising=False)


# --- inert paths -----------------------------------------------------------


async def test_switch_off_is_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    task, ctx = _linked()
    contract = RecordingContract()

    result = await reconcile_linked_task_runtime(task, context=ctx, contract=contract)

    assert result.action == "disabled"
    assert result.updated_task is None
    assert contract.status_calls == [] and contract.create_calls == []


async def test_an_unlinked_task_is_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SWITCH, "1")
    contract = RecordingContract()

    result = await reconcile_linked_task_runtime(_task(), context=_ctx(), contract=contract)

    assert result.action == "not_linked"
    assert result.updated_task is None
    assert contract.status_calls == []


async def test_a_terminal_task_is_not_re_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SWITCH, "1")
    task, ctx = _linked(status="completed")
    contract = RecordingContract()

    result = await reconcile_linked_task_runtime(task, context=ctx, contract=contract)

    assert result.action == "terminal"
    assert result.updated_task is None
    assert contract.status_calls == [], "a settled task needs no Runtime read"


async def test_a_missing_contract_is_reported_not_faked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SWITCH, "1")
    task, ctx = _linked()

    result = await reconcile_linked_task_runtime(task, context=ctx, contract=None)

    assert result.action == "runtime_unavailable"
    assert result.updated_task is None


# --- the actual repair -----------------------------------------------------


async def test_a_lagging_projection_is_repaired_from_the_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    task, ctx = _linked(status="pending")
    contract = RecordingContract(status={"run_id": RUN_ID, "status": "executing"})

    result = await reconcile_linked_task_runtime(task, context=ctx, contract=contract)

    assert result.action == "reconciled"
    assert result.updated_task is not None
    assert result.updated_task["status"] == "executing"
    assert _history(result.updated_task)[-1]["runtime_run_id"] == RUN_ID
    assert _history(result.updated_task)[-1]["runtime_status"] == "executing"
    assert contract.create_calls == [], "a run is never created while reconciling"


async def test_reconciling_after_a_restart_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The restart shape: reload the task from the existing persistence row."""
    monkeypatch.setenv(SWITCH, "1")
    from evoflow.persistence import task_row_mappers as m

    task, ctx = _linked(status="pending")
    contract = RecordingContract(status={"run_id": RUN_ID, "status": "executing"})

    first = await reconcile_linked_task_runtime(task, context=ctx, contract=contract)
    assert first.action == "reconciled"

    # Simulate the process boundary: only the persisted row survives.
    known, extra_json = m._split_extra(first.updated_task, m._TASK_KNOWN)
    reloaded = m._merge_extra(dict(known), extra_json)
    assert reloaded[LINKAGE_TASK_KEY]["runtime_run_id"] == RUN_ID

    after_restart = await reconcile_linked_task_runtime(reloaded, context=ctx, contract=contract)

    assert after_restart.action == "unchanged"
    assert after_restart.reason in {"duplicate_status", "status_unchanged"}
    assert after_restart.updated_task is None, "nothing to write back"
    assert len(_history(reloaded)) == len(_history(first.updated_task))
    assert contract.create_calls == []
    assert contract.status_calls == [RUN_ID, RUN_ID], "reads only, never a write"


async def test_reconciliation_never_moves_a_task_backwards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    task, ctx = _linked(status="pending")
    advanced = RecordingContract(status={"run_id": RUN_ID, "status": "executing"})
    first = await reconcile_linked_task_runtime(task, context=ctx, contract=advanced)

    # The Runtime now answers with an earlier stage (a stale read / replica lag).
    stale = RecordingContract(status={"run_id": RUN_ID, "status": "queued"})
    second = await reconcile_linked_task_runtime(
        first.updated_task, context=ctx, contract=stale
    )

    assert second.action == "unchanged"
    assert second.reason == "runtime_status_regression"
    assert first.updated_task["status"] == "executing"


# --- unusable Runtime answers are fail-safe --------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        "not-a-mapping",
        {},
        {"run_id": RUN_ID},
        {"status": "   "},
    ],
)
async def test_an_unusable_runtime_answer_leaves_the_task_alone(
    monkeypatch: pytest.MonkeyPatch, answer: Any
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    task, ctx = _linked(status="pending")
    contract = RecordingContract(status=answer)

    result = await reconcile_linked_task_runtime(task, context=ctx, contract=contract)

    assert result.action == "runtime_unavailable"
    assert result.updated_task is None
    assert _history(task) == []


async def test_a_run_answering_for_another_run_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    task, ctx = _linked(status="pending")
    contract = RecordingContract(status={"run_id": "run-999", "status": "completed"})

    result = await reconcile_linked_task_runtime(task, context=ctx, contract=contract)

    assert result.action == "runtime_unavailable"
    assert result.updated_task is None
    assert task["status"] == "pending", "another run's state must not be adopted"


async def test_a_foreign_organization_linkage_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SWITCH, "1")
    from app.gateway.task_runtime_linkage import RuntimeRunLinkageError

    task, _ = _linked(org_id=ORG_A)
    contract = RecordingContract()

    with pytest.raises(RuntimeRunLinkageError, match="another organization"):
        await reconcile_linked_task_runtime(task, context=_ctx(ORG_B), contract=contract)

    assert contract.status_calls == []


# --- the read-only promise, structurally -----------------------------------


def test_reconciliation_has_no_write_path_to_the_runtime() -> None:
    source = Path("app/gateway/task_runtime_reconcile.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not (called & {"create_run", "start_run", "request_cancel", "resume_run"})

    # The contract it accepts is the read-only slice.
    protocol = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "RuntimeReconcileContract"
    )
    declared = {
        node.name
        for node in protocol.body
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
    }
    assert declared == {"get_run_status"}
