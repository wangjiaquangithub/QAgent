"""Protective tests for fail-safe display of a broken Runtime linkage.

Covers AG-G2-AUTO-026: a Task Center read must survive a Runtime run that has been
deleted, a linkage from another organization or another task, a malformed linkage,
and a task/run terminal disagreement — without claiming a success that did not
happen and without standing up a replacement run.
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from app.gateway import task_runtime_degrade as degrade
from app.gateway.task_runtime_context import build_task_runtime_context, idempotency_key_for
from app.gateway.task_runtime_linkage import LINKAGE_TASK_KEY, RuntimeRunLinkageError
from app.gateway.task_runtime_reconcile import RuntimeReconcileContract

ORG_A = "org-a"
ORG_B = "org-b"
SWITCH = "EVOFLOW_AUTOMATION_UNATTENDED_RUNTIME"
TASK_ID = "e4c1a2b0"
ORG_SCOPE_KEY = f"tc:org:{ORG_A}"
MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "gateway" / "task_runtime_degrade.py"
)

_REMOTE_WRITE_CALLS = ("create_run", "start_run", "resume_run", "request_cancel")


class StatusContract:
    """Read-only Runtime contract: returns a fixed status, or raises."""

    def __init__(self, *, status: Any = None, raises: BaseException | None = None) -> None:
        self.status_calls: list[str] = []
        self._status = {"status": "running"} if status is None else status
        self._raises = raises

    async def get_run_status(self, run_id: str) -> dict[str, Any]:
        self.status_calls.append(run_id)
        if self._raises is not None:
            raise self._raises
        payload = dict(self._status) if isinstance(self._status, dict) else self._status
        if isinstance(payload, dict):
            payload.setdefault("run_id", run_id)
        return payload


class NoCallContract:
    """A contract that must never be reached at all."""

    def __init__(self) -> None:
        self.status_calls: list[str] = []

    async def get_run_status(self, run_id: str) -> dict[str, Any]:
        self.status_calls.append(run_id)
        raise AssertionError("a broken link must not reach the Runtime")


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


def _linkage(
    *, run_id: str = "run-old", org_scope_key: str = ORG_SCOPE_KEY, task_id: str = TASK_ID
) -> dict[str, str]:
    return {
        "runtime_run_id": run_id,
        "org_scope_key": org_scope_key,
        "task_id": task_id,
        "idempotency_key": idempotency_key_for(org_scope_key, task_id, 0),
    }


def _ctx(task: Mapping[str, Any] | None = None, *, org_id: str = ORG_A):
    return build_task_runtime_context(
        task=task or _task(), authz=_identity(org_id), authorized=True
    )


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SWITCH, raising=False)


def _reconcile(task: Mapping[str, Any], contract: RuntimeReconcileContract | None, *, org_id: str = ORG_A):
    return asyncio.run(
        degrade.reconcile_task_runtime_safely(
            task, context=_ctx(task, org_id=org_id), contract=contract
        )
    )


# --- a healthy link is untouched ------------------------------------------------


def test_a_healthy_link_is_assessed_as_linked() -> None:
    task = _task(**{LINKAGE_TASK_KEY: _linkage()})

    health = degrade.assess_runtime_link(task, context=_ctx(task))

    assert health.state == "linked"
    assert health.usable
    assert health.run_id == "run-old"
    assert health.displayable_run_id == "run-old"


def test_a_task_without_a_link_is_assessed_as_unlinked() -> None:
    task = _task()

    health = degrade.assess_runtime_link(task, context=_ctx(task))

    assert health.state == "unlinked"
    assert health.usable
    assert health.displayable_run_id is None


def test_a_healthy_link_reconciles_through_the_existing_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SWITCH, "1")
    task = _task(**{LINKAGE_TASK_KEY: _linkage()})
    contract = StatusContract(status={"status": "running"})

    outcome = _reconcile(task, contract)

    assert outcome.action == "reconciled"
    assert contract.status_calls == ["run-old"]


# --- a deleted run ---------------------------------------------------------------


def test_a_deleted_run_degrades_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Runtime answers a missing run with KeyError; the task read survives it."""
    monkeypatch.setenv(SWITCH, "1")
    task = _task(status="executing", **{LINKAGE_TASK_KEY: _linkage()})
    contract = StatusContract(raises=KeyError("run_old"))

    outcome = _reconcile(task, contract)

    assert outcome.action == "runtime_unavailable"
    assert outcome.reason == "run_missing"
    assert outcome.updated_task is None  # nothing was written


