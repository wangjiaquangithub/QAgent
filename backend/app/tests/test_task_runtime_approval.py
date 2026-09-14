"""Protective tests for runtime approval visibility on tasks.

Covers AG-G2-AUTO-016: waiting_approval is explicitly visible, a granted/rejected
decision is visible once and never moves the task, the task status converges from
the run status the Runtime reports next, and nothing a client writes can make a
task look approved.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.gateway.task_runtime_context import build_task_runtime_context
from app.gateway.task_runtime_event_bridge import RuntimeEventBridgeError, apply_runtime_event
from app.gateway.task_runtime_linkage import link_runtime_run
from app.gateway.task_runtime_projection import HISTORY_FIELD

ORG_A = "org-a"
ORG_B = "org-b"
TASK_ID = "e4c1a2b0"
RUN_ID = "run-1"


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


def _linked(*, status: str = "pending", org_id: str = ORG_A) -> tuple[dict[str, Any], Any]:
    ctx = build_task_runtime_context(task=_task(status=status), authz=_authz(org_id), authorized=True)
    task, _ = link_runtime_run(_task(status=status), context=ctx, runtime_run_id=RUN_ID)
    return task, ctx


def _history(task: dict[str, Any]) -> list[dict[str, Any]]:
    return list(task.get(HISTORY_FIELD) or [])


def _frame(event_type: str, *, event_id: str = "ev-1", sequence: int = 1, run_id: str = RUN_ID):
    return {
        "event_id": event_id,
        "run_id": run_id,
        "sequence": sequence,
        "type": event_type,
        "payload": {},
    }


def _apply(task: dict[str, Any], ctx: Any, event_type: str, **kwargs: Any) -> tuple[dict, Any]:
    return apply_runtime_event(task, context=ctx, event=_frame(event_type, **kwargs))


# --- waiting -----------------------------------------------------------------


def test_waiting_approval_is_explicitly_visible_and_keeps_the_status() -> None:
    task, ctx = _linked(status="pending")

    updated, outcome = _apply(task, ctx, "run.waiting_approval")

    assert outcome.action == "history_only"
    assert updated["status"] == "pending", "waiting_approval must not become paused or planned"
    record = _history(updated)[-1]
    assert record["runtime_status"] == "waiting_approval"
    assert record["approval_required"] is True
    assert record["hint"] == "Runtime run is waiting for an approval decision."


# --- decisions ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("frame", "marker"),
    [("approval.granted", "approval_granted"), ("approval.rejected", "approval_rejected")],
)
def test_a_decision_is_visible_without_moving_the_task(frame: str, marker: str) -> None:
    task, ctx = _linked(status="pending")

    updated, outcome = _apply(task, ctx, frame, event_id="ev-decision")

    assert outcome.action == "history_only"
    assert updated["status"] == "pending"
    record = _history(updated)[-1]
    assert record["runtime_status"] == marker
    assert record["approval_required"] is False


def test_the_run_status_the_runtime_reports_next_converges_the_task() -> None:
    task, ctx = _linked(status="pending")

    waiting, _ = _apply(task, ctx, "run.waiting_approval", event_id="ev-1", sequence=1)
    granted, _ = _apply(waiting, ctx, "approval.granted", event_id="ev-2", sequence=2)
    running, outcome = _apply(granted, ctx, "run.running", event_id="ev-3", sequence=3)

    assert outcome.status_after == "executing"
    assert running["status"] == "executing", "the Runtime's run status drives the task"


def test_a_rejected_decision_never_shows_success() -> None:
    task, ctx = _linked(status="pending")

    waiting, _ = _apply(task, ctx, "run.waiting_approval", event_id="ev-1", sequence=1)
    rejected, _ = _apply(waiting, ctx, "approval.rejected", event_id="ev-2", sequence=2)

    assert rejected["status"] == "pending", "a rejection is not a completion"

    failed, outcome = _apply(rejected, ctx, "run.failed", event_id="ev-3", sequence=3)
    assert outcome.status_after == "failed"
    assert failed["status"] == "failed"
    assert "completed" not in [entry["runtime_status"] for entry in _history(failed)]


def test_a_repeated_decision_is_recorded_once() -> None:
    task, ctx = _linked(status="pending")

    first, first_outcome = _apply(task, ctx, "approval.granted", event_id="ev-a", sequence=1)
    assert first_outcome.action == "history_only"
    before = len(_history(first))

    second, second_outcome = _apply(first, ctx, "approval.granted", event_id="ev-b", sequence=2)

    assert second_outcome.action == "noop"
    assert second_outcome.reason == "duplicate_status"
    assert len(_history(second)) == before


def test_a_full_approval_round_lands_as_an_ordered_history() -> None:
    task, ctx = _linked(status="pending")

    waiting, _ = _apply(task, ctx, "run.waiting_approval", event_id="ev-1", sequence=1)
    granted, _ = _apply(waiting, ctx, "approval.granted", event_id="ev-2", sequence=2)
    running, _ = _apply(granted, ctx, "run.running", event_id="ev-3", sequence=3)
    completed, outcome = _apply(
        running, ctx, "run.completed", event_id="ev-4", sequence=4
    )

    assert [entry["runtime_status"] for entry in _history(completed)] == [
        "waiting_approval",
        "approval_granted",
        "running",
        "completed",
    ]
    assert outcome.status_after == "completed"


def test_a_decision_cannot_be_applied_to_a_settled_task() -> None:
    task, ctx = _linked(status="pending")
    done, _ = _apply(task, ctx, "run.completed", event_id="ev-1", sequence=1)

    after, outcome = _apply(done, ctx, "approval.rejected", event_id="ev-2", sequence=2)

    assert outcome.action == "stale"
    assert outcome.reason == "task_already_terminal"
    assert after["status"] == "completed"


def test_a_decision_from_another_organization_is_refused() -> None:
    task, _ = _linked(org_id=ORG_A)
    other = build_task_runtime_context(task=_task(), authz=_authz(ORG_B), authorized=True)

    with pytest.raises(RuntimeEventBridgeError, match="another organization"):
        _apply(task, other, "approval.granted", event_id="ev-x")


def test_a_decision_frame_for_another_run_is_refused() -> None:
    task, ctx = _linked()
    before = list(_history(task))

    with pytest.raises(RuntimeEventBridgeError, match="different runtime run"):
        _apply(task, ctx, "approval.granted", event_id="ev-x", run_id="run-999")

    assert _history(task) == before


# --- a client can never approve anything -------------------------------------


def test_forged_approval_metadata_on_the_task_changes_nothing() -> None:
    forged = _task(
        status="pending",
        approval_required=False,
        approval_state="approved",
        runtime_approval={"granted": True},
        approval_granted=True,
    )
    ctx = build_task_runtime_context(task=forged, authz=_authz(ORG_A), authorized=True)
    task, _ = link_runtime_run(forged, context=ctx, runtime_run_id=RUN_ID)

    # A genuine waiting_approval still reports that an approval is required.
    waiting, _ = _apply(task, ctx, "run.waiting_approval", event_id="ev-1", sequence=1)
    record = _history(waiting)[-1]
    assert record["approval_required"] is True
    assert waiting["status"] == "pending"

    # And no projection of a non-terminal frame ever turns the task into a success.
    running, _ = _apply(waiting, ctx, "run.running", event_id="ev-2", sequence=2)
    assert running["status"] == "executing"
    for entry in _history(running):
        assert entry["runtime_status"] != "completed"
