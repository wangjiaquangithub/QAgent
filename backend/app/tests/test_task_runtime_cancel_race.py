"""Protective tests for cancellation racing with a Runtime run's own outcome.

Covers AG-G2-AUTO-024: whichever of cancellation and settlement arrives first
decides the task, a completion or failure that lands after a cancellation never
overwrites it, a cancellation that lands after settlement is not faked, and the
display follows the Runtime's own facts.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from app.gateway.task_runtime_cancel import cancel_linked_runtime_run
from app.gateway.task_runtime_context import build_task_runtime_context
from app.gateway.task_runtime_linkage import link_runtime_run
from app.gateway.task_runtime_projection import (
    HISTORY_FIELD,
    project_runtime_status,
    task_status_for_runtime_status,
)

ORG_A = "org-a"
TASK_ID = "e4c1a2b0"
RUN_ID = "run-1"


class RuntimeContract:
    """Answers the way the Runtime's own cancel call does: with the run's state."""

    def __init__(self, settle_to: str = "cancelled") -> None:
        self.settle_to = settle_to
        self.cancel_calls: list[str] = []

    async def request_cancel(self, run_id: str) -> dict[str, Any]:
        self.cancel_calls.append(run_id)
        return {"run_id": run_id, "status": self.settle_to}


def _authz() -> dict[str, Any]:
    return {
        "org_id": ORG_A,
        "principal": {"principal_id": "webui:1", "principal_type": "internal"},
        "scope_id": "personal:webui:1",
        "is_org_admin": False,
    }


def _task(*, status: str = "executing", **extra: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": TASK_ID,
        "name": "每周经营简报",
        "run_mode": "unattended",
        "status": status,
        "unattended_attempts": 0,
        HISTORY_FIELD: [],
    }
    task.update(extra)
    return task


def _linked(*, status: str = "executing") -> tuple[dict[str, Any], Any]:
    ctx = build_task_runtime_context(task=_task(status=status), authz=_authz(), authorized=True)
    task, _ = link_runtime_run(_task(status=status), context=ctx, runtime_run_id=RUN_ID)
    return task, ctx


def _history(task: dict[str, Any]) -> list[dict[str, Any]]:
    return list(task.get(HISTORY_FIELD) or [])


def _statuses(task: dict[str, Any]) -> list[str]:
    return [str(entry.get("runtime_status") or "") for entry in _history(task)]


async def _cancel(task: dict[str, Any], ctx: Any, *, settle_to: str = "cancelled"):
    return await cancel_linked_runtime_run(
        task, context=ctx, contract=RuntimeContract(settle_to)
    )


# --- cancel first ----------------------------------------------------------


@pytest.mark.parametrize("late_status", ["completed", "failed"])
async def test_a_settlement_after_a_cancellation_never_overwrites_it(
    late_status: str,
) -> None:
    task, ctx = _linked()
    cancelled = await _cancel(task, ctx)
    assert cancelled.updated_task is not None
    locally_cancelled = dict(cancelled.updated_task) | {"status": "cancelled"}

    after, projection = project_runtime_status(
        locally_cancelled, context=ctx, runtime_status=late_status, sequence=80
    )

    assert projection.action == "noop"
    assert projection.reason == "terminal_already_reached"
    assert after["status"] == "cancelled"
    assert _statuses(after) == ["cancelled"]


async def test_a_settlement_after_a_cancellation_never_overwrites_it_without_local_status() -> None:
    # The same race where the caller never set the status itself: the recorded
    # cancellation is what holds the line.
    task, ctx = _linked()
    cancelled = await _cancel(task, ctx)
    assert cancelled.updated_task is not None

    after, projection = project_runtime_status(
        cancelled.updated_task, context=ctx, runtime_status="completed", sequence=80
    )

    assert projection.action == "noop"
    assert projection.reason == "cancellation_already_recorded"
    assert after["status"] == "executing"
    assert _statuses(after) == ["cancelled"]


async def test_a_failure_after_a_cancellation_never_overwrites_it() -> None:
    task, ctx = _linked()
    cancelled = await _cancel(task, ctx)
    assert cancelled.updated_task is not None

    after, projection = project_runtime_status(
        cancelled.updated_task, context=ctx, runtime_status="failed", sequence=81
    )

    assert projection.action == "noop"
    assert after["status"] == "executing"


# --- settled first ---------------------------------------------------------


@pytest.mark.parametrize(
    ("reported", "expected_task_status"),
    [("completed", "completed"), ("failed", "failed"), ("timed_out", "failed")],
)
async def test_a_cancellation_after_settlement_is_not_faked(
    reported: str, expected_task_status: str
) -> None:
    task, ctx = _linked()
    contract = RuntimeContract(reported)
    # What the existing local cancel handler leaves behind before this runs.
    locally_cancelled = dict(task) | {"status": "cancelled"}

    outcome = await cancel_linked_runtime_run(
        locally_cancelled, context=ctx, contract=contract
    )

    assert outcome.action == "runtime_refused"
    assert outcome.reason == f"runtime_reported_{reported}"
    assert contract.cancel_calls == [RUN_ID]
    assert outcome.updated_task is not None
    assert outcome.updated_task["status"] == expected_task_status, (
        "the task must show what the Runtime actually reports, not a cancelled it never was"
    )
    assert _statuses(outcome.updated_task) == [reported]


async def test_the_task_status_restored_uses_the_existing_vocabulary() -> None:
    # timed_out maps onto the Task Center's existing failed, through the same
    # mapping the projection uses -- not a new status invented here.
    assert task_status_for_runtime_status("timed_out") == "failed"
    assert task_status_for_runtime_status("finished") is None

    task, ctx = _linked()
    outcome = await _cancel(dict(task) | {"status": "cancelled"}, ctx, settle_to="timed_out")

    assert outcome.updated_task is not None
    assert outcome.updated_task["status"] == "failed"


async def test_a_still_running_run_leaves_the_users_cancellation_alone() -> None:
    # The Runtime did not cancel it, but the run is not over either: the user's
    # cancellation stays in force and the status is not corrected.
    task, ctx = _linked()
    contract = RuntimeContract("running")

    outcome = await cancel_linked_runtime_run(
        dict(task) | {"status": "cancelled"}, context=ctx, contract=contract
    )

    assert outcome.action == "runtime_refused"
    assert contract.cancel_calls == [RUN_ID]
    assert outcome.updated_task is not None
    assert outcome.updated_task["status"] == "cancelled"
    assert _statuses(outcome.updated_task) == ["running"]


async def test_an_existing_terminal_outcome_is_not_rearbitrated() -> None:
    # The task already settled on its own; a cancellation then gets refused. The
    # terminal protection still holds: only the cancellation this path may have
    # caused is correctable.
    task, ctx = _linked(status="failed")

    outcome = await _cancel(dict(task) | {"status": "failed"}, ctx, settle_to="completed")

    assert outcome.action == "runtime_refused"
    assert outcome.updated_task is not None
    assert outcome.updated_task["status"] == "failed"
    assert _statuses(outcome.updated_task) == ["completed"]


async def test_a_settled_run_that_is_already_the_displayed_status_is_not_rewritten() -> None:
    task, ctx = _linked(status="completed")

    outcome = await _cancel(dict(task) | {"status": "cancelled"}, ctx, settle_to="completed")

    assert outcome.updated_task is not None
    assert outcome.updated_task["status"] == "completed"


# --- repeats ---------------------------------------------------------------


async def test_repeat_cancellation_after_a_settlement_changes_nothing_further() -> None:
    task, ctx = _linked()
    first = await _cancel(dict(task) | {"status": "cancelled"}, ctx, settle_to="completed")
    assert first.updated_task is not None

    second = await _cancel(first.updated_task, ctx, settle_to="completed")

    assert second.action == "runtime_refused"
    assert second.updated_task is None, "there is nothing new to say, so nothing is written"
    assert _statuses(first.updated_task) == ["completed"], "the history must not grow"


async def test_repeat_cancellation_after_an_accepted_one_is_free() -> None:
    task, ctx = _linked()
    contract = RuntimeContract("cancelled")

    first = await cancel_linked_runtime_run(task, context=ctx, contract=contract)
    second = await cancel_linked_runtime_run(
        first.updated_task or task, context=ctx, contract=contract
    )

    assert first.action == "runtime_cancelled"
    assert second.action == "already_cancelled"
    assert contract.cancel_calls == [RUN_ID], "the runtime must not be asked twice"
    assert _statuses(first.updated_task or {}) == ["cancelled"]


async def test_a_settlement_after_an_accepted_cancellation_keeps_the_single_record() -> None:
    task, ctx = _linked()
    cancelled = await _cancel(task, ctx)
    assert cancelled.updated_task is not None
    locally_cancelled = dict(cancelled.updated_task) | {"status": "cancelled"}

    after, projection = project_runtime_status(
        locally_cancelled, context=ctx, runtime_status="completed", sequence=90
    )
    again, repeat = project_runtime_status(
        after, context=ctx, runtime_status="failed", sequence=91
    )

    assert projection.action == repeat.action == "noop"
    assert again["status"] == "cancelled"
    assert len(_history(again)) == 1


# --- one mapping, one vocabulary ------------------------------------------


def test_the_restore_uses_the_projections_mapping_rather_than_a_copy() -> None:
    source = Path("app/gateway/task_runtime_cancel.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "task_status_for_runtime_status" in imported

    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "task_status_for_runtime_status" in called, (
        "the runtime -> task status mapping must be used, not re-implemented here"
    )