def test_a_deleted_run_does_not_fabricate_a_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SWITCH, "1")
    task = _task(status="executing", **{LINKAGE_TASK_KEY: _linkage()})

    outcome = _reconcile(task, StatusContract(raises=KeyError("run_old")))

    assert outcome.projected_status is None
    assert task["status"] == "executing"


def test_a_deleted_run_is_not_replaced_by_a_new_run() -> None:
    """No creation call may exist in the module that handles a broken link."""
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            called.add(node.attr)
        if isinstance(node, ast.Name):
            called.add(node.id)
    for remote in _REMOTE_WRITE_CALLS:
        assert remote not in called, f"{remote} must not appear in the degrade path"


# --- a foreign organization's linkage --------------------------------------------


def test_a_foreign_linkage_degrades_without_revealing_the_run() -> None:
    task = _task(**{LINKAGE_TASK_KEY: _linkage(org_scope_key=f"tc:org:{ORG_B}")})

    health = degrade.assess_runtime_link(task, context=_ctx(task))

    assert health.state == "unusable"
    assert health.reason == "linkage_foreign_org"
    assert not health.usable
    assert health.run_id is None
    assert health.displayable_run_id is None


def test_a_foreign_linkage_is_never_read_from_the_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SWITCH, "1")
    task = _task(**{LINKAGE_TASK_KEY: _linkage(org_scope_key=f"tc:org:{ORG_B}")})
    contract = NoCallContract()

    outcome = _reconcile(task, contract)

    assert outcome.action == "runtime_unavailable"
    assert outcome.reason == "linkage_foreign_org"
    assert outcome.runtime_run_id is None
    assert contract.status_calls == []


def test_a_foreign_linkage_still_yields_a_readable_task() -> None:
    task = _task(status="executing", **{LINKAGE_TASK_KEY: _linkage(org_scope_key=f"tc:org:{ORG_B}")})

    degraded = degrade.read_degraded_task_stream_view(task, context=_ctx(task))

    assert degraded.view.status == "executing"
    assert degraded.view.runtime_run_id is None
    assert not degraded.view.linked
    assert degraded.health.reason == "linkage_foreign_org"


# --- a malformed linkage ---------------------------------------------------------


def test_a_malformed_linkage_degrades_instead_of_raising() -> None:
    task = _task(**{LINKAGE_TASK_KEY: "not-json-at-all"})

    health = degrade.assess_runtime_link(task, context=_ctx(task))

    assert health.state == "unusable"
    assert health.reason == "linkage_malformed"
    assert health.displayable_run_id is None


def test_a_linkage_missing_a_field_is_malformed() -> None:
    task = _task(**{LINKAGE_TASK_KEY: {"runtime_run_id": "run-old"}})

    health = degrade.assess_runtime_link(task, context=_ctx(task))

    assert health.reason == "linkage_malformed"


def test_a_linkage_with_a_malformed_run_id_degrades() -> None:
    task = _task(**{LINKAGE_TASK_KEY: _linkage(run_id="bad run id!")})

    health = degrade.assess_runtime_link(task, context=_ctx(task))

    assert health.reason == "linkage_malformed"


def test_a_linkage_for_another_task_degrades() -> None:
    task = _task(**{LINKAGE_TASK_KEY: _linkage(task_id="deadbeef")})

    health = degrade.assess_runtime_link(task, context=_ctx(task))

    assert health.reason == "linkage_other_task"


