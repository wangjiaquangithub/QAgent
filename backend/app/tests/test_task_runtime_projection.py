"""Protective tests for Runtime status projection onto Task Center tasks.

Covers AG-G2-AUTO-003-B02 acceptance points, using the frozen semantics:
waiting_approval never changes the task status, timed_out becomes failed with the
Runtime detail kept in the existing execution_history field, and terminal states
converge without ever regressing.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.gateway.task_runtime_context import build_task_runtime_context
from app.gateway.task_runtime_linkage import LINKAGE_TASK_KEY, link_runtime_run
from app.gateway.task_runtime_projection import (
    HISTORY_FIELD,
    RuntimeProjectionError,
    project_runtime_status,
)

ORG_A = "org-a"
ORG_B = "org-b"
TASK_ID = "e4c1a2b0"
RUN_ID = "run-1"


def _authz(org_id: str, principal_id: str = "webui:1") -> dict[str, Any]:
    return {
        "org_id": org_id,
        "principal": {"principal_id": principal_id, "principal_type": "internal"},
        "scope_id": f"personal:{principal_id}",
        "is_org_admin": False,
    }


def _task(*, status: str = "executing", **extra: Any) -> dict[str, Any]:
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


def _linked(*, org_id: str = ORG_A, status: str = "executing") -> tuple[dict[str, Any], Any]:
    ctx = build_task_runtime_context(
        task=_task(status=status), authz=_authz(org_id), authorized=True
    )
    task, _ = link_runtime_run(_task(status=status), context=ctx, runtime_run_id=RUN_ID)
    return task, ctx


def _history(task: dict[str, Any]) -> list[dict[str, Any]]:
    return list(task.get(HISTORY_FIELD) or [])


# --- waiting_approval ------------------------------------------------------


def test_waiting_approval_does_not_change_the_task_status() -> None:
    task, ctx = _linked(status="executing")

    updated, outcome = project_runtime_status(
        task, context=ctx, runtime_status="waiting_approval", sequence=1, reason="needs sign-off"
    )

    assert outcome.action == "history_only"
    assert updated["status"] == "executing"
    assert updated["status"] not in {"paused", "planned"}
    record = _history(updated)[-1]
    assert record["runtime_status"] == "waiting_approval"
    assert record["approval_required"] is True
    assert record["source"] == "qagent_runtime"
    assert record["reason"] == "needs sign-off"


def test_waiting_approval_from_pending_also_leaves_the_status_alone() -> None:
    task, ctx = _linked(status="pending")
    updated, outcome = project_runtime_status(
        task, context=ctx, runtime_status="waiting_approval", sequence=1
    )
    assert outcome.action == "history_only"
    assert updated["status"] == "pending"


# --- timed_out -------------------------------------------------------------


def test_timed_out_maps_to_failed_and_keeps_the_runtime_detail() -> None:
    task, ctx = _linked()
    updated, outcome = project_runtime_status(
        task,
        context=ctx,
        runtime_status="timed_out",
        error_code="run.timeout!!",
        reason="exceeded 900s budget\nsecond line",
        sequence=7,
    )

    assert outcome.action == "status_updated"
    assert updated["status"] == "failed"
    record = _history(updated)[-1]
    assert record["runtime_status"] == "timed_out"
    assert record["error_code"] == "run.timeout"
    assert record["reason"] == "exceeded 900s budget second line"
    assert record["sequence"] == 7


# --- terminal convergence --------------------------------------------------


@pytest.mark.parametrize(
    ("runtime_status", "expected"),
    [("completed", "completed"), ("failed", "failed"), ("cancelled", "cancelled")],
)
def test_terminal_statuses_converge_to_the_existing_values(
    runtime_status: str, expected: str
) -> None:
    task, ctx = _linked()
    updated, outcome = project_runtime_status(
        task, context=ctx, runtime_status=runtime_status, sequence=1
    )
    assert outcome.action == "status_updated"
    assert updated["status"] == expected
    assert _history(updated)[-1]["runtime_status"] == runtime_status


def test_repeated_terminal_event_does_not_regress_or_duplicate() -> None:
    task, ctx = _linked()
    first, _ = project_runtime_status(
        task, context=ctx, runtime_status="completed", event_id="ev-1", sequence=9
    )
    before = len(_history(first))

    second, outcome = project_runtime_status(
        first, context=ctx, runtime_status="completed", event_id="ev-1", sequence=9
    )

    assert outcome.action == "noop"
    assert second["status"] == "completed"
    assert len(_history(second)) == before


def test_a_later_terminal_event_does_not_overwrite_an_earlier_terminal() -> None:
    task, ctx = _linked()
    cancelled, _ = project_runtime_status(
        task, context=ctx, runtime_status="cancelled", event_id="ev-cancel", sequence=5
    )

    after, outcome = project_runtime_status(
        cancelled, context=ctx, runtime_status="completed", event_id="ev-done", sequence=6
    )

    assert outcome.action == "noop"
    assert outcome.reason == "terminal_already_reached"
    assert after["status"] == "cancelled", "the old executor must not overwrite a cancel"


# --- stale / regression ----------------------------------------------------


def test_an_older_sequence_never_moves_the_task() -> None:
    task, ctx = _linked()
    advanced, _ = project_runtime_status(
        task, context=ctx, runtime_status="running", event_id="ev-5", sequence=5
    )

    after, outcome = project_runtime_status(
        advanced, context=ctx, runtime_status="planning", event_id="ev-2", sequence=2
    )

    assert outcome.action == "stale"
    assert outcome.reason == "older_sequence"
    assert after["status"] == "executing"
    assert len(_history(after)) == len(_history(advanced))


def test_a_non_terminal_event_cannot_un_terminate_the_task() -> None:
    task, ctx = _linked()
    done, _ = project_runtime_status(
        task, context=ctx, runtime_status="completed", event_id="ev-1", sequence=1
    )

    after, outcome = project_runtime_status(
        done, context=ctx, runtime_status="running", event_id="ev-9", sequence=9
    )

    assert outcome.action == "stale"
    assert outcome.reason == "task_already_terminal"
    assert after["status"] == "completed"


# --- non-terminal progress -------------------------------------------------


@pytest.mark.parametrize(
    ("runtime_status", "expected"),
    [
        ("created", "pending"),
        ("queued", "pending"),
        ("planning", "planning"),
        ("running", "executing"),
        ("executing", "executing"),
    ],
)
def test_non_terminal_statuses_map_onto_existing_values(
    runtime_status: str, expected: str
) -> None:
    task, ctx = _linked(status="pending")
    updated, outcome = project_runtime_status(
        task, context=ctx, runtime_status=runtime_status, sequence=1
    )
    assert updated["status"] == expected
    assert outcome.status_after == expected


# --- refusal paths ---------------------------------------------------------


def test_a_task_without_a_runtime_link_is_refused() -> None:
    ctx = build_task_runtime_context(task=_task(), authz=_authz(ORG_A), authorized=True)
    with pytest.raises(RuntimeProjectionError, match="not linked to a runtime run"):
        project_runtime_status(_task(), context=ctx, runtime_status="completed")


def test_cross_organization_projection_is_refused() -> None:
    task, _ = _linked(org_id=ORG_A)
    other_org_ctx = build_task_runtime_context(
        task=_task(), authz=_authz(ORG_B), authorized=True
    )

    with pytest.raises(RuntimeProjectionError, match="another organization"):
        project_runtime_status(task, context=other_org_ctx, runtime_status="completed")


def test_context_for_another_task_is_refused() -> None:
    task = _task()
    other = build_task_runtime_context(
        task=_task(id="deadbeef"), authz=_authz(ORG_A), authorized=True
    )
    with pytest.raises(RuntimeProjectionError, match="does not belong to this task"):
        project_runtime_status(task, context=other, runtime_status="completed")


def test_unknown_runtime_status_is_refused() -> None:
    task, ctx = _linked()
    with pytest.raises(RuntimeProjectionError, match="status is required"):
        project_runtime_status(task, context=ctx, runtime_status="   ")


# --- hygiene ---------------------------------------------------------------


def test_history_records_are_json_safe_and_carry_no_identifier_leakage() -> None:
    import json

    task, ctx = _linked()
    updated, _ = project_runtime_status(
        task,
        context=ctx,
        runtime_status="failed",
        event_id="ev-1",
        sequence=1,
        error_code="provider.error",
        reason="upstream\nfailed  " + "x" * 500,
    )

    record = _history(updated)[-1]
    json.dumps(record, sort_keys=True)
    assert len(record["reason"]) <= 240
    assert "\n" not in record["reason"]
    # The record carries the trusted org partition, never a client-supplied one.
    assert record["org_scope_key"] == ctx.org_scope_key


def test_projection_does_not_touch_the_linkage() -> None:
    task, ctx = _linked()
    before = dict(task[LINKAGE_TASK_KEY])

    updated, _ = project_runtime_status(
        task, context=ctx, runtime_status="running", sequence=1
    )

    assert updated[LINKAGE_TASK_KEY] == before
