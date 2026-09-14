"""Protective tests for the persisted Runtime scheduling guard.

Covers AG-G2-AUTO-021: consecutive ticks and repeated enqueues must not advance a
task whose current attempt already has a Runtime run, the answer must come from
persisted state rather than an in-process lock, and with the opt-in switch off the
tick must pick exactly what it picked before.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from app.gateway.task_runtime_context import (
    TaskRuntimeContextError,
    build_task_runtime_context,
    idempotency_key_for,
)
from app.gateway.task_runtime_linkage import LINKAGE_TASK_KEY, link_runtime_run
from app.gateway.task_runtime_schedule import (
    RuntimeScheduleError,
    decide_runtime_pickup,
)

ORG_A = "org-a"
TASK_ID = "e4c1a2b0"
RUN_ID = "run-1"
SWITCH = "EVOFLOW_AUTOMATION_UNATTENDED_RUNTIME"


def _authz() -> dict[str, Any]:
    return {
        "org_id": ORG_A,
        "principal": {"principal_id": "webui:1", "principal_type": "internal"},
        "scope_id": "personal:webui:1",
        "is_org_admin": False,
    }


def _task(*, task_id: str = TASK_ID, attempt: int = 0, **extra: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": task_id,
        "name": "每周经营简报",
        "run_mode": "unattended",
        "status": "pending",
        "unattended_attempts": attempt,
    }
    task.update(extra)
    return task


def _ctx(task: dict[str, Any] | None = None) -> Any:
    return build_task_runtime_context(
        task=task or _task(), authz=_authz(), authorized=True
    )


def _linked(*, attempt: int = 0) -> dict[str, Any]:
    task = _task(attempt=attempt)
    linked, _ = link_runtime_run(task, context=_ctx(task), runtime_run_id=RUN_ID)
    return linked


def _reload(task: dict[str, Any]) -> dict[str, Any]:
    """Round-trip a task row through the real persistence mapper (a restart)."""
    from evoflow.persistence import task_row_mappers as m

    known, extra_json = m._split_extra(dict(task), m._TASK_KNOWN)
    return m._merge_extra(known, extra_json)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SWITCH, raising=False)


# --- the switch keeps the tick byte-identical when off ---------------------


def test_the_guard_is_inert_with_the_switch_off() -> None:
    decision = decide_runtime_pickup(_linked())

    assert decision.action == "disabled"
    assert decision.should_skip is False, "a tick must pick exactly what it picked before"


def test_a_non_unattended_task_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SWITCH, "1")

    decision = decide_runtime_pickup(_task(run_mode="interactive"))

    assert decision.action == "not_unattended"
    assert decision.should_skip is False


# --- the persisted answer -------------------------------------------------


def test_a_settled_task_is_reported_but_not_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SWITCH, "1")

    decision = decide_runtime_pickup(_linked() | {"status": "failed"})

    assert decision.action == "task_settled"
    assert decision.should_skip is False, "the pipeline owns retry / rerun for a settled task"


def test_an_untouched_attempt_says_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SWITCH, "1")

    decision = decide_runtime_pickup(_task())

    assert decision.action == "schedule"
    assert decision.should_skip is False
    assert decision.runtime_run_id is None


def test_a_consecutive_tick_sees_the_attempt_already_scheduled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    task = _linked()

    first = decide_runtime_pickup(task)
    second = decide_runtime_pickup(task)

    assert first.action == second.action == "already_scheduled"
    assert first.should_skip is True
    assert first.runtime_run_id == RUN_ID
    assert first.task_id == TASK_ID


def test_a_repeat_after_a_restart_reaches_the_same_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    task = _linked()

    assert decide_runtime_pickup(_reload(task)) == decide_runtime_pickup(task)


def test_a_duplicate_enqueue_does_not_look_like_a_first_pickup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    # The same row delivered twice by a duplicated enqueue.
    task = _linked()
    duplicate = dict(task)

    assert decide_runtime_pickup(task).should_skip is True
    assert decide_runtime_pickup(duplicate).should_skip is True


def test_a_new_attempt_is_not_mistaken_for_a_repeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    # The stored linkage belongs to attempt 0; the row is now on attempt 1.
    requeued = _reload(_linked(attempt=0)) | {"unattended_attempts": 1, "status": "pending"}

    decision = decide_runtime_pickup(requeued)

    assert decision.action == "attempt_changed"
    assert decision.should_skip is False, "a retried attempt is the retry path's business"
    assert decision.runtime_run_id == RUN_ID


@pytest.mark.parametrize("stored", ["not-a-mapping", {"runtime_run_id": RUN_ID}, {}])
def test_an_unusable_linkage_is_skipped_rather_than_read_as_unscheduled(
    monkeypatch: pytest.MonkeyPatch, stored: Any
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    task = _task(**{LINKAGE_TASK_KEY: stored})

    decision = decide_runtime_pickup(task)

    assert decision.action == "linkage_unusable"
    assert decision.should_skip is True, "an unreadable linkage must never read as 'no run yet'"


def test_the_decision_needs_a_task_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SWITCH, "1")

    with pytest.raises(RuntimeScheduleError, match="no server-loaded task row"):
        decide_runtime_pickup(None)


# --- the key the guard compares is the key the trigger uses ----------------


def test_the_guard_recomputes_the_key_the_context_uses() -> None:
    task = _task(attempt=2)

    from app.gateway.task_runtime_linkage import read_stored_linkage

    linkage_scope = _ctx(task).org_scope_key
    assert idempotency_key_for(linkage_scope, TASK_ID, 2) == _ctx(task).idempotency_key
    assert idempotency_key_for(linkage_scope, TASK_ID, "2") == _ctx(task).idempotency_key
    assert read_stored_linkage(_task()) is None


def test_the_key_rejects_a_missing_scope_or_task() -> None:
    with pytest.raises(TaskRuntimeContextError, match="organization scope and a task id"):
        idempotency_key_for("", TASK_ID, 0)
    with pytest.raises(TaskRuntimeContextError, match="organization scope and a task id"):
        idempotency_key_for("tc:org:org-a", "", 0)


def test_the_key_separates_organizations_and_attempts() -> None:
    base = idempotency_key_for("tc:org:org-a", TASK_ID, 0)

    assert base != idempotency_key_for("tc:org:org-b", TASK_ID, 0)
    assert base != idempotency_key_for("tc:org:org-a", TASK_ID, 1)
    assert base == idempotency_key_for("tc:org:org-a", TASK_ID, 0)


# --- the guard is a pure selector -----------------------------------------


def test_the_guard_performs_no_io_and_imports_nothing_async() -> None:
    source = Path("app/gateway/task_runtime_schedule.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Await)], (
        "the guard must be answerable without awaiting anything"
    )
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not (called & {"create_run", "get_run_status", "request_cancel", "start_run"})

    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "resolve_server_task_runtime_identity" not in imported
    # It reads the linkage without an organization check, on purpose: it may only
    # defer work, never authorize it.
    assert "read_stored_linkage" in imported
    assert "read_linked_runtime_run" not in imported


def test_decide_runtime_pickup_is_synchronous() -> None:
    import inspect

    assert not inspect.iscoroutinefunction(decide_runtime_pickup)


# --- the tick consults the guard -----------------------------------------


def test_the_tick_consults_the_guard_before_advancing_a_candidate() -> None:
    # ``task_queue_runner`` pulls in the LangChain-based execution stack through
    # the pipeline module, so the wiring is asserted structurally rather than by
    # importing it.
    source = Path("app/gateway/task_queue_runner.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported = {
        (node.module, alias.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert ("app.gateway.task_runtime_schedule", "decide_runtime_pickup") in imported

    tick = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "task_queue_tick"
    )
    names = {node.id for node in ast.walk(tick) if isinstance(node, ast.Name)}
    attrs = {node.attr for node in ast.walk(tick) if isinstance(node, ast.Attribute)}

    assert "decide_runtime_pickup" in names
    assert "should_skip" in attrs
    # The legacy advancement call is still there: the guard only ever skips.
    assert "advance_unattended_task" in names