def test_a_malformed_linkage_is_not_retried() -> None:
    assert not degrade.is_retryable_reason("linkage_malformed")
    assert not degrade.is_retryable_reason("run_missing")
    assert degrade.is_retryable_reason("runtime_unavailable")


# --- terminal disagreement -------------------------------------------------------


def test_a_terminal_task_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """A task already settled locally is not moved by the run's terminal status."""
    monkeypatch.setenv(SWITCH, "1")
    task = _task(status="cancelled", **{LINKAGE_TASK_KEY: _linkage()})
    contract = StatusContract(status={"status": "completed"})

    outcome = _reconcile(task, contract)

    assert outcome.action == "terminal"
    assert outcome.updated_task is None
    assert task["status"] == "cancelled"


def test_a_terminal_run_does_not_mark_an_unsettled_task_completed() -> None:
    """The projection may converge the task, but nothing here invents a record."""
    task = _task(status="inbox", **{LINKAGE_TASK_KEY: _linkage()})

    degraded = degrade.read_degraded_task_stream_view(task, context=_ctx(task))

    assert degraded.view.status == "inbox"
    assert degraded.view.history == ()


# --- an unreadable Runtime -------------------------------------------------------


def test_an_unreadable_runtime_degrades_as_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SWITCH, "1")
    task = _task(status="executing", **{LINKAGE_TASK_KEY: _linkage()})
    contract = StatusContract(raises=RuntimeError("postgres is down"))

    outcome = _reconcile(task, contract)

    assert outcome.action == "runtime_unavailable"
    assert outcome.reason == "runtime_unavailable"
    assert degrade.is_retryable_reason(outcome.reason)
    assert outcome.runtime_run_id == "run-old"


def test_the_switch_being_off_leaves_the_existing_behaviour(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SWITCH, raising=False)
    task = _task(status="executing", **{LINKAGE_TASK_KEY: _linkage(org_scope_key=f"tc:org:{ORG_B}")})
    contract = NoCallContract()

    outcome = _reconcile(task, contract)

    assert outcome.action == "disabled"
    assert contract.status_calls == []


# --- the degraded view itself ----------------------------------------------------


def test_the_degraded_view_keeps_the_history_the_user_already_had() -> None:
    history = [
        {"source": "qagent_runtime", "runtime_run_id": "run-old", "runtime_status": "running"}
    ]
    task = _task(
        status="executing",
        execution_history=history,
        **{LINKAGE_TASK_KEY: _linkage(org_scope_key=f"tc:org:{ORG_B}")},
    )

    degraded = degrade.read_degraded_task_stream_view(task, context=_ctx(task))

    assert list(degraded.view.history) == history
    assert degraded.view.watermark is None
    assert degraded.view.cursor_rejected is False


def test_a_healthy_link_still_uses_the_normal_recovery_view() -> None:
    task = _task(**{LINKAGE_TASK_KEY: _linkage()})

    degraded = degrade.read_degraded_task_stream_view(task, context=_ctx(task))

    assert degraded.view.runtime_run_id == "run-old"
    assert degraded.view.linked


# --- caller errors stay loud -----------------------------------------------------


def test_a_missing_task_row_is_a_caller_error() -> None:
    with pytest.raises(degrade.RuntimeDegradeError, match="server-loaded task row"):
        degrade.assess_runtime_link(None, context=_ctx())


def test_a_mismatched_context_is_a_caller_error() -> None:
    task = _task()
    other = _task(id="deadbeef")

    with pytest.raises(degrade.RuntimeDegradeError, match="does not belong to this task"):
        degrade.assess_runtime_link(task, context=_ctx(other))


def test_a_linkage_refusal_is_still_a_refusal_upstream() -> None:
    """The shared gate keeps refusing; only this module softens it."""
    from app.gateway.task_runtime_linkage import read_linked_runtime_run

    task = _task(**{LINKAGE_TASK_KEY: _linkage(org_scope_key=f"tc:org:{ORG_B}")})

    with pytest.raises(RuntimeRunLinkageError):
        read_linked_runtime_run(task, context=_ctx(task))
