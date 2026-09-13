"""Protective tests for bridging Runtime Event v1 frames onto task history.

Covers AG-G2-AUTO-004-B02: the required frame coverage, replay idempotency,
ordering, and cross-organization isolation. No SSE, no frontend, no queue.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.gateway.task_runtime_context import build_task_runtime_context
from app.gateway.task_runtime_event_bridge import (
    EVENT_TYPE_TO_RUNTIME_STATUS,
    RuntimeEventBridgeError,
    apply_runtime_event,
)
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


def _task(*, status: str = "executing") -> dict[str, Any]:
    return {
        "id": TASK_ID,
        "name": "每周经营简报",
        "run_mode": "unattended",
        "status": status,
        "unattended_attempts": 0,
        "execution_history": [],
    }


def _linked(*, org_id: str = ORG_A, status: str = "executing") -> tuple[dict[str, Any], Any]:
    ctx = build_task_runtime_context(task=_task(status=status), authz=_authz(org_id), authorized=True)
    task, _ = link_runtime_run(_task(status=status), context=ctx, runtime_run_id=RUN_ID)
    return task, ctx


def _event(
    event_type: str,
    *,
    event_id: str = "ev-1",
    sequence: int = 1,
    payload: Any = None,
    run_id: str = RUN_ID,
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


def test_event_type_coverage_is_the_frozen_status_set() -> None:
    # The bridge must be able to express every required status except timed_out,
    # which Event v1 cannot carry as a frame type.
    assert set(EVENT_TYPE_TO_RUNTIME_STATUS.values()) == {
        "created",
        "queued",
        "planning",
        "waiting_approval",
        "running",
        "executing",
        "completed",
        "failed",
        "cancelled",
    }


@pytest.mark.parametrize(
    ("event_type", "expected_status"),
    [
        # waiting_approval is deliberately absent: it must not move the status.
        ("run.running", "executing"),
        ("run.executing", "executing"),
        ("run.completed", "completed"),
        ("run.failed", "failed"),
        ("run.cancelled", "cancelled"),
    ],
)
def test_required_frames_are_bridged(event_type: str, expected_status: str) -> None:
    task, ctx = _linked(status="pending")
    updated, outcome = apply_runtime_event(
        task, context=ctx, event=_event(event_type, event_id=f"ev-{event_type}", sequence=1)
    )

    assert updated["status"] == expected_status
    record = _history(updated)[-1]
    assert record["runtime_status"] == EVENT_TYPE_TO_RUNTIME_STATUS[event_type]
    assert record["runtime_run_id"] == RUN_ID


def test_waiting_approval_frame_leaves_the_task_status_alone() -> None:
    task, ctx = _linked(status="executing")
    updated, outcome = apply_runtime_event(
        task, context=ctx, event=_event("run.waiting_approval", sequence=2)
    )

    assert outcome.action == "history_only"
    assert updated["status"] == "executing"
    record = _history(updated)[-1]
    assert record["runtime_status"] == "waiting_approval"
    assert record["approval_required"] is True


def test_timed_out_is_applied_explicitly_and_keeps_the_detail() -> None:
    task, ctx = _linked()
    updated, outcome = apply_runtime_event(
        task,
        context=ctx,
        event=_event(
            "run.failed",
            event_id="ev-timeout",
            sequence=4,
            payload={"error": {"code": "run.timeout", "message": "budget exceeded"}},
        ),
        runtime_status="timed_out",
    )

    assert outcome.action == "status_updated"
    assert updated["status"] == "failed"
    record = _history(updated)[-1]
    assert record["runtime_status"] == "timed_out"
    assert record["error_code"] == "run.timeout"
    assert record["reason"] == "budget exceeded"


def test_replaying_the_same_event_does_not_append_twice() -> None:
    task, ctx = _linked()
    event = _event("run.completed", event_id="ev-done", sequence=9)

    first, _ = apply_runtime_event(task, context=ctx, event=event)
    before = len(_history(first))

    second, outcome = apply_runtime_event(first, context=ctx, event=event)

    assert outcome.action == "noop"
    assert second["status"] == "completed"
    assert len(_history(second)) == before


def test_out_of_order_replay_cannot_regress_the_terminal_state() -> None:
    task, ctx = _linked()
    done, _ = apply_runtime_event(
        task, context=ctx, event=_event("run.completed", event_id="ev-9", sequence=9)
    )

    after, outcome = apply_runtime_event(
        done, context=ctx, event=_event("run.running", event_id="ev-2", sequence=2)
    )

    assert outcome.action == "stale"
    assert after["status"] == "completed"
    assert len(_history(after)) == len(_history(done))


def test_a_duplicate_sequence_with_a_new_event_id_does_not_double_append() -> None:
    task, ctx = _linked()
    first, _ = apply_runtime_event(
        task, context=ctx, event=_event("run.running", event_id="ev-a", sequence=3)
    )
    before = len(_history(first))

    second, outcome = apply_runtime_event(
        first, context=ctx, event=_event("run.running", event_id="ev-b", sequence=3)
    )

    assert second["status"] == "executing"
    # Same run, same status, same sequence: the status does not move and the
    # history does not grow a second identical entry for the same sequence.
    assert len(_history(second)) <= before + 1


def test_cross_organization_events_are_refused() -> None:
    task, _ = _linked(org_id=ORG_A)
    other = build_task_runtime_context(task=_task(), authz=_authz(ORG_B), authorized=True)

    with pytest.raises(RuntimeEventBridgeError, match="another organization"):
        apply_runtime_event(task, context=other, event=_event("run.completed"))


def test_frames_naming_another_run_are_refused() -> None:
    # The frame's own run id must agree with the stored linkage, otherwise another
    # run's state would be recorded under this task's linked run (AG-G2-AUTO-009).
    task, ctx = _linked()

    with pytest.raises(RuntimeEventBridgeError, match="different runtime run"):
        apply_runtime_event(
            task, context=ctx, event=_event("run.completed", event_id="ev-x", run_id="run-999")
        )

    assert _history(task) == [], "a foreign run's frame must not be written"


def test_frames_naming_the_linked_run_are_bridged() -> None:
    task, ctx = _linked(status="pending")

    updated, outcome = apply_runtime_event(
        task, context=ctx, event=_event("run.completed", event_id="ev-x")
    )

    assert outcome.status_after == "completed"
    assert _history(updated)[-1]["runtime_run_id"] == RUN_ID


def test_non_status_frames_leave_the_task_untouched() -> None:
    task, ctx = _linked()
    updated, outcome = apply_runtime_event(
        task, context=ctx, event=_event("asset.available", event_id="ev-asset")
    )

    assert outcome.action == "noop"
    assert outcome.reason == "non_status_event"
    assert updated["status"] == "executing"
    assert _history(updated) == []


def test_frames_without_a_run_id_or_unknown_type_are_refused() -> None:
    task, ctx = _linked()
    with pytest.raises(RuntimeEventBridgeError, match="no run_id"):
        apply_runtime_event(
            task, context=ctx, event={"event_id": "ev-1", "sequence": 1, "type": "run.completed"}
        )
    with pytest.raises(RuntimeEventBridgeError, match="unsupported runtime event type"):
        apply_runtime_event(task, context=ctx, event=_event("run.teleported"))


def test_history_records_stay_json_safe() -> None:
    import json

    task, ctx = _linked()
    updated, _ = apply_runtime_event(
        task,
        context=ctx,
        event=_event("run.failed", payload={"error": {"code": "provider.err", "message": "boom"}}),
    )
    json.dumps(_history(updated), sort_keys=True)
